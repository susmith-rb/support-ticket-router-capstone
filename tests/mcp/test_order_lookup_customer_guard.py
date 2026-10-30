"""Tests for customer identity guard and unverified session rejection on order-lookup-mcp.

# REQ-IDENT-004: Fail-Closed Anonymous Fallback & Authorization Boundaries
# REQ-IDENT-005: Cryptographic Trust Chain Propagation across A2A & MCP
# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ticket_router.mcp.order_lookup.server import create_app
from ticket_router.store.fake import FakeOrderRepo
from ticket_router.store.models import Order


def _build_test_app(require_verified_header: bool = True):
    repo = FakeOrderRepo()
    repo.add_order(
        Order(
            order_id="ORD-1001",
            customer_id="cust_1001",
            order_status="PROCESSING",
            items=[{"sku": "ITEM-1", "name": "Pixel 9 Pro"}],
            total_cents=99900,
        )
    )
    app = create_app(order_repo=repo, require_verified_header=require_verified_header)
    return app


# REQ-IDENT-004, REQ-IDENT-005
def test_order_lookup_verified_customer_success():
    """Verify valid customer with X-Customer-Verified: true succeeds."""
    app = _build_test_app(require_verified_header=True)
    client = TestClient(app)

    headers = {
        "X-Customer-ID": "cust_1001",
        "X-Customer-Verified": "true",
    }
    resp = client.post("/tools/get_order", json={"order_id": "ORD-1001"}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["order_id"] == "ORD-1001"
    assert data["status"] == "PROCESSING"


# REQ-IDENT-004, REQ-IDENT-005
def test_order_lookup_unverified_customer_rejected():
    """Verify requests lacking X-Customer-Verified or with false are rejected with 403 unverified_customer."""
    app = _build_test_app(require_verified_header=True)
    client = TestClient(app)

    # Missing X-Customer-Verified header
    resp_missing = client.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={"X-Customer-ID": "cust_1001"},
    )
    assert resp_missing.status_code == 403
    assert resp_missing.json()["detail"] == "unverified_customer"

    # Explicitly false X-Customer-Verified
    resp_false = client.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001"},
        headers={"X-Customer-ID": "cust_1001", "X-Customer-Verified": "false"},
    )
    assert resp_false.status_code == 403
    assert resp_false.json()["detail"] == "unverified_customer"


# REQ-IDENT-004, REQ-IDENT-005
def test_order_lookup_anonymous_customer_rejected():
    """Verify anonymous customer (anon:*) is rejected with 403 unverified_customer even if verified header is true."""
    app = _build_test_app(require_verified_header=True)
    client = TestClient(app)

    headers = {
        "X-Customer-ID": "anon:session-abcd-1234",
        "X-Customer-Verified": "true",
    }
    resp = client.post("/tools/get_order", json={"order_id": "ORD-1001"}, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "unverified_customer"


# REQ-IDENT-005, REQ-AGENT-010, # REQ-AUTH-003
def test_order_lookup_bola_body_guard():
    """Verify BOLA protection: passing customer_id in tool or HTTP body is rejected with 400 before database lookup."""
    app = _build_test_app(require_verified_header=True)
    client = TestClient(app)

    headers = {
        "X-Customer-ID": "cust_1001",
        "X-Customer-Verified": "true",
    }
    resp = client.post(
        "/tools/get_order",
        json={"order_id": "ORD-1001", "customer_id": "cust_1001"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "BOLA/IDOR violation" in resp.json()["detail"]

    # Also test /tools/list_orders
    resp_list = client.post(
        "/tools/list_orders",
        json={"limit": 5, "customer_id": "cust_1001"},
        headers=headers,
    )
    assert resp_list.status_code == 400
    assert "BOLA/IDOR violation" in resp_list.json()["detail"]
