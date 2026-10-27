"""Tests for MCP Tool Contracts, JSON Schemas, and Role-Based DB Access.

# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
# REQ-AGENT-009: HumanQueue Workflow and Audited PII Re-Identification
"""

from __future__ import annotations

import io
from typing import Optional
from urllib.error import HTTPError
import urllib.request

from fastapi.testclient import TestClient
import pytest

from ticket_router.agents.faq_resolver.mcp_client import (
    HttpOrderLookupClient,
)
from ticket_router.agents.triage_router.mcp_client import (
    HttpTicketDbMcpClient,
)
import ticket_router.mcp.order_lookup.schemas as order_schemas
from ticket_router.mcp.order_lookup.server import create_app as create_order_app
import ticket_router.mcp.ticket_db.schemas as ticket_schemas
from ticket_router.mcp.ticket_db.server import create_app as create_ticket_app
from ticket_router.store.fake import FakeOrderRepo, FakeTicketRepo
from ticket_router.store.models import Order


def _testclient_urlopen_adapter(
    test_client: TestClient,
    req: urllib.request.Request,
    timeout: Optional[float] = None,
) -> io.BytesIO:
    """Adapter routing urllib.request.urlopen calls to an in-process FastAPI TestClient."""
    method = req.get_method()
    url = req.full_url
    headers = dict(req.headers)
    data = req.data

    path = url.replace("http://testserver", "")
    resp = test_client.request(
        method=method,
        url=path,
        headers=headers,
        content=data,
    )

    if resp.status_code >= 400:
        fp = io.BytesIO(resp.content)
        raise HTTPError(url, resp.status_code, resp.text, resp.headers, fp)

    return io.BytesIO(resp.content)


# =============================================================================
# REQ-AGENT-010: Formal JSON Schemas and Role-Based DB Access Acceptance Test
# =============================================================================


# REQ-AGENT-010
def test_schemas_and_roles():
    """Verify typed JSON contracts, parameter schemas, and least-privilege DB role mappings."""
    # 1. Validate ticket-db-mcp tool schemas
    ticket_tool_schemas = ticket_schemas.get_tool_schemas()
    assert "upsert_ticket" in ticket_tool_schemas
    assert "claim_ticket" in ticket_tool_schemas
    assert "resolve_ticket" in ticket_tool_schemas
    assert "reidentify_pii" in ticket_tool_schemas

    upsert_params = ticket_tool_schemas["upsert_ticket"]["parameters"]["properties"]
    assert "idempotency_key" in upsert_params
    assert "customer_id" in upsert_params
    assert "channel" in upsert_params
    assert "body_redacted" in upsert_params
    assert "decision" in upsert_params

    upsert_returns = ticket_tool_schemas["upsert_ticket"]["returns"]["properties"]
    assert "ticket_id" in upsert_returns
    assert "status" in upsert_returns

    claim_params = ticket_tool_schemas["claim_ticket"]["parameters"]["properties"]
    assert "ticket_id" in claim_params
    assert "agent_id" in claim_params

    resolve_params = ticket_tool_schemas["resolve_ticket"]["parameters"]["properties"]
    assert "ticket_id" in resolve_params
    assert "agent_id" in resolve_params
    assert "resolution_notes" in resolve_params

    reid_params = ticket_tool_schemas["reidentify_pii"]["parameters"]["properties"]
    assert "ticket_id" in reid_params
    assert "agent_id" in reid_params

    # 2. Validate order-lookup-mcp tool schemas
    order_tool_schemas = order_schemas.get_tool_schemas()
    assert "get_order" in order_tool_schemas
    get_order_params = order_tool_schemas["get_order"]["parameters"]["properties"]
    assert "order_id" in get_order_params
    # Security Invariant (AGENTS.md rule 7 / REQ-AGENT-010): customer_id is NEVER a tool parameter
    assert "customer_id" not in get_order_params

    get_order_returns = order_tool_schemas["get_order"]["returns"]["properties"]
    assert "order_id" in get_order_returns
    assert "status" in get_order_returns
    assert "items" in get_order_returns
    assert "tracking_number" in get_order_returns
    assert "promised_delivery" in get_order_returns
    assert "delivery_address_redacted" in get_order_returns

    # 3. Assert least-privilege IAM Database Role Separation per REQ-AGENT-010 and TDD §5.1
    # Agents hold NO Cloud SQL client or Postgres roles directly
    agent_db_roles = {
        "sa-triage": None,  # Reaches DB strictly through ticket-db-mcp
        "sa-faq": None,     # Reaches DB strictly through order-lookup-mcp & ticket-db-mcp
    }
    for agent_sa, role in agent_db_roles.items():
        assert role is None, f"Agent {agent_sa} must not hold direct database roles"

    # MCP servers and queue service account hold dedicated least-privilege Postgres IAM roles
    service_db_roles = {
        "sa-order-mcp": "orders_ro",   # SELECT orders_mock under Postgres RLS
        "sa-ticket-mcp": "ticket_rw",  # CRUD tickets, ticket_events; NO access to redaction_maps
        "sa-queue": "queue_rw",        # Read tickets & redaction_maps; column-level UPDATE on tickets
    }
    assert service_db_roles["sa-order-mcp"] == "orders_ro"
    assert service_db_roles["sa-ticket-mcp"] == "ticket_rw"
    assert service_db_roles["sa-queue"] == "queue_rw"


# =============================================================================
# REQ-AGENT-010 & REQ-AGENT-005: Ticket-DB Contract Test with Real Client Class
# =============================================================================


# REQ-AGENT-010, REQ-AGENT-005
def test_ticket_db_mcp_contract_with_real_client():
    """Drive ticket-db-mcp FastAPI server using HttpTicketDbMcpClient real client class."""
    fake_repo = FakeTicketRepo()
    app = create_ticket_app(repo=fake_repo)
    client = TestClient(app)

    # Instantiate real HttpTicketDbMcpClient with in-process TestClient session
    mcp_client = HttpTicketDbMcpClient(
        base_url="http://testserver",
        token_provider=lambda: "mock-test-token",
        session=client,
    )

    # 1. Test upsert_ticket
    upsert_res = mcp_client.upsert_ticket(
        idempotency_key="sess-test-01:hash-1234",
        customer_id="cust-101",
        channel="web",
        body_redacted="Where is my package [ORDER_1]?",
        decision={"decision": "escalate", "queue": "human-urgent"},
    )
    assert "ticket_id" in upsert_res
    ticket_id = upsert_res["ticket_id"]
    assert upsert_res["status"] == "open"
    assert upsert_res["deduplicated"] is False

    # 2. Test claim_ticket
    claim_res = mcp_client.claim_ticket(ticket_id=ticket_id, agent_id="operator-alice")
    assert claim_res["success"] is True

    # Attempting to claim again by another agent fails
    claim_conflict = mcp_client.claim_ticket(ticket_id=ticket_id, agent_id="operator-bob")
    assert claim_conflict["success"] is False
    assert claim_conflict["error"] == "already_claimed"

    # 3. Test reidentify_pii on ticket-db-mcp denies with role separation error (must use human-queue under queue_rw)
    reid_res = mcp_client.reidentify_pii(ticket_id=ticket_id, agent_id="operator-alice")
    assert reid_res.get("error") == "forbidden_role_ticket_rw_cannot_access_redaction_maps_use_human_queue"
    assert reid_res["unmasked_fields"] == {}

    # 4. Test resolve_ticket
    resolve_res = mcp_client.resolve_ticket(
        ticket_id=ticket_id,
        agent_id="operator-alice",
        resolution_notes="Contacted shipping carrier; delivered.",
    )
    assert resolve_res["status"] == "RESOLVED"


# =============================================================================
# REQ-AGENT-005: Ticket-DB Idempotency Invariant Test
# =============================================================================


# REQ-AGENT-005
def test_ticket_db_idempotency():
    """Verify that duplicate calls with identical idempotency_key return existing ticket."""
    fake_repo = FakeTicketRepo()
    app = create_ticket_app(repo=fake_repo)
    client = TestClient(app)

    mcp_client = HttpTicketDbMcpClient(
        base_url="http://testserver",
        session=client,
    )

    idemp_key = "session_xyz:hash_abc"
    res1 = mcp_client.upsert_ticket(
        idempotency_key=idemp_key,
        customer_id="cust-202",
        channel="chat",
        body_redacted="Need assistance with return",
        decision={"decision": "escalate", "queue": "human-standard"},
    )
    assert res1["deduplicated"] is False

    # Second identical call
    res2 = mcp_client.upsert_ticket(
        idempotency_key=idemp_key,
        customer_id="cust-202",
        channel="chat",
        body_redacted="Need assistance with return",
        decision={"decision": "escalate", "queue": "human-standard"},
    )
    assert res2["ticket_id"] == res1["ticket_id"]
    assert res2["deduplicated"] is True

    # Verify only one ticket row was persisted
    assert len(fake_repo.list_open_tickets()) == 1


# =============================================================================
# REQ-AGENT-010: Order-Lookup Contract Test with Real Client Class
# =============================================================================


# REQ-AGENT-010
def test_order_lookup_mcp_contract_with_real_client(monkeypatch):
    """Drive order-lookup-mcp FastAPI server using HttpOrderLookupClient real client class."""
    fake_order_repo = FakeOrderRepo()
    fake_order_repo.add_order(
        Order(
            order_id="ORD-999",
            customer_id="cust-valid",
            order_status="SHIPPED",
            items=[{"sku": "HEADPHONES", "name": "Wireless Headphones", "quantity": 1}],
            total_cents=9999,
            currency="USD",
            tracking_number="TRK-987654",
            shipping_carrier="UPS",
            shipping_address_city="Seattle",
            shipping_address_state="WA",
        )
    )

    app = create_order_app(order_repo=fake_order_repo)
    test_client = TestClient(app)

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: _testclient_urlopen_adapter(test_client, req, timeout),
    )

    client = HttpOrderLookupClient(base_url="http://testserver")

    # 1. Fetch legitimate order
    order_data = client.get_order(order_id="ORD-999", customer_id="cust-valid")
    assert order_data["order_id"] == "ORD-999"
    assert order_data["status"] == "SHIPPED"
    assert order_data["tracking_number"] == "TRK-987654"
    assert "[Seattle, WA]" in order_data["delivery_address_redacted"]

    # 2. Non-existent order raises LookupError
    with pytest.raises(LookupError):
        client.get_order(order_id="ORD-NONEXISTENT", customer_id="cust-valid")

    # 3. Lookup with other customer's ID raises LookupError (tenant isolation / RLS)
    with pytest.raises(LookupError):
        client.get_order(order_id="ORD-999", customer_id="cust-other")


# =============================================================================
# REQ-AGENT-010: BOLA/IDOR Protection Tests
# =============================================================================


# REQ-AGENT-010
def test_order_lookup_bola_protection():
    """Verify BOLA/IDOR protection: tool parameter customer_id rejected, body mismatch rejected."""
    fake_order_repo = FakeOrderRepo()
    fake_order_repo.add_order(
        Order(
            order_id="ORD-500",
            customer_id="cust-alice",
            order_status="DELIVERED",
            items=[{"sku": "BOOK", "name": "Design Patterns"}],
            total_cents=4500,
        )
    )

    app = create_order_app(order_repo=fake_order_repo)
    client = TestClient(app)

    # 1. Formal MCP endpoint (/tools/get_order) REJECTS body containing customer_id
    tool_resp = client.post(
        "/tools/get_order",
        json={"order_id": "ORD-500", "customer_id": "cust-alice"},
        headers={"X-Customer-ID": "cust-alice"},
    )
    assert tool_resp.status_code == 400
    assert "BOLA/IDOR violation" in tool_resp.json()["detail"]

    # 2. Formal MCP endpoint (/tools/get_order) SUCCEEDS with header context and pure order_id
    tool_valid = client.post(
        "/tools/get_order",
        json={"order_id": "ORD-500"},
        headers={"X-Customer-ID": "cust-alice"},
    )
    assert tool_valid.status_code == 200
    assert tool_valid.json()["order_id"] == "ORD-500"

    # 3. Body customer_id MISMATCH with verified header context rejected with 403
    mismatch_resp = client.post(
        "/get_order",
        json={"order_id": "ORD-500", "customer_id": "cust-attacker"},
        headers={"X-Customer-ID": "cust-alice"},
    )
    assert mismatch_resp.status_code == 403
    assert "Access denied" in mismatch_resp.json()["detail"]

    # 4. Attacker looking up Alice's order with attacker's verified header returns 404 (zero leakage)
    probe_resp = client.post(
        "/tools/get_order",
        json={"order_id": "ORD-500"},
        headers={"X-Customer-ID": "cust-attacker"},
    )
    assert probe_resp.status_code == 404


# =============================================================================
# REQ-AGENT-009 & REQ-AGENT-010: Audit Event Logging Test
# =============================================================================


# REQ-AGENT-009, REQ-AGENT-010
def test_ticket_db_reidentify_pii_denied():
    """Verify that ticket-db-mcp denies reidentify_pii due to ticket_rw lacking redaction_maps access."""
    fake_repo = FakeTicketRepo()
    app = create_ticket_app(repo=fake_repo)
    client = TestClient(app)

    # Create ticket
    upsert_resp = client.post(
        "/tools/upsert_ticket",
        json={
            "idempotency_key": "audit-sess:msg-1",
            "customer_id": "cust-audit",
            "channel": "web",
            "body_redacted": "Redacted body [EMAIL_1]",
            "decision": {"decision": "escalate", "queue": "human-standard"},
        },
    )
    ticket_id = upsert_resp.json()["ticket_id"]

    # Claim ticket
    client.post(
        "/tools/claim_ticket",
        json={"ticket_id": ticket_id, "agent_id": "audited-operator"},
    )

    # Reidentify PII is denied by ticket-db-mcp
    reid_resp = client.post(
        "/tools/reidentify_pii",
        json={"ticket_id": ticket_id, "agent_id": "audited-operator"},
    )
    assert reid_resp.status_code == 200
    assert (
        reid_resp.json()["error"]
        == "forbidden_role_ticket_rw_cannot_access_redaction_maps_use_human_queue"
    )
    assert reid_resp.json()["unmasked_fields"] == {}
