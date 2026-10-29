"""Agent-to-Agent (A2A) handoff protocol and client implementations.

# REQ-AGENT-006: Inter-Agent A2A Handoff Protocol
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

import requests
from requests.adapters import HTTPAdapter

from ticket_router.common.idtoken import get_id_token
from ticket_router.common.observability import get_current_traceparent, start_span


def _create_pooled_session() -> requests.Session:
    """Create thread-safe requests session with keep-alive connection pooling."""
    sess = requests.Session()
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


@dataclass(frozen=True)
class A2AHandoffPayload:
    """Inter-agent handoff payload sent from TriageRouter to FaqResolver (REQ-AGENT-006)."""
    ticket_id: str
    session_id: str
    predicted_intent: str
    redacted_message: str
    memory_summary: str = ""
    customer_id: Optional[str] = None
    customer_verified: bool = False
    identity_issuer: Optional[str] = None
    traceparent: Optional[str] = None


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
    """HTTP client dispatching A2A requests with per-hop OIDC ID token.

    Uses pooled keep-alive HTTP connections via requests.Session.
    """

    def __init__(
        self,
        endpoint_url: str,
        target_audience: Optional[str] = None,
        timeout_s: float = 20.0,
        token_provider: Optional[Callable[[str], Optional[str]]] = None,
        session: Optional[Any] = None,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.target_audience = target_audience or endpoint_url
        self.timeout_s = timeout_s
        self.token_provider = token_provider or get_id_token
        self.session = session if session is not None else _create_pooled_session()

    def handoff(self, payload: A2AHandoffPayload) -> Dict[str, Any]:
        clean_endpoint = self.endpoint_url.rstrip("/")
        if clean_endpoint.endswith(("/a2a/handoff", "/a2a", "/resolve")):
            url = clean_endpoint
        else:
            url = f"{clean_endpoint}/a2a/handoff"

        headers = {"Content-Type": "application/json"}
        token = None
        try:
            token = self.token_provider(self.target_audience)
        except Exception:
            token = None
        if token:
            headers["Authorization"] = f"Bearer {token}"

        with start_span(
            "a2a.handoff.faq_resolver",
            attributes={
                "ticket_id": payload.ticket_id,
                "predicted_intent": payload.predicted_intent,
            },
        ) as span:
            outbound_tp = getattr(span, "traceparent", None) or payload.traceparent or get_current_traceparent()
            payload_dict = asdict(payload)
            if outbound_tp:
                headers["traceparent"] = outbound_tp
                payload_dict["traceparent"] = outbound_tp

            data_bytes = json.dumps(payload_dict).encode("utf-8")
            resp = self.session.post(url, data=data_bytes, headers=headers, timeout=self.timeout_s)
            resp.raise_for_status()
            return resp.json()
