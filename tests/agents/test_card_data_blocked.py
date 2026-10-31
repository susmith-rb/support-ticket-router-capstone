"""Tests for ingress card data blocking and PCI scope isolation.

# REQ-MA-003: Ingress Concurrent Evaluation, Security Policy & Fail-Safe Escalation
# REQ-AGENT-032: Ingress Payment Card Data Block & PCI Scope Isolation
# REQ-STORE-009: Reversible PII Tokens and KMS Envelope-Encrypted Redaction Maps
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from ticket_router.agents.triage_router.adk_agent import (
    TriageRouterAdkAgent,
    extract_answer_from_result,
)
from ticket_router.agents.triage_router.agent import TriageRouterAgent
from ticket_router.agents.triage_router.mcp_client import InMemoryTicketDbMcpClient
from ticket_router.agents.triage_router.policy import (
    RouterThresholds,
    TriagePolicy,
    VerifiedFacts,
)
from ticket_router.agents.triage_router.templates import (
    CARD_DATA_BLOCKED_REPLY,
    render_card_data_blocked_reply,
)
from ticket_router.handoff.human_handoff import HumanHandoff
from ticket_router.handoff.publisher import FakePublisher
from ticket_router.models.router_s1_client import RouterPrediction
from ticket_router.security.model_armor import (
    ModelArmorVerdict,
    SdpFinding,
    parse_model_armor_response,
)

FIXTURE_PATH = Path(__file__).parents[1] / "security" / "fixtures" / "ma_advanced_sdp_response.json"
FIXTURE_TEXT = (
    "Reach me at jane.verify@example.com, phone 415-555-0100, SSN 123-45-6789, "
    "card 4111 1111 1111 1111"
)


class MockModelArmorWithFixture:
    """Mock client returning the live advanced SDP response fixture."""

    def __init__(self, fixture_path: Path = FIXTURE_PATH, original_text: str = FIXTURE_TEXT):
        self.data = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.original_text = original_text

    def sanitize_user_prompt(self, text: str) -> ModelArmorVerdict:
        return parse_model_armor_response(self.data, latency_ms=1.0, original_text=self.original_text)


class FakeModelArmorWithFindings:
    """Hermetic Model Armor client producing findings with dict-location ranges."""

    def __init__(self, findings: List[Dict[str, Any]]):
        self.findings = findings

    def sanitize_user_prompt(self, text: str) -> ModelArmorVerdict:
        sdp_findings = [
            SdpFinding(
                info_type=f["infoType"],
                likelihood="VERY_LIKELY",
                location=f.get("location"),
            )
            for f in self.findings
        ]
        return ModelArmorVerdict(
            is_clean=len(self.findings) == 0,
            blocked=False,
            action="ALLOW",
            sdp_findings=sdp_findings,
            raw_filter_results={
                "sdpFilterResult": {
                    "inspectResult": {
                        "findings": self.findings,
                    }
                }
            },
        )


class SpyA2AClient:
    """Spy A2A client tracking handoff invocations."""

    def __init__(self) -> None:
        self.calls: List[Any] = []

    def handoff(self, payload: Any) -> Dict[str, Any]:
        self.calls.append(payload)
        return {"status": "AUTO_RESOLVED", "answer": "resolved"}


def _fake_tier2_invoker(text: str) -> RouterPrediction:
    return RouterPrediction(
        intent="returns_refunds",
        escalate_prob=0.1,
        intent_prob=0.95,
        sentiment="neutral",
        urgency="normal",
        latency_ms=15.0,
    )


# REQ-MA-003
def test_policy_blocks_card_finding_from_live_fixture() -> None:
    """Verify policy returns blocked for a card finding shaped like the live fixture.

    Acceptance criteria for REQ-MA-003:
    - decision="blocked"
    - tier="ingress_guard"
    - reason="card_data_blocked"
    - escalate=False
    - queue="none"
    - tokenized_text is None, pii_mapping is empty
    - details={"blocked_info_types": ["CREDIT_CARD_NUMBER"]}
    - zero raw text or digits in details
    """
    mock_ma = MockModelArmorWithFixture()
    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        model_armor_client=mock_ma,
        tier2_invoker=_fake_tier2_invoker,
    )

    decision = policy.evaluate(
        text=FIXTURE_TEXT,
        session_id="sess-card-001",
        customer_id="cust-1001",
        verified_facts=VerifiedFacts(),
    )

    assert decision.decision == "blocked"
    assert decision.tier == "ingress_guard"
    assert decision.reason == "card_data_blocked"
    assert decision.escalate is False
    assert decision.queue == "none"
    assert decision.tokenized_text is None
    assert decision.pii_mapping == {}
    assert decision.details == {"blocked_info_types": ["CREDIT_CARD_NUMBER"]}

    # Verify zero raw card digits or raw text in details or decision reason
    assert "4111" not in str(decision.details)
    assert "4111" not in decision.reason
    assert FIXTURE_TEXT not in str(decision.details)


# REQ-AGENT-032
def test_agent_blocks_card_data_returns_400_and_zero_calls() -> None:
    """Verify agent returns status 400 and makes zero calls to store/queue/a2a.

    Acceptance criteria for REQ-AGENT-032:
    - status_code: 400, error: "card_data_blocked"
    - zero calls to mcp_client.upsert_ticket
    - zero calls to mcp_client.save_redaction_map
    - zero calls to human_handoff (publisher messages empty)
    - zero calls to a2a_client
    - customer-facing reply directs user to secure payment
    """
    mock_ma = MockModelArmorWithFixture()
    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        model_armor_client=mock_ma,
        tier2_invoker=_fake_tier2_invoker,
    )
    mcp_client = InMemoryTicketDbMcpClient()
    publisher = FakePublisher()
    handoff = HumanHandoff(ticket_db_client=mcp_client, publisher=publisher)
    spy_a2a = SpyA2AClient()

    agent = TriageRouterAgent(
        policy=policy,
        mcp_client=mcp_client,
        human_handoff=handoff,
        a2a_client=spy_a2a,
        lazy_init=False,
    )
    adk_agent = TriageRouterAdkAgent(router=agent)

    ticket_id = "test-ticket-card-blocked"
    result = agent.triage(
        session_id="sess-card-002",
        ticket_id=ticket_id,
        message_text=FIXTURE_TEXT,
    )

    # 1. Result attributes
    assert result.status_code == 400
    assert result.error == "card_data_blocked"
    assert result.decision.decision == "blocked"
    assert result.decision.tier == "ingress_guard"
    assert result.decision.queue == "none"
    assert result.calming_ack == CARD_DATA_BLOCKED_REPLY

    # 2. Dict subscript and conversion compatibility
    assert result["status_code"] == 400
    assert result["error"] == "card_data_blocked"
    assert result.to_dict()["status_code"] == 400
    assert result.to_dict()["error"] == "card_data_blocked"

    # 3. User-facing text extraction
    extracted = extract_answer_from_result(result)
    assert extracted == render_card_data_blocked_reply()
    assert "secure payment page" in extracted

    # 4. Invariant: ZERO persistence, ZERO redaction map, ZERO A2A, ZERO HumanHandoff
    assert len(mcp_client.tickets) == 0
    assert len(mcp_client.redaction_maps) == 0
    assert len(publisher.published_messages) == 0
    assert len(spy_a2a.calls) == 0

    # 5. ADK agent query() structured response
    adk_out = adk_agent.query(FIXTURE_TEXT, session_id="sess-card-002")
    assert adk_out["status_code"] == 400
    assert adk_out["error"] == "card_data_blocked"
    assert adk_out["decision"] == "blocked"
    assert adk_out["queue"] == "none"
    assert CARD_DATA_BLOCKED_REPLY in adk_out["answer"]


# REQ-MA-003 / REQ-AGENT-032
def test_tier0_urgent_plus_card_is_blocked_not_escalated() -> None:
    """Verify that a message matching Tier 0 urgent rules + card data is blocked, NOT escalated."""
    urgent_card_text = (
        "URGENT: I need to speak to a representative right now! "
        "Here is my card number 4111 2222 3333 4444."
    )
    card_finding = {
        "infoType": "CREDIT_CARD_NUMBER",
        "location": {
            "codepointRange": {
                "start": urgent_card_text.index("4111 2222 3333 4444"),
                "end": urgent_card_text.index("4111 2222 3333 4444") + len("4111 2222 3333 4444"),
            }
        },
    }
    fake_ma = FakeModelArmorWithFindings([card_finding])
    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        model_armor_client=fake_ma,
    )
    mcp_client = InMemoryTicketDbMcpClient()
    publisher = FakePublisher()
    handoff = HumanHandoff(ticket_db_client=mcp_client, publisher=publisher)

    # 1. Policy evaluate directly
    decision = policy.evaluate(
        text=urgent_card_text,
        session_id="sess-t0-card",
        customer_id="cust-1001",
        verified_facts=VerifiedFacts(),
    )
    assert decision.decision == "blocked"
    assert decision.escalate is False
    assert decision.tier == "ingress_guard"
    assert decision.queue == "none"
    assert decision.reason == "card_data_blocked"
    assert decision.details == {"blocked_info_types": ["CREDIT_CARD_NUMBER"]}

    # 2. Agent triage turn
    agent = TriageRouterAgent(
        policy=policy,
        mcp_client=mcp_client,
        human_handoff=handoff,
        lazy_init=False,
    )
    res = agent.triage(
        session_id="sess-t0-card",
        ticket_id="TICK-T0-CARD",
        message_text=urgent_card_text,
    )
    assert res.status_code == 400
    assert res.error == "card_data_blocked"
    assert res.decision.decision == "blocked"
    assert len(mcp_client.tickets) == 0
    assert len(publisher.published_messages) == 0


# REQ-STORE-009 / REQ-MA-003
def test_non_card_pii_still_tokenizes() -> None:
    """Verify that non-card PII (e.g. email) still tokenizes and does not get blocked."""
    email = "bob@example.com"
    email_text = f"Hello support, please check my order. My email is {email}."
    email_finding = {
        "infoType": "EMAIL_ADDRESS",
        "location": {
            "codepointRange": {
                "start": email_text.index(email),
                "end": email_text.index(email) + len(email),
            }
        },
    }
    fake_ma = FakeModelArmorWithFindings([email_finding])
    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        model_armor_client=fake_ma,
        tier2_invoker=_fake_tier2_invoker,
    )
    mcp_client = InMemoryTicketDbMcpClient()
    publisher = FakePublisher()
    handoff = HumanHandoff(ticket_db_client=mcp_client, publisher=publisher)

    decision = policy.evaluate(
        text=email_text,
        session_id="sess-noncard",
        customer_id="cust-1001",
        verified_facts=VerifiedFacts(),
    )
    assert decision.decision != "blocked"
    assert decision.tokenized_text is not None
    assert "[EMAIL_ADDRESS_1]" in decision.tokenized_text
    assert email not in decision.tokenized_text
    assert decision.pii_mapping == {"[EMAIL_ADDRESS_1]": email}

    agent = TriageRouterAgent(
        policy=policy,
        mcp_client=mcp_client,
        human_handoff=handoff,
        lazy_init=False,
    )
    res = agent.triage(
        session_id="sess-noncard",
        ticket_id="TICK-NONCARD-1",
        message_text=email_text,
    )
    assert res.status_code == 200
    assert res.error is None
    assert len(mcp_client.tickets) > 0


# REQ-MA-003
def test_structured_event_logged_on_card_blocked(caplog: Any) -> None:
    """Verify structured event 'card_data_blocked' is logged with info types only (no text)."""
    mock_ma = MockModelArmorWithFixture()
    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        model_armor_client=mock_ma,
        tier2_invoker=_fake_tier2_invoker,
    )
    agent = TriageRouterAgent(
        policy=policy,
        mcp_client=InMemoryTicketDbMcpClient(),
        lazy_init=False,
    )

    ticket_id = "test-log-audit-001"
    with caplog.at_level(logging.WARNING):
        res = agent.triage(
            session_id="sess-audit-001",
            ticket_id=ticket_id,
            message_text=FIXTURE_TEXT,
        )

    assert res.status_code == 400
    log_records = [
        json.loads(r.getMessage())
        for r in caplog.records
        if "card_data_blocked" in r.getMessage() and r.getMessage().strip().startswith("{")
    ]
    assert len(log_records) >= 1
    record = log_records[0]
    assert record.get("event") == "card_data_blocked"
    assert record.get("ticket_id") == ticket_id
    assert record.get("blocked_info_types") == ["CREDIT_CARD_NUMBER"]
    # Invariant: No customer message text or card digits in log
    assert "4111" not in json.dumps(record)
    assert "jane.verify" not in json.dumps(record)
