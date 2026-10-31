"""Tests for Human Review UI behind Cloud Run IAP with verified rep identity.

# REQ-AGENT-034: Human Review UI with Cloud Run IAP and Verified Rep Identity
# REQ-AUTH-010: Human Review UI Rep Identity Assertion and sa-review-ui Least Privilege
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi.testclient import TestClient

from ticket_router.agents.human_queue.server import create_app as create_human_queue_app
from ticket_router.agents.review_ui.client import HumanQueueClient
from ticket_router.agents.review_ui.server import create_app as create_review_ui_app
from ticket_router.store.fake import FakeTicketRepo
from ticket_router.store.models import RedactionMap, TicketCreate


def make_iap_verifier(valid_tokens: Dict[str, Dict[str, Any]]):
    def verifier(token: str, expected_aud: Optional[str] = None) -> Dict[str, Any]:
        if token in valid_tokens:
            data = valid_tokens[token]
            if expected_aud and data.get("aud") and data.get("aud") != expected_aud:
                raise ValueError(f"Audience mismatch: expected {expected_aud}, got {data.get('aud')}")
            return data
        raise ValueError("Invalid IAP JWT assertion token")

    return verifier


class DirectInMemoryQueueClient(HumanQueueClient):
    """Test client wrapping the human-queue FastAPI TestClient in-memory."""

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

    def reidentify_pii(self, ticket_id: str, rep_id: str) -> Dict[str, Any]:
        headers = self._build_headers(rep_id)
        resp = self.hq_client.post("/reidentify", json={"ticket_id": ticket_id}, headers=headers)
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


# REQ-AGENT-033
def test_review_ui_iap_auth_and_access_control():
    """Verify IAP authentication guard fails closed on missing/invalid JWT and accepts valid JWT."""
    valid_tokens = {
        "valid-rep-jwt": {
            "iss": "https://cloud.google.com/iap",
            "email": "rep-alice@gcp.altostrat.com",
            "aud": "/projects/123/locations/us-central1/services/human-review-ui",
        },
        "bad-iss-jwt": {
            "iss": "https://malicious-issuer.com",
            "email": "attacker@example.com",
            "aud": "/projects/123/locations/us-central1/services/human-review-ui",
        },
        "no-email-jwt": {
            "iss": "https://cloud.google.com/iap",
            "aud": "/projects/123/locations/us-central1/services/human-review-ui",
        },
    }
    verifier = make_iap_verifier(valid_tokens)
    expected_aud = "/projects/123/locations/us-central1/services/human-review-ui"

    app = create_review_ui_app(
        iap_verifier=verifier,
        expected_audience=expected_aud,
    )
    client = TestClient(app)

    # 1. Missing IAP header -> 401
    resp_missing = client.get("/")
    assert resp_missing.status_code == 401
    assert "missing" in resp_missing.json()["detail"].lower()

    # 2. Invalid IAP header -> 401
    resp_invalid = client.get("/", headers={"x-goog-iap-jwt-assertion": "bogus-jwt"})
    assert resp_invalid.status_code == 401

    # 3. Bad issuer -> 401
    resp_bad_iss = client.get("/", headers={"x-goog-iap-jwt-assertion": "bad-iss-jwt"})
    assert resp_bad_iss.status_code == 401

    # 4. Missing email claim -> 401
    resp_no_email = client.get("/", headers={"x-goog-iap-jwt-assertion": "no-email-jwt"})
    assert resp_no_email.status_code == 401

    # 5. Valid JWT -> 200 and rep_id rendered in HTML
    resp_valid = client.get("/", headers={"x-goog-iap-jwt-assertion": "valid-rep-jwt"})
    assert resp_valid.status_code == 200
    assert "rep-alice@gcp.altostrat.com" in resp_valid.text
    assert "Review Queue" in resp_valid.text


# REQ-AGENT-033
def test_review_ui_full_workflow():
    """Verify full human review workflow: listing queue, claim, audited PII re-ID, and resolve."""
    repo = FakeTicketRepo()

    # Seed an urgent ticket with encrypted PII redaction map
    t_urg = repo.create_ticket(
        TicketCreate(
            customer_id="cust-201",
            body_redacted="My package was stolen from porch! [ORDER_99]",
            assigned_queue="human-urgent",
        )
    )
    repo.save_redaction_map(
        RedactionMap(
            map_id=uuid4(),
            ticket_id=t_urg.ticket_id,
            customer_id=t_urg.customer_id,
            encrypted_mappings=json.dumps({"email": "cust-201@victim.com", "phone": "+15550199"}).encode("utf-8"),
        )
    )

    # Configure human-queue app with OIDC verifier accepting sa-review-ui
    def hq_verifier(token: str, aud: Optional[str] = None) -> Dict[str, Any]:
        if token == "valid-sa-review-ui-token":
            return {
                "iss": "https://accounts.google.com",
                "email": "sa-review-ui@susmith-fde-ticket-router.iam.gserviceaccount.com",
                "email_verified": True,
            }
        raise ValueError("Unauthorized SA token")

    hq_app = create_human_queue_app(ticket_repo=repo, token_verifier=hq_verifier)
    hq_test_client = TestClient(hq_app)

    # Connect UI client to in-memory human-queue test server
    ui_queue_client = DirectInMemoryQueueClient(hq_test_client)

    valid_tokens = {
        "alice-jwt": {
            "iss": "https://cloud.google.com/iap",
            "email": "rep-alice@gcp.altostrat.com",
            "aud": "aud-review-ui",
        },
        "bob-jwt": {
            "iss": "https://cloud.google.com/iap",
            "email": "rep-bob@gcp.altostrat.com",
            "aud": "aud-review-ui",
        },
    }
    verifier = make_iap_verifier(valid_tokens)

    ui_app = create_review_ui_app(
        queue_client=ui_queue_client,
        iap_verifier=verifier,
        expected_audience="aud-review-ui",
    )
    ui_client = TestClient(ui_app)

    alice_headers = {"x-goog-iap-jwt-assertion": "alice-jwt"}
    bob_headers = {"x-goog-iap-jwt-assertion": "bob-jwt"}

    # 1. Queue list: Alice views queue, sees redacted body only
    q_resp = ui_client.get("/", headers=alice_headers)
    assert q_resp.status_code == 200
    assert "My package was stolen from porch!" in q_resp.text
    # Zero unmasked PII in queue list
    assert "cust-201@victim.com" not in q_resp.text
    assert "+15550199" not in q_resp.text

    # 2. Ticket detail: Alice opens ticket detail
    t_url = f"/tickets/{t_urg.ticket_id}"
    detail_resp = ui_client.get(t_url, headers=alice_headers)
    assert detail_resp.status_code == 200
    assert "Claim Ticket" in detail_resp.text
    assert "cust-201@victim.com" not in detail_resp.text

    # 3. Claim action: Alice claims ticket
    claim_resp = ui_client.post(f"{t_url}/claim", headers=alice_headers, follow_redirects=True)
    assert claim_resp.status_code == 200
    assert "Restore PII (Audited)" in claim_resp.text
    assert "Resolve Ticket" in claim_resp.text

    # Verify repository state: ticket assigned to rep-alice@gcp.altostrat.com
    claimed_ticket = repo.get_ticket(t_urg.ticket_id)
    assert claimed_ticket is not None
    assert claimed_ticket.assigned_to == "rep-alice@gcp.altostrat.com"

    # 4. Unauthorized unmasking attempt: Bob tries to unmask Alice's ticket -> Denied
    reid_bob = ui_client.post(f"{t_url}/reidentify", headers=bob_headers)
    assert reid_bob.status_code == 200
    assert "Access Denied" in reid_bob.text
    assert "cust-201@victim.com" not in reid_bob.text

    # 5. Authorized unmasking: Alice unmasks PII -> Visible to Alice and audited
    reid_alice = ui_client.post(f"{t_url}/reidentify", headers=alice_headers)
    assert reid_alice.status_code == 200
    assert "Restored Customer PII (Audited)" in reid_alice.text
    assert "cust-201@victim.com" in reid_alice.text
    assert "+15550199" in reid_alice.text

    # Verify immutable audit event in ticket_events
    events = repo.list_events(t_urg.ticket_id)
    reid_events = [e for e in events if e.event_type == "pii_reidentified"]
    assert len(reid_events) == 1
    audit_entry = reid_events[0]
    assert audit_entry.actor == "rep-alice@gcp.altostrat.com"
    assert audit_entry.payload["rep_id"] == "rep-alice@gcp.altostrat.com"
    assert "email" in audit_entry.payload["fields_unmasked"]
    assert "phone" in audit_entry.payload["fields_unmasked"]
    # PII itself is NEVER written to the audit log payload
    assert "cust-201@victim.com" not in str(audit_entry.payload)

    # 6. Resolve action: Alice resolves ticket with resolution notes
    resolve_resp = ui_client.post(
        f"{t_url}/resolve",
        headers=alice_headers,
        data={"resolution_notes": "Issued immediate replacement package via carrier."},
        follow_redirects=True,
    )
    assert resolve_resp.status_code == 200
    assert "Ticket is resolved" in resolve_resp.text

    resolved_ticket = repo.get_ticket(t_urg.ticket_id)
    assert resolved_ticket is not None
    assert resolved_ticket.status == "resolved"
    assert resolved_ticket.resolution_notes == "Issued immediate replacement package via carrier."
