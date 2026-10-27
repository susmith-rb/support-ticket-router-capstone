"""Agent-to-Agent (A2A) handoff protocol and client implementations.

# REQ-AGENT-006: Inter-Agent A2A Handoff Protocol
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

from ticket_router.common.auth import get_access_token


@dataclass(frozen=True)
class A2AHandoffPayload:
    """Inter-agent handoff payload sent from TriageRouter to FaqResolver (REQ-AGENT-006)."""
    ticket_id: str
    session_id: str
    predicted_intent: str
    redacted_message: str
    memory_summary: str = ""


@runtime_checkable
class A2AClientProtocol(Protocol):
    """Protocol for Agent-to-Agent (A2A) handoffs."""

    def handoff(self, payload: A2AHandoffPayload) -> Dict[str, Any]:
        """Dispatch handoff payload to target agent. Returns response dict."""
        ...


class InMemoryA2AClient:
    """Hermetic in-memory A2A client for unit and integration testing."""

    def __init__(self, response_generator: Optional[Callable[[A2AHandoffPayload], Dict[str, Any]]] = None) -> None:
        self.handoffs: list[A2AHandoffPayload] = []
        self._generator = response_generator

    def handoff(self, payload: A2AHandoffPayload) -> Dict[str, Any]:
        self.handoffs.append(payload)
        if self._generator is not None:
            return self._generator(payload)
        return {
            "status": "ACCEPTED",
            "ticket_id": payload.ticket_id,
            "session_id": payload.session_id,
            "target": "faq_resolver",
            "predicted_intent": payload.predicted_intent,
        }


class HttpA2AClient:
    """HTTP client dispatching A2A requests over Agent Gateway with per-hop OIDC token."""

    def __init__(
        self,
        endpoint_url: str,
        target_audience: str = "sa-faq@susmith-fde-ticket-router.iam.gserviceaccount.com",
        timeout_s: float = 5.0,
        token_provider: Optional[Callable[[str], Optional[str]]] = None,
        session: Optional[Any] = None,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.target_audience = target_audience
        self.timeout_s = timeout_s
        self.token_provider = token_provider or (lambda aud: get_access_token())
        self._session = session

    def handoff(self, payload: A2AHandoffPayload) -> Dict[str, Any]:
        url = f"{self.endpoint_url.rstrip('/')}/a2a/handoff"
        headers = {"Content-Type": "application/json"}
        token = self.token_provider(self.target_audience)
        if token:
            headers["Authorization"] = f"Bearer {token}"

        data_bytes = json.dumps(asdict(payload)).encode("utf-8")

        if self._session is not None:
            resp = self._session.post(url, data=data_bytes, headers=headers, timeout=self.timeout_s)
            resp.raise_for_status()
            return resp.json()

        import requests
        resp = requests.post(url, data=data_bytes, headers=headers, timeout=self.timeout_s)
        resp.raise_for_status()
        return resp.json()
