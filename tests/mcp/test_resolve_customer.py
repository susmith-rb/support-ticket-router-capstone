"""Tests for resolve_customer tool on ticket-db-mcp.

# REQ-IDENT-003: Server-Side JWT Verification & Identity Resolution via ticket-db-mcp
# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ticket_router.mcp.ticket_db.server import create_app
from ticket_router.store.fake import FakeCustomerRepo, FakeTicketRepo
from ticket_router.store.models import Customer, CustomerIdentity


def _build_test_app():
    fake_ticket_repo = FakeTicketRepo()
    fake_customer_repo = FakeCustomerRepo(
        customers=[
            Customer(customer_id="cust_1001", status="active"),
            Customer(customer_id="cust_1002", status="suspended"),
        ],
        identities=[
            CustomerIdentity(
                issuer="https://securetoken.google.com/susmith-fde-ticket-router",
                subject="uid-active-1001",
                customer_id="cust_1001",
                email_verified=True,
            ),
            CustomerIdentity(
                issuer="https://securetoken.google.com/susmith-fde-ticket-router",
                subject="uid-suspended-1002",
                customer_id="cust_1002",
                email_verified=False,
            ),
        ],
    )
    app = create_app(repo=fake_ticket_repo, customer_repo=fake_customer_repo)
    return app, fake_customer_repo


# REQ-IDENT-003, REQ-AGENT-010
def test_resolve_customer_active():
    """Verify active linked customer resolves correctly to internal customer_id with active status."""
    app, customer_repo = _build_test_app()
    client = TestClient(app)

    payload = {
        "issuer": "https://securetoken.google.com/susmith-fde-ticket-router",
        "subject": "uid-active-1001",
    }
    resp = client.post("/tools/resolve_customer", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["customer_id"] == "cust_1001"
    assert data["status"] == "active"
    assert data["linked"] is True

    # Also test non-tools path alias
    resp_alias = client.post("/resolve_customer", json=payload)
    assert resp_alias.status_code == 200
    assert resp_alias.json() == data

    # Verify audit event in identity_events
    events = customer_repo.list_events(customer_id="cust_1001")
    assert any(e.event_type == "resolve_ok" and e.payload.get("subject") == "uid-active-1001" for e in events)


# REQ-IDENT-003, REQ-AGENT-010
def test_resolve_customer_unlinked():
    """Verify unlinked identity returns 200 with linked=False and customer_id=None (never 404)."""
    app, customer_repo = _build_test_app()
    client = TestClient(app)

    payload = {
        "issuer": "https://securetoken.google.com/susmith-fde-ticket-router",
        "subject": "uid-unknown-9999",
    }
    resp = client.post("/tools/resolve_customer", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["customer_id"] is None
    assert data["status"] == "unlinked"
    assert data["linked"] is False

    events = customer_repo.list_events()
    assert any(
        e.event_type == "resolve_unlinked"
        and e.customer_id is None
        and e.payload.get("subject") == "uid-unknown-9999"
        for e in events
    )


# REQ-IDENT-003, REQ-AGENT-010
def test_resolve_customer_suspended():
    """Verify suspended customer returns 200 with linked=True and status='suspended'."""
    app, customer_repo = _build_test_app()
    client = TestClient(app)

    payload = {
        "issuer": "https://securetoken.google.com/susmith-fde-ticket-router",
        "subject": "uid-suspended-1002",
    }
    resp = client.post("/tools/resolve_customer", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["customer_id"] == "cust_1002"
    assert data["status"] == "suspended"
    assert data["linked"] is True

    events = customer_repo.list_events(customer_id="cust_1002")
    assert any(
        e.event_type == "resolve_suspended"
        and e.payload.get("subject") == "uid-suspended-1002"
        for e in events
    )


# REQ-IDENT-003, REQ-AGENT-010
def test_resolve_customer_validation_errors():
    """Verify missing fields or unexpected fields fail with HTTP 422/400 validation error."""
    app, _ = _build_test_app()
    client = TestClient(app)

    # Missing issuer
    resp1 = client.post("/tools/resolve_customer", json={"subject": "sub-1"})
    assert resp1.status_code in (400, 422)

    # Missing subject
    resp2 = client.post("/tools/resolve_customer", json={"issuer": "iss-1"})
    assert resp2.status_code in (400, 422)

    # Extra forbidden field
    resp3 = client.post("/tools/resolve_customer", json={"issuer": "iss-1", "subject": "sub-1", "email": "leak@test.com"})
    assert resp3.status_code in (400, 422)
