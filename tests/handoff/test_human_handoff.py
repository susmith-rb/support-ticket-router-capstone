"""Unit tests for shared HumanHandoff workflow (REQ-AGENT-030).

# REQ-AGENT-030: Shared HumanHandoff Workflow (deterministic, used by TriageRouter and FaqResolver)
# REQ-AGENT-004: Gaming-Resistant Queue Priority Assignment
# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from ticket_router.agents.triage_router.mcp_client import InMemoryTicketDbMcpClient
from ticket_router.handoff.human_handoff import HandoffResult, HumanHandoff
from ticket_router.handoff.publisher import FakePublisher


def test_escalate_persists_publishes_and_acks():
    """Verify escalate() persists state, publishes PII-free event, and returns calming ack (REQ-AGENT-030)."""
    mcp_client = InMemoryTicketDbMcpClient()
    publisher = FakePublisher()
    handoff = HumanHandoff(
        ticket_db_client=mcp_client,
        publisher=publisher,
        escalated_by="triage_router",
        topic="human-queue",
    )

    result = handoff.escalate(
        ticket_id="TICK-101",
        queue="human-urgent",
        reason="critical_billing_escalation",
        escalated_by="triage_router",
        degraded=False,
        priority="urgent",
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        customer_id="cust-42",
        session_id="sess-101",
        body_redacted="Sanitized inquiry text",
        channel="web",
    )

    # 1. HandoffResult contracts
    assert isinstance(result, HandoffResult)
    assert result.persisted is True
    assert result.published is True
    assert result.message_id is not None
    assert "TICK-101" in result.calming_ack
    assert "15 minutes" in result.calming_ack

    # 2. Persisted state verification in MCP ticket-db client
    assert "TICK-101:escalation" in mcp_client.by_idempotency_key
    ticket_uuid = mcp_client.by_idempotency_key["TICK-101:escalation"]
    persisted_ticket = mcp_client.tickets[ticket_uuid]
    assert persisted_ticket["decision"]["queue"] == "human-urgent"
    assert persisted_ticket["decision"]["decision"] == "escalate"
    assert persisted_ticket["status"] == "OPEN"

    # 3. Published message verification in FakePublisher
    assert len(publisher.published_messages) == 1
    published = publisher.published_messages[0]
    assert published["topic"] == "human-queue"
    assert published["ordering_key"] == "TICK-101"
    assert published["attributes"] == {
        "event_type": "ticket.escalated",
        "queue": "human-urgent",
        "escalated_by": "triage_router",
    }


def test_event_payload_is_pii_free():
    """Verify published JSON payload conforms strictly to schema ticket.escalated/v1 without PII (REQ-AGENT-030)."""
    mcp_client = InMemoryTicketDbMcpClient()
    publisher = FakePublisher()
    handoff = HumanHandoff(
        ticket_db_client=mcp_client,
        publisher=publisher,
        escalated_by="faq_resolver",
    )

    raw_traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    handoff.escalate(
        ticket_id="TICK-202",
        queue="human-standard",
        reason="uncited_answer",
        escalated_by="faq_resolver",
        degraded=True,
        traceparent=raw_traceparent,
        customer_id="cust-sensitive-pii-999",
        session_id="sess-secret-token",
        body_redacted="Sensitive message text should never be in pubsub payload",
    )

    assert len(publisher.published_messages) == 1
    published = publisher.published_messages[0]
    payload = json.loads(published["data"].decode("utf-8"))

    # Strict schema validation: exactly {schema, ticket_id, ticket_uuid, queue, priority, reason, escalated_by, degraded, ts, traceparent}
    expected_keys = {
        "schema",
        "ticket_id",
        "ticket_uuid",
        "queue",
        "priority",
        "reason",
        "escalated_by",
        "degraded",
        "ts",
        "traceparent",
    }
    assert set(payload.keys()) == expected_keys
    assert payload["schema"] == "ticket.escalated/v1"
    assert payload["ticket_id"] == "TICK-202"
    assert "ticket_uuid" in payload
    assert payload["queue"] == "human-standard"
    assert payload["priority"] == "standard"
    assert payload["reason"] == "uncited_answer"
    assert payload["escalated_by"] == "faq_resolver"
    assert payload["degraded"] is True
    assert payload["traceparent"] == raw_traceparent

    # Assert ABSOLUTELY NO PII or sensitive keys
    forbidden_keys = {
        "customer_id",
        "message",
        "message_text",
        "body",
        "body_redacted",
        "session_id",
        "token",
        "tokens",
        "redaction_map",
        "sanitized_text",
    }
    for forbidden in forbidden_keys:
        assert forbidden not in payload, f"Forbidden key '{forbidden}' found in Pub/Sub payload"

    payload_str = json.dumps(payload)
    assert "cust-sensitive-pii-999" not in payload_str
    assert "sess-secret-token" not in payload_str
    assert "Sensitive message text" not in payload_str


def test_publish_retries_and_recovers():
    """Verify publisher retries up to 3 times and succeeds if an early attempt fails (REQ-AGENT-030)."""
    mcp_client = InMemoryTicketDbMcpClient()
    # Fails first 2 attempts, succeeds on 3rd attempt
    publisher = FakePublisher(fail_times=2)
    handoff = HumanHandoff(
        ticket_db_client=mcp_client,
        publisher=publisher,
        escalated_by="triage_router",
    )

    result = handoff.escalate(
        ticket_id="TICK-303",
        queue="human-standard",
        reason="transient_retry_test",
    )

    assert result.persisted is True
    assert result.published is True
    assert publisher.call_count == 3
    assert len(publisher.published_messages) == 1


def test_publish_failure_records_event_and_returns_published_false():
    """Verify persistent ticket records publish_failed event when publish attempts are exhausted (REQ-AGENT-030)."""
    mcp_client = InMemoryTicketDbMcpClient()
    # Always fails publish
    publisher = FakePublisher(always_fail=True)
    handoff = HumanHandoff(
        ticket_db_client=mcp_client,
        publisher=publisher,
        escalated_by="triage_router",
    )

    result = handoff.escalate(
        ticket_id="TICK-404",
        queue="human-standard",
        reason="permanent_publish_failure",
    )

    assert result.persisted is True
    assert result.published is False
    assert result.message_id is None
    # Calming ack must still be provided so user is never stranded
    assert "TICK-404" in result.calming_ack
    assert "2 hours" in result.calming_ack

    # Verify publish_failed event was recorded
    assert len(mcp_client.events) >= 1
    event = mcp_client.events[0]
    assert event["event_type"] == "publish_failed"
    assert event["ticket_id"] == "TICK-404"
    assert event["actor"] == "triage_router"


def test_escalate_never_raises_on_catastrophic_failure():
    """Verify HumanHandoff.escalate never raises an exception even if all dependencies throw (REQ-AGENT-030)."""
    bad_mcp = MagicMock()
    bad_mcp.upsert_ticket.side_effect = RuntimeError("Cloud SQL down")
    bad_publisher = MagicMock()
    bad_publisher.publish.side_effect = RuntimeError("PubSub unreachable")

    handoff = HumanHandoff(
        ticket_db_client=bad_mcp,
        publisher=bad_publisher,
        escalated_by="triage_router",
    )

    # Must never raise into the ticket flow
    result = handoff.escalate(
        ticket_id="TICK-505",
        queue="human-urgent",
        reason="chaos_failure",
    )

    assert isinstance(result, HandoffResult)
    assert result.persisted is False
    assert result.published is False
    assert result.message_id is None
    assert "TICK-505" in result.calming_ack
    assert "15 minutes" in result.calming_ack


# REQ-AGENT-030
def test_escalate_includes_canonical_ticket_uuid_in_payload():
    """Verify escalate() includes ticket_uuid in payload and uses it for persistence (REQ-AGENT-030)."""
    mcp_client = InMemoryTicketDbMcpClient()
    publisher = FakePublisher()
    handoff = HumanHandoff(
        ticket_db_client=mcp_client,
        publisher=publisher,
        escalated_by="triage_router",
        topic="human-queue",
    )

    explicit_uuid = "12345678-1234-5678-1234-567812345678"
    result = handoff.escalate(
        ticket_id="TICK-555",
        ticket_uuid=explicit_uuid,
        queue="human-standard",
        reason="test_uuid_propagation",
        idempotency_key="sess-1:msg-hash-1",
    )

    assert result.published is True
    assert len(publisher.published_messages) == 1
    published = publisher.published_messages[0]
    payload = json.loads(published["data"].decode("utf-8"))
    assert payload["ticket_id"] == "TICK-555"
    assert payload["ticket_uuid"] == explicit_uuid

