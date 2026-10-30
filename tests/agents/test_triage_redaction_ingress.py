"""Tests for TriageRouter PII tokenization and KMS envelope redaction map ingress.

# REQ-STORE-009: Reversible PII Tokens and KMS Envelope-Encrypted Redaction Maps
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, List, Optional

from ticket_router.agents.triage_router.agent import TriageRouterAgent
from ticket_router.agents.triage_router.mcp_client import InMemoryTicketDbMcpClient
from ticket_router.agents.triage_router.policy import RouterThresholds, TriagePolicy
from ticket_router.handoff.human_handoff import HumanHandoff
from ticket_router.handoff.publisher import FakePublisher
from ticket_router.models.router_s1_client import RouterPrediction
from ticket_router.security.envelope import EnvelopeCipher, RedactionSealer
from ticket_router.security.model_armor import (
    ModelArmorVerdict,
    SdpFinding,
)


class MockKmsSession:
    """Hermetic in-memory mock for KMS REST encrypt/decrypt calls."""

    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail

    def post(self, url: str, json: dict, headers: dict, timeout: float = 10.0):
        if self.should_fail:
            raise RuntimeError("Cloud KMS simulation error: 503 Service Unavailable")

        class Resp:
            def __init__(self, data):
                self._data = data
                self.status_code = 200

            def json(self):
                return self._data

            def raise_for_status(self):
                pass

        payload = json or {}
        if url.endswith(":encrypt"):
            raw_dek = base64.b64decode(payload.get("plaintext", ""))
            return Resp({
                "ciphertext": base64.b64encode(b"MOCK_WRAP:" + raw_dek).decode("ascii"),
                "name": "projects/test/locations/us-central1/keyRings/test/cryptoKeys/redaction-map-wrapper/cryptoKeyVersions/1",
            })
        if url.endswith(":decrypt"):
            ct = base64.b64decode(payload.get("ciphertext", ""))
            assert ct.startswith(b"MOCK_WRAP:")
            return Resp({
                "plaintext": base64.b64encode(ct[len(b"MOCK_WRAP:"):]).decode("ascii"),
            })
        return Resp({})


class FakeModelArmorWithSdp:
    """Hermetic Model Armor client producing SDP inspection findings."""

    def __init__(self, targets: Optional[List[str]] = None):
        self.targets = targets or ["alice@example.com"]

    def sanitize_user_prompt(self, text: str) -> ModelArmorVerdict:
        findings: List[Dict[str, Any]] = []
        sdp_findings: List[SdpFinding] = []
        for target in self.targets:
            start = 0
            while True:
                idx = text.find(target, start)
                if idx == -1:
                    break
                end = idx + len(target)
                loc_dict = {"codepointRange": {"start": idx, "end": end}}
                findings.append({
                    "infoType": "EMAIL_ADDRESS",
                    "location": loc_dict,
                })
                sdp_findings.append(
                    SdpFinding(info_type="EMAIL_ADDRESS", location=str(loc_dict))
                )
                start = end

        return ModelArmorVerdict(
            is_clean=len(findings) == 0,
            blocked=False,
            action="ALLOW",
            sdp_findings=sdp_findings,
            sanitized_text=text.replace("alice@example.com", "[REDACTED_EMAIL_ADDRESS]"),
            raw_filter_results={
                "sdpFilterResult": {
                    "inspectResult": {
                        "findings": findings,
                    }
                }
            },
        )


def _fake_tier2_invoker(text: str) -> RouterPrediction:
    return RouterPrediction(
        intent="returns_refunds",
        escalate_prob=0.1,
        intent_prob=0.95,
        sentiment="neutral",
        urgency="normal",
        latency_ms=15.0,
    )


# REQ-STORE-009
def test_triage_redaction_ingress_end_to_end(monkeypatch):
    """Verify end-to-end PII tokenization and KMS envelope sealing at ingress.

    Acceptance criteria for REQ-STORE-009:
    1. Persisted body contains reversible typed token [EMAIL_ADDRESS_1], not raw email.
    2. Persisted decision JSON contains no PII and no plaintext mapping.
    3. save_redaction_map called once with an envelope that decrypts to the mapping.
    """
    key_name = "projects/test/locations/us-central1/keyRings/test/cryptoKeys/redaction-map-wrapper"
    monkeypatch.setenv("REDACTION_KMS_KEY", key_name)

    kms_session = MockKmsSession()
    cipher = EnvelopeCipher(kms_key_name=key_name, session=kms_session)
    sealer = RedactionSealer(cipher=cipher)

    email = "alice@example.com"
    user_msg = f"Hello support, please check my order. My email is {email}."

    fake_ma = FakeModelArmorWithSdp(targets=[email])
    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        model_armor_client=fake_ma,
        tier2_invoker=_fake_tier2_invoker,
    )
    mcp_client = InMemoryTicketDbMcpClient()
    handoff = HumanHandoff(ticket_db_client=mcp_client, publisher=FakePublisher())

    agent = TriageRouterAgent(
        policy=policy,
        mcp_client=mcp_client,
        human_handoff=handoff,
        redaction_sealer=sealer,
        lazy_init=False,
    )

    ticket_id = "test-ticket-001"
    session_id = "session-001"
    resp = agent.triage(
        session_id=session_id,
        ticket_id=ticket_id,
        message_text=user_msg,
    )

    assert resp is not None

    # 1. Verify ticket row exists in MCP store
    assert len(mcp_client.tickets) > 0
    ticket_row = list(mcp_client.tickets.values())[0]

    # 2. Body persisted contains [EMAIL_ADDRESS_1] and NOT alice@example.com
    persisted_body = ticket_row["body_redacted"]
    assert "[EMAIL_ADDRESS_1]" in persisted_body
    assert email not in persisted_body

    # 3. Decision JSON contains no PII and no mapping
    dec_dict = ticket_row["decision"]
    dec_json_str = json.dumps(dec_dict)
    assert email not in dec_json_str
    assert "pii_mapping" not in dec_dict
    assert "tokenized_text" not in dec_dict
    assert "mapping" not in dec_dict.get("details", {})
    assert "pii_mapping" not in dec_dict.get("details", {})

    # 4. save_redaction_map called once with valid envelope
    assert hasattr(mcp_client, "redaction_maps")
    assert ticket_id in mcp_client.redaction_maps
    stored_maps = mcp_client.redaction_maps[ticket_id]
    assert len(stored_maps) == 1
    stored_map = stored_maps[0]

    assert stored_map["key_id"] == "kms-envelope-v1"
    envelope_b64 = stored_map["envelope_b64"]
    assert envelope_b64

    # 5. Envelope decrypts to the expected mapping using KMS session and AAD=ticket_id
    decrypted_bytes = cipher.decrypt(
        envelope_b64.encode("ascii"),
        aad=ticket_id.encode("utf-8"),
    )
    decrypted_map = json.loads(decrypted_bytes.decode("utf-8"))
    assert decrypted_map == {"[EMAIL_ADDRESS_1]": email}


# REQ-STORE-009
def test_triage_redaction_kms_failure_does_not_break_turn(monkeypatch, caplog):
    """Verify KMS failure fails closed without breaking the customer turn.

    Acceptance criteria:
    - KMS encryption failure logs 'redaction_map_failed' with ticket_id only.
    - Persisted body remains tokenized (no plaintext leaks).
    - Turn completes and returns a response without crashing.
    """
    key_name = "projects/test/locations/us-central1/keyRings/test/cryptoKeys/redaction-map-wrapper"
    monkeypatch.setenv("REDACTION_KMS_KEY", key_name)

    failing_kms = MockKmsSession(should_fail=True)
    cipher = EnvelopeCipher(kms_key_name=key_name, session=failing_kms)
    sealer = RedactionSealer(cipher=cipher)

    email = "bob@example.com"
    user_msg = f"I need a refund, my email is {email}."

    fake_ma = FakeModelArmorWithSdp(targets=[email])
    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        model_armor_client=fake_ma,
        tier2_invoker=_fake_tier2_invoker,
    )
    mcp_client = InMemoryTicketDbMcpClient()
    handoff = HumanHandoff(ticket_db_client=mcp_client, publisher=FakePublisher())

    agent = TriageRouterAgent(
        policy=policy,
        mcp_client=mcp_client,
        human_handoff=handoff,
        redaction_sealer=sealer,
        lazy_init=False,
    )

    ticket_id = "test-ticket-kms-fail"
    with caplog.at_level(logging.WARNING):
        resp = agent.triage(
            session_id="session-kms-fail",
            ticket_id=ticket_id,
            message_text=user_msg,
        )

    # 1. Turn finishes gracefully
    assert resp is not None

    # 2. Ticket was persisted safely with tokenized body (fail-closed)
    assert len(mcp_client.tickets) > 0
    ticket_row = list(mcp_client.tickets.values())[0]
    assert "[EMAIL_ADDRESS_1]" in ticket_row["body_redacted"]
    assert email not in ticket_row["body_redacted"]

    # 3. Decision contains no PII
    assert email not in json.dumps(ticket_row["decision"])

    # 4. redaction_map_failed was logged with ticket_id only (no PII, no mapping)
    log_records = [r.getMessage() for r in caplog.records if "redaction_map_failed" in r.getMessage()]
    assert len(log_records) >= 1
    log_json = json.loads(log_records[0])
    assert log_json.get("event") == "redaction_map_failed"
    assert log_json.get("ticket_id") == ticket_id
    assert email not in log_records[0]


# REQ-STORE-009
def test_triage_redaction_escalation_path(monkeypatch):
    """Verify PII tokenization and envelope sealing on the escalation branch."""
    key_name = "projects/test/locations/us-central1/keyRings/test/cryptoKeys/redaction-map-wrapper"
    monkeypatch.setenv("REDACTION_KMS_KEY", key_name)

    kms_session = MockKmsSession()
    cipher = EnvelopeCipher(kms_key_name=key_name, session=kms_session)
    sealer = RedactionSealer(cipher=cipher)

    email = "charlie@example.com"
    user_msg = f"My package arrived broken and damaged. My email is {email}."

    fake_ma = FakeModelArmorWithSdp(targets=[email])

    def escalating_tier2(text: str) -> RouterPrediction:
        return RouterPrediction(
            intent="returns_refunds",
            escalate=True,
            escalate_prob=0.85,
            intent_prob=0.95,
            sentiment="angry",
            urgency="immediate",
            latency_ms=25.0,
        )

    policy = TriagePolicy(
        thresholds=RouterThresholds(),
        model_armor_client=fake_ma,
        tier2_invoker=escalating_tier2,
    )
    mcp_client = InMemoryTicketDbMcpClient()
    handoff = HumanHandoff(ticket_db_client=mcp_client, publisher=FakePublisher())

    agent = TriageRouterAgent(
        policy=policy,
        mcp_client=mcp_client,
        human_handoff=handoff,
        redaction_sealer=sealer,
        lazy_init=False,
    )

    ticket_id = "test-ticket-esc"
    resp = agent.triage(
        session_id="session-esc",
        ticket_id=ticket_id,
        message_text=user_msg,
    )

    assert resp is not None
    assert len(mcp_client.tickets) > 0
    ticket_row = list(mcp_client.tickets.values())[0]
    assert ticket_row["status"] == "OPEN"  # Escalated tickets are stored with status OPEN in InMemory client
    assert "[EMAIL_ADDRESS_1]" in ticket_row["body_redacted"]
    assert email not in ticket_row["body_redacted"]

    # Redaction map was stored for the escalated ticket
    assert ticket_id in mcp_client.redaction_maps
    stored_map = mcp_client.redaction_maps[ticket_id][0]
    decrypted = cipher.decrypt(
        stored_map["envelope_b64"].encode("ascii"),
        aad=ticket_id.encode("utf-8"),
    )
    assert json.loads(decrypted.decode("utf-8")) == {"[EMAIL_ADDRESS_1]": email}
