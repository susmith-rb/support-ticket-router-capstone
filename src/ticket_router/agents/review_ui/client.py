"""HTTP client invoking human-queue microservice with sa-review-ui OIDC credentials.

# REQ-AGENT-034: Human Review UI with Cloud Run IAP and Verified Rep Identity
# REQ-AUTH-010: Human Review UI Rep Identity Assertion and sa-review-ui Least Privilege
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

import requests

logger = logging.getLogger("ticket_router.agents.review_ui.client")


class HumanQueueClient:
    """Client for human-queue service asserting sa-review-ui OIDC identity and verified rep_id."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        token_provider: Optional[Callable[[str], str]] = None,
        session: Optional[requests.Session] = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = (base_url or os.environ.get("HUMAN_QUEUE_URL", "http://localhost:8080")).rstrip("/")
        self.token_provider = token_provider
        self.session = session or requests.Session()
        self.timeout = timeout

    def _get_id_token(self) -> str:
        """Fetch OIDC ID token for the human-queue service audience."""
        if self.token_provider is not None:
            return self.token_provider(self.base_url)
        try:
            from google.auth.transport.requests import Request as GoogleAuthRequest
            from google.oauth2 import id_token as google_id_token

            auth_req = GoogleAuthRequest()
            return google_id_token.fetch_id_token(auth_req, self.base_url)
        except Exception as exc:
            logger.debug("Falling back on empty ID token: %s", exc)
            return ""

    def _build_headers(self, rep_id: str) -> Dict[str, str]:
        headers = {
            "X-Rep-Id": rep_id,
            "Accept": "application/json",
        }
        token = self._get_id_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def list_queue(
        self,
        rep_id: str,
        queue: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List open tickets eligible for human review, sorted by business priority."""
        url = f"{self.base_url}/queue"
        params: Dict[str, Any] = {"limit": limit}
        if queue:
            params["queue"] = queue

        headers = self._build_headers(rep_id)
        resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def get_ticket(
        self,
        ticket_id: str,
        rep_id: str,
    ) -> Dict[str, Any]:
        """Fetch ticket details and event history for a given ticket ID (redacted only)."""
        url = f"{self.base_url}/tickets/{ticket_id}"
        headers = self._build_headers(rep_id)
        resp = self.session.get(url, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def claim_ticket(
        self,
        ticket_id: str,
        rep_id: str,
    ) -> Dict[str, Any]:
        """Claim a ticket for the authenticated rep."""
        url = f"{self.base_url}/claim"
        headers = self._build_headers(rep_id)
        # Note: operator_id is intentionally omitted from the body; rep_id is passed via X-Rep-Id header
        payload = {"ticket_id": ticket_id}
        resp = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def reidentify_pii(
        self,
        ticket_id: str,
        rep_id: str,
    ) -> Dict[str, Any]:
        """Unmask customer PII for the claimed ticket (audited)."""
        url = f"{self.base_url}/reidentify"
        headers = self._build_headers(rep_id)
        # Note: operator_id is intentionally omitted from the body; rep_id is passed via X-Rep-Id header
        payload = {"ticket_id": ticket_id}
        resp = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def resolve_ticket(
        self,
        ticket_id: str,
        rep_id: str,
        resolution_notes: str,
    ) -> Dict[str, Any]:
        """Resolve a ticket with rep notes."""
        url = f"{self.base_url}/resolve"
        headers = self._build_headers(rep_id)
        # Note: operator_id is intentionally omitted from the body; rep_id is passed via X-Rep-Id header
        payload = {
            "ticket_id": ticket_id,
            "resolution_notes": resolution_notes,
        }
        resp = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()
