"""Tests for HumanQueueAgent workflow, priority scheduling, and audited PII re-identification.

# REQ-AGENT-009: HumanQueue Workflow and Audited PII Re-Identification
# REQ-AGENT-004: Gaming-Resistant Queue Priority Assignment
# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from ticket_router.agents.human_queue.agent import HumanQueueAgent
from ticket_router.agents.human_queue.server import create_app as create_human_queue_app
from ticket_router.agents.triage_router.mcp_client import InMemoryTicketDbMcpClient
from ticket_router.store.fake import FakeTicketRepo
from ticket_router.store.models import RedactionMap, TicketCreate

# =============================================================================
# REQ-AGENT-009: HumanQueue Acceptance Test
# =============================================================================


# REQ-AGENT-009
def test_queue_claim_and_pii_reid():
    """Verify queue priority listing, atomic claim, restricted PII re-ID, and immutable audit event."""
    repo = FakeTicketRepo()
    now = datetime.now(timezone.utc)

    # 1. Create tickets across priorities with varying timestamps
    t_std_old = repo.create_ticket(
        TicketCreate(
            customer_id="cust-101",
            body_redacted="Old standard request [ORDER_1]",
            assigned_queue="human-standard",
        )
    )
    # Manually adjust created_at to simulate older ticket
    t_std_old.created_at = now - timedelta(hours=2)

    t_urg_new = repo.create_ticket(
        TicketCreate(
            customer_id="cust-102",
            body_redacted="Recent urgent escalation [ORDER_2]",
            assigned_queue="human-urgent",
        )
    )
    t_urg_new.created_at = now - timedelta(minutes=5)
    repo.save_redaction_map(
        RedactionMap(
            map_id=uuid4(),
            ticket_id=t_urg_new.ticket_id,
            customer_id=t_urg_new.customer_id,
            encrypted_mappings=json.dumps({"email": "cust-102@example.com"}).encode("utf-8"),
        )
    )

    t_std_new = repo.create_ticket(
        TicketCreate(
            customer_id="cust-103",
            body_redacted="New standard request",
            assigned_queue="human-standard",
        )
    )
    t_std_new.created_at = now - timedelta(minutes=1)

    agent = HumanQueueAgent(ticket_repo=repo)

    # 2. Priority Ordering Test (REQ-AGENT-004 & REQ-AGENT-009)
    # Urgent (P1) ticket MUST be first despite being newer than old standard ticket
    open_queue = agent.list_open_queue()
    assert len(open_queue) == 3
    assert open_queue[0].ticket_id == t_urg_new.ticket_id
    assert open_queue[1].ticket_id == t_std_old.ticket_id
    assert open_queue[2].ticket_id == t_std_new.ticket_id

    # 3. Queue Depths Test
    depths = agent.queue_depths()
    assert depths["human-urgent"] == 1
    assert depths["human-standard"] == 2

    # 4. Atomic Claim Test (REQ-AGENT-009)
    claim_alice = agent.claim_ticket(t_urg_new.ticket_id, operator_id="operator-alice")
    assert claim_alice["success"] is True

    # Concurrent claim by operator-bob fails (already claimed)
    claim_bob = agent.claim_ticket(t_urg_new.ticket_id, operator_id="operator-bob")
    assert claim_bob["success"] is False
    assert claim_bob["error"] == "already_claimed"

    # 5. PII Re-Identification Authorization Guard (REQ-AGENT-009)
    # Non-claiming operator Bob is rejected with PermissionError
    with pytest.raises(PermissionError) as exc_info:
        agent.reidentify_pii(t_urg_new.ticket_id, operator_id="operator-bob")
    assert "not authorized" in str(exc_info.value)

    # Claiming operator Alice succeeds
    reid_result = agent.reidentify_pii(t_urg_new.ticket_id, operator_id="operator-alice")
    assert "unmasked_fields" in reid_result
    assert "email" in reid_result["unmasked_fields"]
    assert "cust-102@example.com" in reid_result["unmasked_fields"]["email"]

    # 6. Immutable Audit Event Verification (REQ-AGENT-009 & REQ-AGENT-014)
    events = repo.list_events(t_urg_new.ticket_id)
    reid_events = [e for e in events if e.event_type == "pii_reidentified"]
    assert len(reid_events) == 1

    audit_entry = reid_events[0]
    assert audit_entry.actor == "operator-alice"
    assert audit_entry.payload["operator_id"] == "operator-alice"
    assert audit_entry.payload["ticket_id"] == str(t_urg_new.ticket_id)
    assert "email" in audit_entry.payload["fields_unmasked"]
    # PII Integrity: raw email or customer text is NOT in the audit event payload
    assert "cust-102@example.com" not in str(audit_entry.payload)

    # 7. Resolution Test
    res = agent.resolve_ticket(
        ticket_id=t_urg_new.ticket_id,
        operator_id="operator-alice",
        resolution_notes="Contacted customer; replacement dispatched.",
    )
    assert res["status"] == "RESOLVED"

    # Ticket is no longer in open queue
    remaining_open = agent.list_open_queue()
    assert len(remaining_open) == 2
    assert t_urg_new.ticket_id not in [t.ticket_id for t in remaining_open]


# =============================================================================
# HumanQueueAgent with MCP Client Delegation Test
# =============================================================================


# REQ-AGENT-009
def test_human_queue_with_mcp_client_delegation():
    """Verify HumanQueueAgent delegating claim, resolve, and re-ID to TicketDbMcpClient."""
    mcp_client = InMemoryTicketDbMcpClient()
    upsert = mcp_client.upsert_ticket(
        idempotency_key="mcp-sess:msg-1",
        customer_id="cust-mcp",
        channel="web",
        body_redacted="Redacted message",
        decision={"decision": "escalate", "queue": "human-standard"},
    )
    ticket_id = upsert["ticket_id"]

    agent = HumanQueueAgent(mcp_client=mcp_client)

    # Claim via MCP
    claim_res = agent.claim_ticket(ticket_id=ticket_id, operator_id="operator-clara")
    assert claim_res["success"] is True

    # Reidentify PII via MCP
    reid_res = agent.reidentify_pii(ticket_id=ticket_id, operator_id="operator-clara")
    assert "unmasked_fields" in reid_res
    assert reid_res["unmasked_fields"]["email"] == "customer@example.com"

    # Unauthorized reidentify via MCP raises PermissionError
    with pytest.raises(PermissionError):
        agent.reidentify_pii(ticket_id=ticket_id, operator_id="operator-dave")

    # Resolve via MCP
    resolve_res = agent.resolve_ticket(
        ticket_id=ticket_id,
        operator_id="operator-clara",
        resolution_notes="Resolved via MCP",
    )
    assert resolve_res["status"] == "RESOLVED"


# =============================================================================
# HumanQueue Cloud Run HTTP Service Test
# =============================================================================


# REQ-AGENT-001, REQ-AGENT-009
def test_human_queue_cloud_run_service():
    """Verify HumanQueueAgent Cloud Run FastAPI server endpoints."""
    repo = FakeTicketRepo()
    t = repo.create_ticket(
        TicketCreate(
            customer_id="cust-cr",
            body_redacted="Cloud Run service test",
            assigned_queue="human-urgent",
        )
    )
    repo.save_redaction_map(
        RedactionMap(
            map_id=uuid4(),
            ticket_id=t.ticket_id,
            customer_id=t.customer_id,
            encrypted_mappings=json.dumps({"email": "cust-cr@example.com"}).encode("utf-8"),
        )
    )

    def fake_verifier(token: str, aud: Optional[str] = None) -> Dict[str, Any]:
        if token == "valid-ui-token":
            return {
                "iss": "https://accounts.google.com",
                "email": "sa-review-ui@susmith-fde-ticket-router.iam.gserviceaccount.com",
                "email_verified": True,
            }
        if token == "non-ui-token":
            return {
                "iss": "https://accounts.google.com",
                "email": "unauthorized-caller@example.com",
                "email_verified": True,
            }
        raise ValueError("Invalid OIDC token")

    app = create_human_queue_app(ticket_repo=repo, token_verifier=fake_verifier)
    client = TestClient(app)

    ui_headers_eva = {
        "Authorization": "Bearer valid-ui-token",
        "X-Rep-Id": "operator-eva",
    }
    ui_headers_frank = {
        "Authorization": "Bearer valid-ui-token",
        "X-Rep-Id": "operator-frank",
    }

    # 1. Healthz probe
    assert client.get("/healthz").status_code == 200

    # 2. Queue listing
    q_resp = client.get("/queue")
    assert q_resp.status_code == 200
    items = q_resp.json()
    assert len(items) == 1
    assert items[0]["ticket_id"] == str(t.ticket_id)

    # 3. Queue depths
    depths_resp = client.get("/queue/depths")
    assert depths_resp.status_code == 200
    assert depths_resp.json()["human-urgent"] == 1

    # 4. Detail endpoint
    detail_resp = client.get(f"/tickets/{t.ticket_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["ticket"]["ticket_id"] == str(t.ticket_id)

    # 5. Rejection: Body operator_id forbidden (REQ-AGENT-009 / REQ-AGENT-034)
    body_op_resp = client.post(
        "/claim",
        headers=ui_headers_eva,
        json={"ticket_id": str(t.ticket_id), "operator_id": "operator-eva"},
    )
    assert body_op_resp.status_code == 400
    assert "forbidden" in body_op_resp.json()["detail"].lower()

    # 6. Rejection: Non-UI caller rejected with 403
    non_ui_resp = client.post(
        "/claim",
        headers={"Authorization": "Bearer non-ui-token", "X-Rep-Id": "operator-eva"},
        json={"ticket_id": str(t.ticket_id)},
    )
    assert non_ui_resp.status_code == 403

    # 7. Rejection: Missing Authorization header rejected with 401
    no_auth_resp = client.post(
        "/claim",
        headers={"X-Rep-Id": "operator-eva"},
        json={"ticket_id": str(t.ticket_id)},
    )
    assert no_auth_resp.status_code == 401

    # 8. Rejection: Missing X-Rep-Id header rejected with 400
    no_rep_resp = client.post(
        "/claim",
        headers={"Authorization": "Bearer valid-ui-token"},
        json={"ticket_id": str(t.ticket_id)},
    )
    assert no_rep_resp.status_code == 400

    # 9. Successful claim endpoint with X-Rep-Id
    claim_resp = client.post(
        "/claim",
        headers=ui_headers_eva,
        json={"ticket_id": str(t.ticket_id)},
    )
    assert claim_resp.status_code == 200
    assert claim_resp.json()["success"] is True

    # 10. Unauthorized re-identification rejected with 403 (operator-frank did not claim)
    unauth_resp = client.post(
        "/reidentify",
        headers=ui_headers_frank,
        json={"ticket_id": str(t.ticket_id)},
    )
    assert unauth_resp.status_code == 403

    # 11. Authorized re-identification returns 200 and audits rep_id
    auth_resp = client.post(
        "/reidentify",
        headers=ui_headers_eva,
        json={"ticket_id": str(t.ticket_id)},
    )
    assert auth_resp.status_code == 200
    assert "email" in auth_resp.json()["unmasked_fields"]

    # Verify rep_id in audit events
    events = repo.list_events(t.ticket_id)
    reid_ev = next(e for e in events if e.event_type == "pii_reidentified")
    assert reid_ev.actor == "operator-eva"
    assert reid_ev.payload["rep_id"] == "operator-eva"

    # 12. Resolve endpoint
    resolve_resp = client.post(
        "/resolve",
        headers=ui_headers_eva,
        json={
            "ticket_id": str(t.ticket_id),
            "resolution_notes": "All resolved.",
        },
    )
    assert resolve_resp.status_code == 200
    assert resolve_resp.json()["status"] == "RESOLVED"


# REQ-AGENT-009
def test_human_queue_reidentify_pii_from_redaction_map():
    """Verify that HumanQueueAgent reads encrypted_mappings from RedactionMap in repository."""
    import json
    from uuid import uuid4

    from ticket_router.store.models import RedactionMap

    repo = FakeTicketRepo()
    ticket = repo.create_ticket(
        TicketCreate(
            customer_id="cust-999",
            body_redacted="Please help me with [TOKEN_PII]",
            assigned_queue="human-standard",
        )
    )

    # Save real redaction map in repo
    secret_data = {
        "full_name": "Dr. Eleanor Vance",
        "email": "eleanor.vance@hillhouse.example",
        "ssn_last4": "1234",
    }
    redaction_map = RedactionMap(
        map_id=uuid4(),
        ticket_id=ticket.ticket_id,
        customer_id=ticket.customer_id,
        encrypted_mappings=json.dumps(secret_data).encode("utf-8"),
    )
    repo.save_redaction_map(redaction_map)

    agent = HumanQueueAgent(ticket_repo=repo)

    # Operator claims ticket
    claim_res = agent.claim_ticket(ticket.ticket_id, operator_id="operator-theodora")
    assert claim_res["success"] is True

    # Reidentify PII should fetch and parse the repo's RedactionMap
    unmasked = agent.reidentify_pii(ticket.ticket_id, operator_id="operator-theodora")
    assert unmasked["unmasked_fields"]["full_name"] == "Dr. Eleanor Vance"
    assert unmasked["unmasked_fields"]["email"] == "eleanor.vance@hillhouse.example"
    assert unmasked["unmasked_fields"]["ssn_last4"] == "1234"

    # Audit event should list the unmasked field names but NOT the secrets
    events = repo.list_events(ticket.ticket_id)
    reid_events = [e for e in events if e.event_type == "pii_reidentified"]
    assert len(reid_events) == 1
    assert "full_name" in reid_events[0].payload["fields_unmasked"]
    assert "Eleanor" not in str(reid_events[0].payload)
