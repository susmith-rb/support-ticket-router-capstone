"""Tests for redaction map 30-day TTL purge endpoint.

# REQ-STORE-010: Redaction Map 30-Day TTL Purge
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ticket_router.agents.human_queue.purge import (
    router as purge_router,
)
from ticket_router.agents.human_queue.purge import (
    set_token_verifier,
)
from ticket_router.store.fake import FakeTicketRepo
from ticket_router.store.models import RedactionMap, TicketCreate


@pytest.fixture(autouse=True)
def reset_verifier():
    yield
    set_token_verifier(None)


# REQ-STORE-010
def test_purge_requires_scheduler_oidc_and_deletes_expired(monkeypatch):
    """Verify purge endpoint requires valid Cloud Scheduler OIDC token and deletes expired maps.

    Acceptance test for REQ-STORE-010:
    1. Unauthorized callers (missing, invalid token, audience/email mismatch) -> HTTP 401.
    2. Authorized Cloud Scheduler SA -> HTTP 200, deletes expired rows only.
    3. Idempotent repeat calls delete 0 rows.
    """
    repo = FakeTicketRepo()
    now = datetime.now(timezone.utc)

    # 1. Seed tickets and redaction maps
    t1 = repo.create_ticket(TicketCreate(customer_id="cust-1", body_redacted="Ticket 1"))
    t2 = repo.create_ticket(TicketCreate(customer_id="cust-2", body_redacted="Ticket 2"))

    # Map 1: Expired (35 days ago)
    repo.save_redaction_map(
        RedactionMap(
            map_id=uuid4(),
            ticket_id=t1.ticket_id,
            customer_id="cust-1",
            encrypted_mappings=b"expired-payload",
            created_at=now - timedelta(days=35),
            expires_at=now - timedelta(days=5),
        )
    )

    # Map 2: Active (expires in 25 days)
    repo.save_redaction_map(
        RedactionMap(
            map_id=uuid4(),
            ticket_id=t2.ticket_id,
            customer_id="cust-2",
            encrypted_mappings=b"active-payload",
            created_at=now - timedelta(days=5),
            expires_at=now + timedelta(days=25),
        )
    )

    # App setup
    app = FastAPI()
    app.state.ticket_repo = repo
    app.include_router(purge_router)
    client = TestClient(app)

    expected_aud = "https://human-queue-xxx.a.run.app/internal/purge-expired-redaction-maps"
    scheduler_sa = "sa-redaction-purge@susmith-fde-ticket-router.iam.gserviceaccount.com"

    monkeypatch.setenv("PURGE_AUDIENCE", expected_aud)
    monkeypatch.setenv("PURGE_INVOKER_EMAIL", scheduler_sa)

    endpoint = "/internal/purge-expired-redaction-maps"

    # 2. Rejection Cases -> 401

    # Case A: Missing Authorization header
    resp = client.post(endpoint)
    assert resp.status_code == 401
    assert "Authorization header" in resp.json()["detail"]

    # Case B: Non-Bearer scheme
    resp = client.post(endpoint, headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert resp.status_code == 401

    # Case C: Invalid / untrusted token
    def mock_invalid_verifier(tok, aud, email):
        raise ValueError("Invalid JWT signature")

    set_token_verifier(mock_invalid_verifier)
    resp = client.post(endpoint, headers={"Authorization": "Bearer bad-token"})
    assert resp.status_code == 401
    assert "Unauthorized" in resp.json()["detail"]

    # Case D: Audience mismatch
    def mock_aud_mismatch_verifier(tok, aud, email):
        if aud != "https://wrong-audience.com":
            raise ValueError(f"Audience mismatch: expected {aud}")
        return {"email": scheduler_sa, "email_verified": True}

    set_token_verifier(mock_aud_mismatch_verifier)
    resp = client.post(endpoint, headers={"Authorization": "Bearer token-wrong-aud"})
    assert resp.status_code == 401

    # Case E: Unverified email
    def mock_unverified_email(tok, aud, email):
        return {"email": scheduler_sa, "email_verified": False}

    set_token_verifier(mock_unverified_email)
    resp = client.post(endpoint, headers={"Authorization": "Bearer token-unverified"})
    assert resp.status_code == 401

    # Case F: Caller email mismatch (not the scheduler SA)
    def mock_wrong_caller(tok, aud, email):
        raise ValueError(f"Caller email evil@example.com does not match {email}")

    set_token_verifier(mock_wrong_caller)
    resp = client.post(endpoint, headers={"Authorization": "Bearer token-evil-caller"})
    assert resp.status_code == 401

    # 3. Successful Purge Case -> 200
    def mock_valid_scheduler_token(tok, aud, email):
        assert aud == expected_aud
        assert email == scheduler_sa
        return {"email": scheduler_sa, "email_verified": True}

    set_token_verifier(mock_valid_scheduler_token)
    resp = client.post(endpoint, headers={"Authorization": "Bearer valid-scheduler-token"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] == 1

    # Verify repository state: expired map purged, active map retained
    assert repo.get_redaction_map(t1.ticket_id) is None
    active_map = repo.get_redaction_map(t2.ticket_id)
    assert active_map is not None
    assert active_map.encrypted_mappings == b"active-payload"

    # 4. Idempotency: Second call deletes 0 rows
    resp2 = client.post(endpoint, headers={"Authorization": "Bearer valid-scheduler-token"})
    assert resp2.status_code == 200
    assert resp2.json()["deleted"] == 0
