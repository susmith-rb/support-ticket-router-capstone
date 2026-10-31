"""Tests for multi-turn ticket holding and customer follow-up attachment.

# REQ-AGENT-033: Multi-Turn Conversation Holding and Customer Follow-up Attachment
# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
# REQ-AGENT-032: Ingress Payment Card Data Block & PCI Scope Isolation
# REQ-STORE-009: Reversible PII Tokens and KMS Envelope-Encrypted Redaction Maps
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, List, Tuple

import pytest

from ticket_router.agents.triage_router.a2a_client import A2AHandoffPayload
from ticket_router.agents.triage_router.agent import TriageRouterAgent
from ticket_router.agents.triage_router.mcp_client import InMemoryTicketDbMcpClient
from ticket_router.agents.triage_router.policy import (
    RouterThresholds,
    TriagePolicy,
)
from ticket_router.agents.triage_router.templates import (
    CARD_DATA_BLOCKED_REPLY,
    render_card_data_blocked_reply,
    render_followup_calming_reply,
)
from ticket_router.handoff.human_handoff import HumanHandoff
from ticket_router.handoff.publisher import FakePublisher
from ticket_router.models.router_s1_client import RouterPrediction
from ticket_router.security.model_armor import (
    ModelArmorVerdict,
    SdpFinding,
)


class CountingRouterS1:
    """Mock RouterS1 client counting model inference invocations."""

    def __init__(self, escalate: bool = False, p_esc: float = 0.05) -> None:
        self.calls = 0
        self.escalate = escalate
        self.p_esc = p_esc

    def route(self, text: str) -> RouterPrediction:
        self.calls += 1
        return RouterPrediction(
            intent="shipping_policy",
            intent_confidence=0.95,
            intent_probabilities={"shipping_policy": 0.95, "other": 0.05},
            escalate=self.escalate,
            escalate_probability=self.p_esc,
            sentiment="neutral",
            urgency="normal",
        )


class CountingTier2Invoker:
    """Mock Tier 2 Gemini invoker counting model inference invocations."""

    def __init__(self, escalate: bool = False) -> None:
        self.calls = 0
        self.escalate = escalate

    def invoke(self, text: str) -> RouterPrediction:
        self.calls += 1
        return RouterPrediction(
            intent="shipping_policy",
            intent_confidence=0.90,
            intent_probabilities={"shipping_policy": 0.90, "other": 0.10},
            escalate=self.escalate,
            escalate_probability=0.10,
            sentiment="neutral",
            urgency="normal",
        )


class CountingA2AClient:
    """Mock A2A client counting handoffs to FaqResolver."""

    def __init__(self) -> None:
        self.calls = 0

    def handoff(self, payload: A2AHandoffPayload) -> Dict[str, Any]:
        self.calls += 1
        return {
            "status": "AUTO_RESOLVED",
            "answer": "Our standard shipping takes 3-5 business days.",
            "citations": [{"id": "faq_shipping_1", "text": "3-5 business days"}],
        }


class FakeModelArmorWithFindings:
    """Mock Model Armor client returning configured SDP findings."""

    def __init__(self, findings: List[Dict[str, Any]]) -> None:
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
            latency_ms=1.0,
        )


class FakeRedactionSealer:
    """Hermetic in-memory mock for sealing redaction maps."""

    def __init__(self) -> None:
        self.sealed_calls: List[Tuple[str, Dict[str, str]]] = []

    def seal(self, ticket_id: str, mapping: Dict[str, str]) -> Tuple[str, str]:
        self.sealed_calls.append((ticket_id, mapping))
        raw = json.dumps(mapping).encode("utf-8")
        return base64.b64encode(raw).decode("ascii"), "key-version-test-1"


# REQ-AGENT-033
def test_followup_attaches_to_holding_ticket_with_zero_model_calls():
    """Turn 1 escalates to human queue; Turn 2 follow-up in same session attaches

    audit event and returns calming ack with ZERO model calls to S1, Gemini, or FaqResolver.
    """
    mcp_client = InMemoryTicketDbMcpClient()
    s1_client = CountingRouterS1()
    tier2_invoker = CountingTier2Invoker()
    a2a_client = CountingA2AClient()
    publisher = FakePublisher()
    human_handoff = HumanHandoff(
        ticket_db_client=mcp_client,
        publisher=publisher,
        escalated_by="triage_router",
    )
    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        s1_client=s1_client,  # type: ignore[arg-type]
        tier2_invoker=tier2_invoker,
    )
    agent = TriageRouterAgent(
        policy=policy,
        mcp_client=mcp_client,
        a2a_client=a2a_client,
        human_handoff=human_handoff,
    )

    session_id = "sess-hold-001"

    # Turn 1: Escalation via Tier 0 explicit human representative request
    res1 = agent.triage(
        session_id=session_id,
        message_text="I want to speak with a human agent please",
    )
    assert res1.status_code == 200
    assert res1.decision.decision == "escalate"
    active_ticket = mcp_client.get_active_ticket_for_session(session_id)
    assert active_ticket is not None
    holding_tid = str(active_ticket["ticket_id"])
    assert holding_tid in mcp_client.tickets
    assert len(publisher.published_messages) == 1

    # Reset counters before follow-up turn
    s1_client.calls = 0
    tier2_invoker.calls = 0
    a2a_client.calls = 0

    # Turn 2: Follow-up message in the same session
    res2 = agent.triage(
        session_id=session_id,
        message_text="Also, please check on my refund status while you review this.",
    )

    # Invariants: Zero model or FaqResolver calls, zero re-publishing to PubSub
    assert s1_client.calls == 0, f"Expected 0 S1 calls, got {s1_client.calls}"
    assert tier2_invoker.calls == 0, f"Expected 0 Tier2 calls, got {tier2_invoker.calls}"
    assert a2a_client.calls == 0, f"Expected 0 FaqResolver calls, got {a2a_client.calls}"
    assert len(publisher.published_messages) == 1, "Should not re-publish escalation event on follow-up"

    # Verifications on decision and response
    expected_human_tid = res1.ticket_id
    assert res2.ticket_id == expected_human_tid
    assert res2.decision.decision == "escalate"
    assert res2.decision.tier == "human_holding"
    assert res2.decision.predicted_intent == "followup"
    assert res2.calming_ack == render_followup_calming_reply(expected_human_tid)
    assert f"ticket #{expected_human_tid}" in res2.calming_ack
    assert holding_tid not in res2.calming_ack, "DB UUID must never be exposed to customers"

    assert res2.handoff_response is not None
    assert res2.handoff_response["status"] == "ESCALATED"
    assert res2.handoff_response["route"] == "followup"
    assert res2.handoff_response["ticket_id"] == expected_human_tid
    assert res2.handoff_response["holding_ticket_uuid"] == holding_tid
    assert res2.handoff_response["handoff_done"] is True

    # Audit event appended to ticket
    events = [e for e in mcp_client.events if e.get("ticket_id") == holding_tid]
    followup_events = [e for e in events if e.get("event_type") == "customer_followup"]
    assert len(followup_events) == 1
    assert followup_events[0]["payload"]["body_redacted"] == "Also, please check on my refund status while you review this."

    # Turn 3: Duplicate follow-up message is deduplicated
    res3 = agent.triage(
        session_id=session_id,
        message_text="Also, please check on my refund status while you review this.",
    )
    assert res3.ticket_id == expected_human_tid
    assert res3.calming_ack == render_followup_calming_reply(expected_human_tid)
    assert holding_tid not in res3.calming_ack, "DB UUID must never be exposed to customers"
    followup_events_after = [e for e in mcp_client.events if e.get("event_type") == "customer_followup"]
    assert len(followup_events_after) == 1, "Duplicate message should be deduplicated"


# REQ-AGENT-033
def test_releasing_statuses_allow_subsequent_routing():
    """Tickets in releasing statuses (resolved, resolved_auto, closed, returned_to_automation)

    release the session and allow subsequent turns to route normally through automation.
    """
    for rel_status in ("resolved", "resolved_auto", "closed", "returned_to_automation"):
        mcp_client = InMemoryTicketDbMcpClient()
        s1_client = CountingRouterS1()
        tier2_invoker = CountingTier2Invoker()
        a2a_client = CountingA2AClient()
        agent = TriageRouterAgent(
            policy=TriagePolicy(
                thresholds=RouterThresholds(),
                s1_client=s1_client,  # type: ignore[arg-type]
                tier2_invoker=tier2_invoker,
            ),
            mcp_client=mcp_client,
            a2a_client=a2a_client,
            human_handoff=HumanHandoff(
                ticket_db_client=mcp_client,
                publisher=FakePublisher(),
            ),
        )

        sess_id = f"sess-{rel_status}"
        # Pre-seed a ticket that has been released
        mcp_client.tickets["TICK-OLD"] = {
            "ticket_id": "TICK-OLD",
            "session_id": sess_id,
            "status": rel_status,
            "assigned_queue": "human-standard",
            "channel": "web",
        }

        # Send a normal FAQ question
        res = agent.triage(
            session_id=sess_id,
            message_text="What is your return policy?",
        )

        # Invariant: Must NOT be treated as a human holding follow-up
        assert res.decision.tier != "human_holding"
        assert res.handoff_response is None or res.handoff_response.get("route") != "followup"
        # S1 or FaqResolver ran normally
        assert s1_client.calls > 0 or a2a_client.calls > 0


def _failing_get_active(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("Cloud SQL connection pool exhausted: 500")


# REQ-AGENT-033
def test_lookup_failure_fails_open_to_normal_routing(caplog: pytest.LogCaptureFixture):
    """If get_active_ticket_for_session fails with an exception, triage logs warning

    and fails open to standard tiered routing without interrupting customer service.
    """
    mcp_client = InMemoryTicketDbMcpClient()
    mcp_client.get_active_ticket_for_session = _failing_get_active  # type: ignore[method-assign]

    agent = TriageRouterAgent(
        policy=TriagePolicy(
            thresholds=RouterThresholds(),
            s1_client=CountingRouterS1(),  # type: ignore[arg-type]
            tier2_invoker=CountingTier2Invoker().invoke,
        ),
        mcp_client=mcp_client,
        a2a_client=CountingA2AClient(),
        human_handoff=HumanHandoff(
            ticket_db_client=mcp_client,
            publisher=FakePublisher(),
        ),
    )

    with caplog.at_level(logging.WARNING):
        res = agent.triage(
            session_id="sess-failopen-001",
            message_text="Hello, how can you help me?",
        )

    assert res.status_code == 200
    # Tier 0 greeting was evaluated normally
    assert res.decision.tier == "tier0_rules"
    assert res.decision.details.get("category") == "greeting"

    # Structured warning logged
    assert any("get_active_ticket_failed" in rec.message for rec in caplog.records)


# REQ-AGENT-033
# REQ-AGENT-032
def test_followup_blocks_card_data_with_400():
    """Payment card data in a follow-up turn is blocked at ingress with HTTP 400

    and zero calls to persistence or ticket follow-up append.
    """
    mcp_client = InMemoryTicketDbMcpClient()
    mcp_client.tickets["TICK-CARD-1"] = {
        "ticket_id": "TICK-CARD-1",
        "session_id": "sess-card-001",
        "status": "assigned",
        "assigned_queue": "human-standard",
        "channel": "web",
    }

    card_text = "Here is my card 4111 1111 1111 1111 for the order"
    card_start = card_text.find("4111")
    card_end = card_start + len("4111 1111 1111 1111")
    finding = {
        "infoType": "CREDIT_CARD_NUMBER",
        "location": {
            "codepointRange": {
                "start": str(card_start),
                "end": str(card_end),
            }
        },
    }
    ma_client = FakeModelArmorWithFindings([finding])
    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        s1_client=CountingRouterS1(),  # type: ignore[arg-type]
        tier2_invoker=CountingTier2Invoker(),
        model_armor_client=ma_client,
    )
    agent = TriageRouterAgent(
        policy=policy,
        mcp_client=mcp_client,
        a2a_client=CountingA2AClient(),
        human_handoff=HumanHandoff(
            ticket_db_client=mcp_client,
            publisher=FakePublisher(),
        ),
    )

    res = agent.triage(
        session_id="sess-card-001",
        message_text=card_text,
    )

    assert res.status_code == 400
    assert res.error == "card_data_blocked"
    assert res.calming_ack == CARD_DATA_BLOCKED_REPLY
    assert res.calming_ack == render_card_data_blocked_reply()

    # Zero follow-up events persisted
    assert len(mcp_client.events) == 0


# REQ-AGENT-033
# REQ-STORE-009
def test_followup_tokenizes_pii_in_appended_event():
    """Customer follow-up containing PII (email/phone) is tokenized with reversible tokens

    in the appended ticket event, and the KMS envelope redaction map is sealed and stored.
    """
    mcp_client = InMemoryTicketDbMcpClient()
    mcp_client.tickets["TICK-PII-1"] = {
        "ticket_id": "TICK-PII-1",
        "session_id": "sess-pii-001",
        "status": "escalated",
        "assigned_queue": "human-standard",
        "channel": "web",
    }

    raw_text = "My alternate email is test.user@example.com for updates"
    email_str = "test.user@example.com"
    em_start = raw_text.find(email_str)
    em_end = em_start + len(email_str)
    finding = {
        "infoType": "EMAIL_ADDRESS",
        "location": {
            "codepointRange": {
                "start": str(em_start),
                "end": str(em_end),
            }
        },
    }
    ma_client = FakeModelArmorWithFindings([finding])
    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        s1_client=CountingRouterS1(),  # type: ignore[arg-type]
        tier2_invoker=CountingTier2Invoker(),
        model_armor_client=ma_client,
    )

    sealer = FakeRedactionSealer()
    agent = TriageRouterAgent(
        policy=policy,
        mcp_client=mcp_client,
        a2a_client=CountingA2AClient(),
        human_handoff=HumanHandoff(
            ticket_db_client=mcp_client,
            publisher=FakePublisher(),
        ),
        redaction_sealer=sealer,  # type: ignore[arg-type]
    )

    res = agent.triage(
        session_id="sess-pii-001",
        message_text=raw_text,
    )

    assert res.status_code == 200
    assert res.ticket_id == "TICK-PII-1"
    assert res.handoff_response["route"] == "followup"

    # Redaction map was sealed and saved
    assert len(sealer.sealed_calls) == 1
    assert "TICK-PII-1" in mcp_client.redaction_maps

    # Appended event contains tokenized body, not raw email
    ev = next(e for e in mcp_client.events if e.get("event_type") == "customer_followup")
    persisted_body = ev["payload"]["body_redacted"]
    assert "test.user@example.com" not in persisted_body
    assert "[EMAIL_ADDRESS_" in persisted_body


# REQ-AGENT-033
def test_open_auto_answered_ticket_does_not_hold_but_open_human_queue_does() -> None:
    """Live data: auto-answered tickets are also status "open"; only human-queue ones hold."""
    from ticket_router.store.models import is_holding_ticket

    assert is_holding_ticket("open", None) is False
    assert is_holding_ticket("open", "none") is False
    assert is_holding_ticket("open", "human-standard") is True
    assert is_holding_ticket("open", "human-priority") is True
    assert is_holding_ticket("assigned", None) is True
    assert is_holding_ticket("resolved_auto", "human-standard") is False
    assert is_holding_ticket("returned_to_automation", "human-standard") is False
