"""Unit and integration tests for ticket reopening and return to automation.

# REQ-AGENT-033: Multi-Turn Conversation Holding and Customer Follow-up Attachment
# REQ-AGENT-034: Human Review UI with Cloud Run IAP and Verified Rep Identity
# REQ-AGENT-009: HumanQueue Workflow and Audited PII Re-Identification
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from ticket_router.agents.human_queue.agent import HumanQueueAgent
from ticket_router.agents.human_queue.server import create_app as create_human_queue_app
from ticket_router.agents.review_ui.client import HumanQueueClient
from ticket_router.agents.review_ui.server import (
    _BASE_CSS,
)
from ticket_router.agents.review_ui.server import (
    create_app as create_review_ui_app,
)
from ticket_router.store.fake import FakeTicketRepo
from ticket_router.store.models import TicketCreate


def _make_iap_verifier(valid_tokens: Dict[str, Dict[str, Any]]):
    def verifier(token: str, expected_aud: Optional[str] = None) -> Dict[str, Any]:
        if token in valid_tokens:
            data = valid_tokens[token]
            if expected_aud and data.get("aud") and data.get("aud") != expected_aud:
                raise ValueError(f"Audience mismatch: expected {expected_aud}, got {data.get('aud')}")
            return data
        raise ValueError("Invalid IAP JWT assertion token")

    return verifier


class DirectInMemoryQueueClient(HumanQueueClient):
    """Test client wrapping human-queue FastAPI TestClient in-memory."""

    def __init__(self, hq_client: TestClient, token: str = "valid-sa-review-ui-token") -> None:
        super().__init__(base_url="http://testserver")
        self.hq_client = hq_client
        self.test_token = token

    def _build_headers(self, rep_id: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.test_token}",
            "X-Rep-Id": rep_id,
            "Accept": "application/json",
        }

    def list_queue(self, rep_id: str, queue: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        headers = self._build_headers(rep_id)
        params = {"limit": limit}
        if queue:
            params["queue"] = queue
        resp = self.hq_client.get("/queue", params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def get_ticket(self, ticket_id: str, rep_id: str) -> Dict[str, Any]:
        headers = self._build_headers(rep_id)
        resp = self.hq_client.get(f"/tickets/{ticket_id}", headers=headers)
        resp.raise_for_status()
        return resp.json()

    def claim_ticket(self, ticket_id: str, rep_id: str) -> Dict[str, Any]:
        headers = self._build_headers(rep_id)
        resp = self.hq_client.post("/claim", json={"ticket_id": ticket_id}, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def resolve_ticket(self, ticket_id: str, rep_id: str, resolution_notes: str) -> Dict[str, Any]:
        headers = self._build_headers(rep_id)
        resp = self.hq_client.post(
            "/resolve",
            json={"ticket_id": ticket_id, "resolution_notes": resolution_notes},
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    def reopen_ticket(
        self,
        ticket_id: str,
        rep_id: str,
        reason: str = "",
        target_status: str = "returned_to_automation",
    ) -> Dict[str, Any]:
        headers = self._build_headers(rep_id)
        resp = self.hq_client.post(
            "/reopen",
            json={
                "ticket_id": ticket_id,
                "reason": reason,
                "target_status": target_status,
            },
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


# REQ-AGENT-033 & REQ-AGENT-034
def test_reopen_endpoint_auth_and_validation():
    """Verify POST /reopen security gates, OIDC authentication, and payload validation."""
    repo = FakeTicketRepo()
    ticket = repo.create_ticket(
        TicketCreate(
            customer_id="cust-reopen-1",
            body_redacted="Escalated query for testing reopen",
            assigned_queue="human-standard",
        )
    )

    def hq_verifier(token: str, aud: Optional[str] = None) -> Dict[str, Any]:
        if token == "valid-token":
            return {
                "iss": "https://accounts.google.com",
                "email": "sa-review-ui@susmith-fde-ticket-router.iam.gserviceaccount.com",
                "email_verified": True,
            }
        if token == "unauthorized-sa-token":
            return {
                "iss": "https://accounts.google.com",
                "email": "unauthorized-sa@evil.iam.gserviceaccount.com",
                "email_verified": True,
            }
        raise ValueError("Invalid token")

    app = create_human_queue_app(ticket_repo=repo, token_verifier=hq_verifier)
    client = TestClient(app)

    valid_headers = {
        "Authorization": "Bearer valid-token",
        "X-Rep-Id": "rep-alice@gcp.altostrat.com",
    }

    # 1. Missing Authorization header -> 401
    resp_no_auth = client.post(
        "/reopen",
        json={"ticket_id": str(ticket.ticket_id), "target_status": "returned_to_automation"},
        headers={"X-Rep-Id": "rep-alice@gcp.altostrat.com"},
    )
    assert resp_no_auth.status_code == 401
    assert "Authorization header" in resp_no_auth.json()["detail"]

    # 2. Empty Bearer token -> 401
    resp_empty_token = client.post(
        "/reopen",
        json={"ticket_id": str(ticket.ticket_id)},
        headers={"Authorization": "Bearer ", "X-Rep-Id": "rep-alice@gcp.altostrat.com"},
    )
    assert resp_empty_token.status_code == 401

    # 3. Unauthorized caller service account -> 403
    resp_unauth_sa = client.post(
        "/reopen",
        json={"ticket_id": str(ticket.ticket_id), "target_status": "returned_to_automation"},
        headers={
            "Authorization": "Bearer unauthorized-sa-token",
            "X-Rep-Id": "rep-alice@gcp.altostrat.com",
        },
    )
    assert resp_unauth_sa.status_code == 403
    assert "not authorized to act as review UI" in resp_unauth_sa.json()["detail"]

    # 4. Missing X-Rep-Id header -> 400
    resp_no_rep = client.post(
        "/reopen",
        json={"ticket_id": str(ticket.ticket_id), "target_status": "returned_to_automation"},
        headers={"Authorization": "Bearer valid-token"},
    )
    assert resp_no_rep.status_code == 400
    assert "Missing required 'X-Rep-Id' header" in resp_no_rep.json()["detail"]

    # 5. Caller-supplied operator_id or agent_id in request body -> 400 (anti-spoofing)
    resp_body_operator = client.post(
        "/reopen",
        json={
            "ticket_id": str(ticket.ticket_id),
            "operator_id": "malicious-operator",
            "target_status": "returned_to_automation",
        },
        headers=valid_headers,
    )
    assert resp_body_operator.status_code == 400
    assert "Caller-supplied operator_id/agent_id is forbidden" in resp_body_operator.json()["detail"]

    resp_body_agent = client.post(
        "/reopen",
        json={
            "ticket_id": str(ticket.ticket_id),
            "agent_id": "malicious-agent",
            "target_status": "returned_to_automation",
        },
        headers=valid_headers,
    )
    assert resp_body_agent.status_code == 400

    # 6. Invalid target_status -> 400
    resp_invalid_status = client.post(
        "/reopen",
        json={"ticket_id": str(ticket.ticket_id), "target_status": "invalid_status"},
        headers=valid_headers,
    )
    assert resp_invalid_status.status_code == 400
    assert "Invalid target_status" in resp_invalid_status.json()["detail"]

    # 7. Non-existent ticket UUID -> 404
    missing_id = str(uuid4())
    resp_missing_uuid = client.post(
        "/reopen",
        json={"ticket_id": missing_id, "target_status": "returned_to_automation"},
        headers=valid_headers,
    )
    assert resp_missing_uuid.status_code == 404
    assert resp_missing_uuid.json()["detail"] == "Ticket not found"

    # 8. Malformed non-UUID ticket_id -> 404
    resp_malformed_id = client.post(
        "/reopen",
        json={"ticket_id": "not-a-valid-uuid", "target_status": "returned_to_automation"},
        headers=valid_headers,
    )
    assert resp_malformed_id.status_code == 404
    assert resp_malformed_id.json()["detail"] == "Ticket not found"

    # 9. Successful reopen to returned_to_automation -> 200
    resp_ok = client.post(
        "/reopen",
        json={
            "ticket_id": str(ticket.ticket_id),
            "reason": "Customer answered via FAQ bot",
            "target_status": "returned_to_automation",
        },
        headers=valid_headers,
    )
    assert resp_ok.status_code == 200
    body = resp_ok.json()
    assert body["status"] == "RETURNED_TO_AUTOMATION"
    assert body["ticket_id"] == str(ticket.ticket_id)

    # 10. Successful reopen back to open -> 200
    resp_ok_open = client.post(
        "/reopen",
        json={
            "ticket_id": str(ticket.ticket_id),
            "reason": "Customer requires further human follow-up",
            "target_status": "open",
        },
        headers=valid_headers,
    )
    assert resp_ok_open.status_code == 200
    body_open = resp_ok_open.json()
    assert body_open["status"] == "OPEN"
    assert body_open["ticket_id"] == str(ticket.ticket_id)


# REQ-AGENT-033
def test_end_to_end_holding_release_to_automation():
    """Verify that returning ticket to automation releases multi-turn conversation holding."""
    repo = FakeTicketRepo()
    agent = HumanQueueAgent(ticket_repo=repo)

    # 1. Escalate ticket for a conversation session
    session_id = "sess-multi-turn-001"
    ticket = repo.create_ticket(
        TicketCreate(
            customer_id="cust-hold-001",
            body_redacted="Help, my order [ORDER_1] is missing from porch!",
            assigned_queue="human-standard",
            session_id=session_id,
        )
    )

    # Ingress state: ticket is open in human queue -> holds the session
    active_before = repo.get_active_ticket_for_session(session_id)
    assert active_before is not None
    assert active_before.ticket_id == ticket.ticket_id
    assert active_before.status == "open"

    # 2. Operator claims the ticket -> status becomes assigned -> still holds session
    claim_res = agent.claim_ticket(ticket_id=ticket.ticket_id, operator_id="rep-alice@gcp.altostrat.com")
    assert claim_res.get("success") is True

    active_claimed = repo.get_active_ticket_for_session(session_id)
    assert active_claimed is not None
    assert active_claimed.status == "assigned"

    # 3. Operator returns ticket to automation
    reopen_res = agent.reopen_ticket(
        ticket_id=ticket.ticket_id,
        operator_id="rep-alice@gcp.altostrat.com",
        reason="Customer self-serve intent identified; return to triage bot.",
        target_status="returned_to_automation",
    )
    assert reopen_res["status"] == "RETURNED_TO_AUTOMATION"
    assert reopen_res["ticket_id"] == str(ticket.ticket_id)

    # Verify ticket status in repository
    updated_ticket = repo.get_ticket(ticket.ticket_id)
    assert updated_ticket is not None
    assert updated_ticket.status == "returned_to_automation"
    assert updated_ticket.resolution_notes == "Customer self-serve intent identified; return to triage bot."

    # Verify session is RELEASED: subsequent messages in this session route to automation!
    active_after = repo.get_active_ticket_for_session(session_id)
    assert active_after is None, "Conversation holding must be released when status is returned_to_automation!"

    # Verify audit event in ticket_events
    events = repo.list_events(ticket.ticket_id)
    reopen_events = [e for e in events if e.event_type == "returned_to_automation"]
    assert len(reopen_events) == 1
    event = reopen_events[0]
    assert event.actor == "rep-alice@gcp.altostrat.com"
    assert event.payload == {
        "reason": "Customer self-serve intent identified; return to triage bot.",
        "rep_id": "rep-alice@gcp.altostrat.com",
        "target_status": "returned_to_automation",
    }
    # Zero PII in event payload
    assert "cust-hold-001" not in str(event.payload)


# REQ-AGENT-033
def test_reopen_resolved_ticket_back_to_human_queue():
    """Verify that reopening a resolved ticket back to open re-enters queue and re-engages holding."""
    repo = FakeTicketRepo()
    agent = HumanQueueAgent(ticket_repo=repo)

    session_id = "sess-reopen-human-002"
    ticket = repo.create_ticket(
        TicketCreate(
            customer_id="cust-reopen-002",
            body_redacted="I need a replacement for defective item [ORDER_2]",
            assigned_queue="human-urgent",
            session_id=session_id,
        )
    )

    # Claim and resolve ticket
    agent.claim_ticket(ticket.ticket_id, "rep-bob@gcp.altostrat.com")
    agent.resolve_ticket(
        ticket_id=ticket.ticket_id,
        operator_id="rep-bob@gcp.altostrat.com",
        resolution_notes="Issued refund to customer.",
    )

    resolved_ticket = repo.get_ticket(ticket.ticket_id)
    assert resolved_ticket is not None
    assert resolved_ticket.status == "resolved"

    # Ticket is not in open queue
    open_tickets = agent.list_open_queue(limit=50)
    assert not any(t.ticket_id == ticket.ticket_id for t in open_tickets)

    # Resolved ticket does not hold conversation
    assert repo.get_active_ticket_for_session(session_id) is None

    # Reopen ticket back to human queue
    reopen_res = agent.reopen_ticket(
        ticket_id=ticket.ticket_id,
        operator_id="rep-bob@gcp.altostrat.com",
        reason="Customer stated replacement was never received.",
        target_status="open",
    )
    assert reopen_res["status"] == "OPEN"

    # Ticket re-enters open queue
    open_tickets_after = agent.list_open_queue(limit=50)
    assert any(t.ticket_id == ticket.ticket_id for t in open_tickets_after)

    # Ticket re-engages conversation holding because status='open' and assigned_queue='human-urgent'
    holding_ticket = repo.get_active_ticket_for_session(session_id)
    assert holding_ticket is not None
    assert holding_ticket.ticket_id == ticket.ticket_id
    assert holding_ticket.status == "open"

    # Verify audit event in ticket_events
    events = repo.list_events(ticket.ticket_id)
    reopened_events = [e for e in events if e.event_type == "reopened"]
    assert len(reopened_events) == 1
    event = reopened_events[0]
    assert event.actor == "rep-bob@gcp.altostrat.com"
    assert event.payload == {
        "reason": "Customer stated replacement was never received.",
        "rep_id": "rep-bob@gcp.altostrat.com",
        "target_status": "open",
    }


# REQ-AGENT-033
def test_agent_reopen_direct_validation():
    """Verify validation edge cases for HumanQueueAgent.reopen_ticket."""
    repo = FakeTicketRepo()
    agent = HumanQueueAgent(ticket_repo=repo)

    # Invalid target_status raises ValueError
    with pytest.raises(ValueError, match="Invalid target_status"):
        agent.reopen_ticket(
            ticket_id=str(uuid4()),
            operator_id="rep@example.com",
            target_status="closed",
        )

    # Invalid UUID string returns not_found
    res_bad_uuid = agent.reopen_ticket(
        ticket_id="bad-uuid",
        operator_id="rep@example.com",
        target_status="open",
    )
    assert res_bad_uuid == {"status": "not_found"}

    # Missing UUID returns not_found
    res_missing = agent.reopen_ticket(
        ticket_id=uuid4(),
        operator_id="rep@example.com",
        target_status="open",
    )
    assert res_missing == {"status": "not_found"}


# REQ-AGENT-034
def test_human_queue_client_reopen_ticket():
    """Verify HumanQueueClient.reopen_ticket builds correct request headers, URL, and payload."""
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": "RETURNED_TO_AUTOMATION", "ticket_id": "test-uuid"}
    mock_session.post.return_value = mock_resp

    client = HumanQueueClient(
        base_url="https://human-queue-service.internal",
        token_provider=lambda aud: "mock-sa-id-token",
        session=mock_session,
    )

    result = client.reopen_ticket(
        ticket_id="test-uuid",
        rep_id="rep-charlie@gcp.altostrat.com",
        reason="Returned to bot",
        target_status="returned_to_automation",
    )

    assert result["status"] == "RETURNED_TO_AUTOMATION"
    mock_session.post.assert_called_once()
    call_args, call_kwargs = mock_session.post.call_args
    assert call_args[0] == "https://human-queue-service.internal/reopen"
    assert call_kwargs["headers"]["X-Rep-Id"] == "rep-charlie@gcp.altostrat.com"
    assert call_kwargs["headers"]["Authorization"] == "Bearer mock-sa-id-token"
    assert "operator_id" not in call_kwargs["json"]
    assert "agent_id" not in call_kwargs["json"]
    assert call_kwargs["json"] == {
        "ticket_id": "test-uuid",
        "reason": "Returned to bot",
        "target_status": "returned_to_automation",
    }


# REQ-AGENT-034
def test_review_ui_rendering_reopen_controls_and_post_actions():
    """Verify Review UI renders reopen buttons and handles POST /tickets/{id}/reopen."""
    repo = FakeTicketRepo()

    ticket = repo.create_ticket(
        TicketCreate(
            customer_id="cust-ui-001",
            body_redacted="Review UI reopen integration test message",
            assigned_queue="human-standard",
        )
    )

    def hq_verifier(token: str, aud: Optional[str] = None) -> Dict[str, Any]:
        if token == "valid-sa-review-ui-token":
            return {
                "iss": "https://accounts.google.com",
                "email": "sa-review-ui@susmith-fde-ticket-router.iam.gserviceaccount.com",
                "email_verified": True,
            }
        raise ValueError("Invalid SA token")

    hq_app = create_human_queue_app(ticket_repo=repo, token_verifier=hq_verifier)
    hq_test_client = TestClient(hq_app)
    ui_queue_client = DirectInMemoryQueueClient(hq_test_client)

    valid_tokens = {
        "alice-jwt": {
            "iss": "https://cloud.google.com/iap",
            "email": "rep-alice@gcp.altostrat.com",
            "aud": "aud-review-ui",
        },
    }
    verifier = _make_iap_verifier(valid_tokens)

    ui_app = create_review_ui_app(
        queue_client=ui_queue_client,
        iap_verifier=verifier,
        expected_audience="aud-review-ui",
    )
    ui_client = TestClient(ui_app)

    headers = {"x-goog-iap-jwt-assertion": "alice-jwt"}
    t_url = f"/tickets/{ticket.ticket_id}"

    # 1. Unclaimed ticket: Claim Ticket button is visible, reopen buttons NOT visible
    resp_unclaimed = ui_client.get(t_url, headers=headers)
    assert resp_unclaimed.status_code == 200
    assert "Claim Ticket" in resp_unclaimed.text
    assert "Return to Automation (Reopen Bot)" not in resp_unclaimed.text
    assert "Reopen to Human Queue" not in resp_unclaimed.text

    # 2. Claim ticket as Alice
    claim_resp = ui_client.post(f"{t_url}/claim", headers=headers, follow_redirects=True)
    assert claim_resp.status_code == 200
    # Now claimed by Alice: Restore PII, Resolve Ticket, and Return to Automation are visible
    assert "Restore PII (Audited)" in claim_resp.text
    assert "Resolve Ticket" in claim_resp.text
    assert "Return to Automation (Reopen Bot)" in claim_resp.text
    assert f'action="/tickets/{ticket.ticket_id}/reopen"' in claim_resp.text
    assert 'value="returned_to_automation"' in claim_resp.text

    # 3. Post to /tickets/{id}/reopen with target_status="returned_to_automation"
    reopen_post_resp = ui_client.post(
        f"{t_url}/reopen",
        headers=headers,
        data={
            "target_status": "returned_to_automation",
            "reason": "Handed back to triage router bot.",
        },
        follow_redirects=False,
    )
    # Redirects 303 to ticket detail
    assert reopen_post_resp.status_code == 303
    assert reopen_post_resp.headers["location"] == t_url

    # Check ticket state in repo
    t_after_return = repo.get_ticket(ticket.ticket_id)
    assert t_after_return is not None
    assert t_after_return.status == "returned_to_automation"

    # 4. View ticket in returned_to_automation state
    resp_returned = ui_client.get(t_url, headers=headers)
    assert resp_returned.status_code == 200
    assert "Ticket is returned to automation" in resp_returned.text
    assert "Reopen to Human Queue" in resp_returned.text
    assert 'value="open"' in resp_returned.text

    # 5. Post to /tickets/{id}/reopen with target_status="open" (reopen to human queue)
    reopen_open_resp = ui_client.post(
        f"{t_url}/reopen",
        headers=headers,
        data={
            "target_status": "open",
            "reason": "Customer needs human support again.",
        },
        follow_redirects=False,
    )
    assert reopen_open_resp.status_code == 303
    assert reopen_open_resp.headers["location"] == t_url

    # Check ticket status is back to open
    t_after_reopen = repo.get_ticket(ticket.ticket_id)
    assert t_after_reopen is not None
    assert t_after_reopen.status == "open"

    # 6. Resolve the ticket
    resolve_resp = ui_client.post(
        f"{t_url}/resolve",
        headers=headers,
        data={"resolution_notes": "Completed manual investigation."},
        follow_redirects=False,
    )
    assert resolve_resp.status_code == 303

    # 7. View resolved ticket: banner + "Reopen to Human Queue" button
    resp_resolved = ui_client.get(t_url, headers=headers)
    assert resp_resolved.status_code == 200
    assert "Ticket is resolved." in resp_resolved.text
    assert "Reopen to Human Queue" in resp_resolved.text

    # 8. Error handling: if client.reopen_ticket raises an exception -> redirects with error_msg
    error_client = MagicMock(spec=HumanQueueClient)
    error_client.reopen_ticket.side_effect = RuntimeError("Service unavailable")
    error_app = create_review_ui_app(
        queue_client=error_client,
        iap_verifier=verifier,
        expected_audience="aud-review-ui",
    )
    error_ui = TestClient(error_app)
    err_resp = error_ui.post(
        f"{t_url}/reopen",
        headers=headers,
        data={"target_status": "open"},
        follow_redirects=False,
    )
    assert err_resp.status_code == 303
    assert err_resp.headers["location"] == f"{t_url}?error_msg=Failed+to+reopen+ticket"


def test_badge_styling_in_base_css():
    """Verify .badge-returned_to_automation styling exists in _BASE_CSS."""
    assert ".badge-returned_to_automation" in _BASE_CSS
