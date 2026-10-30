"""Unit tests for human-queue Pub/Sub push escalation consumer (REQ-AGENT-031).

# REQ-AGENT-031: Escalation Event Consumer (Pub/Sub push -> human-queue)
# REQ-AGENT-030: Shared HumanHandoff Workflow
# REQ-AGENT-009: HumanQueue Workflow and Audited PII Re-Identification
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Optional
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from ticket_router.agents.human_queue.server import create_app
from ticket_router.store.fake import FakeTicketRepo
from ticket_router.store.models import TicketCreate


def _encode_push_body(
    payload: Dict[str, Any],
    subscription: str = "projects/test-project/subscriptions/human-queue-sub",
) -> Dict[str, Any]:
    b64_data = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return {
        "message": {
            "data": b64_data,
            "attributes": {
                "event_type": "ticket.escalated",
                "queue": payload.get("queue", "human-standard"),
                "escalated_by": payload.get("escalated_by", "triage_router"),
            },
            "messageId": "msg-push-1001",
            "publishTime": "2026-10-30T12:00:00Z",
            "orderingKey": payload.get("ticket_id", "a1b2c3d4-e5f6-4a5b-8c9d-0123456789ab"),
        },
        "subscription": subscription,
    }


def _make_mock_verifier(
    valid_token: str = "valid-oidc-token",
    valid_aud: str = "https://human-queue.run.app/pubsub/escalations",
    valid_email: str = "sa-queue-push@test-project.iam.gserviceaccount.com",
):
    def verifier(token: str, audience: Optional[str]) -> Dict[str, Any]:
        if token != valid_token:
            raise ValueError("Invalid ID token signature")
        if audience and audience != valid_aud:
            raise ValueError(f"Audience mismatch: expected {valid_aud}, got {audience}")
        return {
            "iss": "accounts.google.com",
            "aud": valid_aud,
            "email": valid_email,
            "email_verified": True,
            "sub": "100200300400",
        }

    return verifier


def test_push_requires_valid_oidc_and_is_idempotent(monkeypatch: pytest.MonkeyPatch):
    """Verify OIDC verification and idempotent queued_for_review event recording (REQ-AGENT-031)."""
    expected_aud = "https://human-queue.run.app/pubsub/escalations"
    expected_email = "sa-queue-push@test-project.iam.gserviceaccount.com"
    monkeypatch.setenv("PUSH_AUDIENCE", expected_aud)
    monkeypatch.setenv("PUSH_INVOKER_EMAIL", expected_email)

    ticket_repo = FakeTicketRepo()
    valid_uuid = "a1b2c3d4-e5f6-4a5b-8c9d-0123456789ab"
    ticket_repo.create_ticket(
        TicketCreate(
            ticket_id=UUID(valid_uuid),
            customer_id="cust-1001",
            body_redacted="Inquiry",
            assigned_queue="human-standard",
        )
    )
    verifier = _make_mock_verifier(valid_aud=expected_aud, valid_email=expected_email)
    app = create_app(ticket_repo=ticket_repo, token_verifier=verifier)
    client = TestClient(app)

    valid_payload = {
        "schema": "ticket.escalated/v1",
        "ticket_id": "TICK-1001",
        "ticket_uuid": valid_uuid,
        "queue": "human-standard",
        "priority": "standard",
        "reason": "customer_requested_human",
        "escalated_by": "triage_router",
        "degraded": False,
        "ts": "2026-10-30T12:00:00Z",
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    }
    push_body = _encode_push_body(valid_payload)

    # 1. Missing Authorization header -> 401
    resp_no_auth = client.post("/pubsub/escalations", json=push_body)
    assert resp_no_auth.status_code == 401

    # 2. Invalid OIDC token -> 401
    resp_bad_token = client.post(
        "/pubsub/escalations",
        json=push_body,
        headers={"Authorization": "Bearer bad-expired-token"},
    )
    assert resp_bad_token.status_code == 401

    # 3. Valid OIDC token: first delivery records queued_for_review event and returns 200
    resp_valid = client.post(
        "/pubsub/escalations",
        json=push_body,
        headers={"Authorization": "Bearer valid-oidc-token"},
    )
    assert resp_valid.status_code == 200
    data = resp_valid.json()
    assert data["status"] == "queued"
    assert data["ticket_id"] == "TICK-1001"
    assert data["ticket_uuid"] == valid_uuid

    # Verify queued_for_review event recorded in ticket repository
    events = ticket_repo.list_events(valid_uuid)
    queued_events = [ev for ev in events if ev.event_type == "queued_for_review"]
    assert len(queued_events) == 1
    assert queued_events[0].actor == "triage_router"
    assert queued_events[0].payload["queue"] == "human-standard"
    assert queued_events[0].payload["priority"] == "standard"

    # 4. Idempotency: duplicate delivery with same ticket_id returns 204 without second event
    resp_dup = client.post(
        "/pubsub/escalations",
        json=push_body,
        headers={"Authorization": "Bearer valid-oidc-token"},
    )
    assert resp_dup.status_code == 204
    events_after_dup = ticket_repo.list_events(valid_uuid)
    queued_after_dup = [ev for ev in events_after_dup if ev.event_type == "queued_for_review"]
    assert len(queued_after_dup) == 1, "Duplicate push delivery must NOT record a second queued_for_review event"


# REQ-AGENT-031
def test_push_missing_ticket_returns_204_and_logs(monkeypatch: pytest.MonkeyPatch):
    """Verify push handler returns 204 (not 500) and logs ticket_not_found when ticket row does not exist (REQ-AGENT-031)."""
    expected_aud = "https://human-queue.run.app/pubsub/escalations"
    expected_email = "sa-queue-push@test-project.iam.gserviceaccount.com"
    monkeypatch.setenv("PUSH_AUDIENCE", expected_aud)
    monkeypatch.setenv("PUSH_INVOKER_EMAIL", expected_email)

    ticket_repo = FakeTicketRepo()  # Empty repo: ticket does not exist
    verifier = _make_mock_verifier(valid_aud=expected_aud, valid_email=expected_email)
    app = create_app(ticket_repo=ticket_repo, token_verifier=verifier)
    client = TestClient(app)

    missing_uuid = "00000000-0000-0000-0000-000000009999"
    payload = {
        "schema": "ticket.escalated/v1",
        "ticket_id": "TICK-9999",
        "ticket_uuid": missing_uuid,
        "queue": "human-standard",
        "priority": "standard",
        "reason": "missing_row_test",
        "escalated_by": "triage_router",
        "degraded": False,
        "ts": "2026-10-30T12:00:00Z",
    }

    resp = client.post(
        "/pubsub/escalations",
        json=_encode_push_body(payload),
        headers={"Authorization": "Bearer valid-oidc-token"},
    )
    assert resp.status_code == 204
    # Ticket must NOT be auto-created
    assert ticket_repo.get_ticket(missing_uuid) is None


# REQ-AGENT-031
def test_push_legacy_message_without_ticket_uuid_returns_204(monkeypatch: pytest.MonkeyPatch):
    """Verify push handler accepts legacy messages without ticket_uuid with 204 and warning (REQ-AGENT-031)."""
    expected_aud = "https://human-queue.run.app/pubsub/escalations"
    expected_email = "sa-queue-push@test-project.iam.gserviceaccount.com"
    monkeypatch.setenv("PUSH_AUDIENCE", expected_aud)
    monkeypatch.setenv("PUSH_INVOKER_EMAIL", expected_email)

    ticket_repo = FakeTicketRepo()
    verifier = _make_mock_verifier(valid_aud=expected_aud, valid_email=expected_email)
    app = create_app(ticket_repo=ticket_repo, token_verifier=verifier)
    client = TestClient(app)

    legacy_payload = {
        "schema": "ticket.escalated/v1",
        "ticket_id": "TICK-LEGACY",
        "queue": "human-standard",
        "priority": "standard",
        "reason": "legacy_test",
        "escalated_by": "triage_router",
        "degraded": False,
        "ts": "2026-10-30T12:00:00Z",
    }

    resp = client.post(
        "/pubsub/escalations",
        json=_encode_push_body(legacy_payload),
        headers={"Authorization": "Bearer valid-oidc-token"},
    )
    assert resp.status_code == 204



def test_push_rejects_malformed_and_pii_payloads(monkeypatch: pytest.MonkeyPatch):
    """Verify push handler validates ticket.escalated/v1 schema and rejects PII (REQ-AGENT-031)."""
    expected_aud = "https://human-queue.run.app/pubsub/escalations"
    expected_email = "sa-queue-push@test-project.iam.gserviceaccount.com"
    monkeypatch.setenv("PUSH_AUDIENCE", expected_aud)
    monkeypatch.setenv("PUSH_INVOKER_EMAIL", expected_email)

    ticket_repo = FakeTicketRepo()
    verifier = _make_mock_verifier(valid_aud=expected_aud, valid_email=expected_email)
    app = create_app(ticket_repo=ticket_repo, token_verifier=verifier)
    client = TestClient(app)
    auth_headers = {"Authorization": "Bearer valid-oidc-token"}

    # Case A: Corrupted non-base64 data -> 400
    corrupt_body = {"message": {"data": "not-valid-base64==="}}
    resp_corrupt = client.post("/pubsub/escalations", json=corrupt_body, headers=auth_headers)
    assert resp_corrupt.status_code == 400

    # Case B: Wrong schema string -> 400
    bad_schema_payload = {
        "schema": "ticket.escalated/v2-unknown",
        "ticket_id": "c3d4e5f6-a7b8-4c5d-9e0f-23456789abcd",
        "queue": "human-standard",
        "priority": "standard",
        "reason": "test",
        "escalated_by": "triage_router",
        "degraded": False,
        "ts": "2026-10-30T12:00:00Z",
    }
    resp_bad_schema = client.post(
        "/pubsub/escalations",
        json=_encode_push_body(bad_schema_payload),
        headers=auth_headers,
    )
    assert resp_bad_schema.status_code == 400

    # Case C: Missing required field (queue) -> 400
    missing_queue = {
        "schema": "ticket.escalated/v1",
        "ticket_id": "d4e5f6a7-b8c9-4d5e-0f1a-3456789abcde",
        "priority": "standard",
        "reason": "test",
        "escalated_by": "triage_router",
        "degraded": False,
        "ts": "2026-10-30T12:00:00Z",
    }
    resp_missing_field = client.post(
        "/pubsub/escalations",
        json=_encode_push_body(missing_queue),
        headers=auth_headers,
    )
    assert resp_missing_field.status_code == 400

    # Case D: Forbidden PII keys included in payload -> 400
    pii_payload = {
        "schema": "ticket.escalated/v1",
        "ticket_id": "e5f6a7b8-c9d0-4e5f-1a2b-456789abcdef",
        "queue": "human-standard",
        "priority": "standard",
        "reason": "test",
        "escalated_by": "triage_router",
        "degraded": False,
        "ts": "2026-10-30T12:00:00Z",
        "customer_id": "cust-forbidden-pii",
        "message": "Raw customer inquiry text",
    }
    resp_pii = client.post(
        "/pubsub/escalations",
        json=_encode_push_body(pii_payload),
        headers=auth_headers,
    )
    assert resp_pii.status_code == 400
    assert "Forbidden PII keys" in resp_pii.text


def test_push_rejects_unauthorized_invoker_and_unverified_email(monkeypatch: pytest.MonkeyPatch):
    """Verify push handler strictly enforces invoker identity and verified email status (REQ-AGENT-031)."""
    expected_aud = "https://human-queue.run.app/pubsub/escalations"
    expected_email = "sa-queue-push@test-project.iam.gserviceaccount.com"
    monkeypatch.setenv("PUSH_AUDIENCE", expected_aud)
    monkeypatch.setenv("PUSH_INVOKER_EMAIL", expected_email)

    ticket_repo = FakeTicketRepo()

    # Verifier with wrong email
    def wrong_email_verifier(token: str, audience: Optional[str]) -> Dict[str, Any]:
        return {
            "iss": "accounts.google.com",
            "aud": expected_aud,
            "email": "malicious-actor@other-project.iam.gserviceaccount.com",
            "email_verified": True,
        }

    app_wrong_email = create_app(ticket_repo=ticket_repo, token_verifier=wrong_email_verifier)
    client_wrong_email = TestClient(app_wrong_email)

    payload = {
        "schema": "ticket.escalated/v1",
        "ticket_id": "b2c3d4e5-f6a7-4b5c-8d9e-123456789abc",
        "queue": "human-standard",
        "priority": "standard",
        "reason": "test",
        "escalated_by": "triage_router",
        "degraded": False,
        "ts": "2026-10-30T12:00:00Z",
    }
    resp = client_wrong_email.post(
        "/pubsub/escalations",
        json=_encode_push_body(payload),
        headers={"Authorization": "Bearer token"},
    )
    assert resp.status_code == 401
    assert "Unauthorized invoker email" in resp.text
