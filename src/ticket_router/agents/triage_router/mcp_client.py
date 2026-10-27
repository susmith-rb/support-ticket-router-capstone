"""Ticket-DB Model Context Protocol (MCP) client protocol and implementations.

# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

import requests
from requests.adapters import HTTPAdapter

from ticket_router.common.idtoken import get_id_token
from ticket_router.common.resilience import TICKET_DB_MCP_TIMEOUT_S


def _create_pooled_session() -> requests.Session:
    """Create thread-safe requests session with keep-alive connection pooling."""
    sess = requests.Session()
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


def compute_idempotency_key(session_id: str, body_redacted: str) -> str:
    """Generate deterministic idempotency key from session_id:message_hash (REQ-AGENT-005)."""
    msg_hash = hashlib.sha256(body_redacted.encode("utf-8")).hexdigest()
    return f"{session_id}:{msg_hash}"


@runtime_checkable
class TicketDbMcpClientProtocol(Protocol):
    """Formal protocol definition for ticket-db-mcp tool contracts (REQ-AGENT-010)."""

    def upsert_ticket(
        self,
        idempotency_key: str,
        customer_id: str,
        channel: str,
        body_redacted: str,
        decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Upsert ticket state idempotently. Returns {ticket_id, status}."""
        ...

    def claim_ticket(self, ticket_id: str, agent_id: str) -> Dict[str, Any]:
        """Claim ticket for human operator review. Returns {success}."""
        ...

    def resolve_ticket(
        self,
        ticket_id: str,
        agent_id: str,
        resolution_notes: str,
    ) -> Dict[str, Any]:
        """Mark ticket as resolved. Returns {status}."""
        ...

    def reidentify_pii(self, ticket_id: str, agent_id: str) -> Dict[str, Any]:
        """Audited customer PII unmasking for claimed ticket. Returns {unmasked_fields}."""
        ...


class InMemoryTicketDbMcpClient:
    """In-memory hermetic implementation of ticket-db-mcp client for unit testing."""

    def __init__(self) -> None:
        self.tickets: Dict[str, Dict[str, Any]] = {}  # ticket_id -> ticket
        self.by_idempotency_key: Dict[str, str] = {}  # idempotency_key -> ticket_id
        self.events: list[Dict[str, Any]] = []

    def upsert_ticket(
        self,
        idempotency_key: str,
        customer_id: str,
        channel: str,
        body_redacted: str,
        decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        # Idempotency check: if key exists, return existing ticket
        if idempotency_key in self.by_idempotency_key:
            existing_id = self.by_idempotency_key[idempotency_key]
            ticket = self.tickets[existing_id]
            # Update decision / channel if changed
            ticket["decision"] = decision
            return {"ticket_id": existing_id, "status": ticket["status"], "deduplicated": True}

        ticket_id = f"t_{uuid.uuid4().hex[:12]}"
        status = "OPEN" if decision.get("decision") == "escalate" else "RESOLVED_AUTO"

        record = {
            "ticket_id": ticket_id,
            "idempotency_key": idempotency_key,
            "customer_id": customer_id,
            "channel": channel,
            "body_redacted": body_redacted,
            "decision": decision,
            "status": status,
            "claimed_by": None,
        }
        self.tickets[ticket_id] = record
        self.by_idempotency_key[idempotency_key] = ticket_id
        return {"ticket_id": ticket_id, "status": status, "deduplicated": False}

    def claim_ticket(self, ticket_id: str, agent_id: str) -> Dict[str, Any]:
        if ticket_id not in self.tickets:
            return {"success": False, "error": "not_found"}
        ticket = self.tickets[ticket_id]
        if ticket["claimed_by"] and ticket["claimed_by"] != agent_id:
            return {"success": False, "error": "already_claimed"}
        ticket["claimed_by"] = agent_id
        ticket["status"] = "IN_PROGRESS"
        return {"success": True}

    def resolve_ticket(
        self,
        ticket_id: str,
        agent_id: str,
        resolution_notes: str,
    ) -> Dict[str, Any]:
        if ticket_id not in self.tickets:
            return {"status": "not_found"}
        ticket = self.tickets[ticket_id]
        ticket["status"] = "RESOLVED"
        ticket["resolution_notes"] = resolution_notes
        return {"status": "RESOLVED"}

    def reidentify_pii(self, ticket_id: str, agent_id: str) -> Dict[str, Any]:
        if ticket_id not in self.tickets:
            return {"unmasked_fields": {}, "error": "not_found"}
        ticket = self.tickets[ticket_id]
        if ticket["claimed_by"] != agent_id:
            return {"unmasked_fields": {}, "error": "unauthorized_not_claimed"}
        return {"unmasked_fields": {"email": "customer@example.com", "phone": "+15551234567"}}


class HttpTicketDbMcpClient:
    """HTTP client calling ticket-db-mcp server on Cloud Run (REQ-AGENT-010)."""

    def __init__(
        self,
        base_url: str,
        timeout_s: float = TICKET_DB_MCP_TIMEOUT_S,
        token_provider: Optional[Callable[[str], Optional[str]]] = None,
        session: Optional[Any] = None,
        audience: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.audience = audience or self.base_url
        self.timeout_s = timeout_s
        self.token_provider = token_provider or get_id_token
        self.session = session if session is not None else _create_pooled_session()

    def _post_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/tools/{tool_name}"
        headers = {"Content-Type": "application/json"}
        token = None
        try:
            token = self.token_provider(self.audience)
        except TypeError:
            token = self.token_provider()
        except Exception:
            token = None
        if token:
            headers["Authorization"] = f"Bearer {token}"

        data_bytes = json.dumps(arguments).encode("utf-8")
        if hasattr(self.session, "app"):
            resp = self.session.post(url, content=data_bytes, headers=headers, timeout=self.timeout_s)
        else:
            resp = self.session.post(url, data=data_bytes, headers=headers, timeout=self.timeout_s)
        resp.raise_for_status()
        return resp.json()

    def upsert_ticket(
        self,
        idempotency_key: str,
        customer_id: str,
        channel: str,
        body_redacted: str,
        decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._post_tool(
            "upsert_ticket",
            {
                "idempotency_key": idempotency_key,
                "customer_id": customer_id,
                "channel": channel,
                "body_redacted": body_redacted,
                "decision": decision,
            },
        )

    def claim_ticket(self, ticket_id: str, agent_id: str) -> Dict[str, Any]:
        return self._post_tool("claim_ticket", {"ticket_id": ticket_id, "agent_id": agent_id})

    def resolve_ticket(
        self,
        ticket_id: str,
        agent_id: str,
        resolution_notes: str,
    ) -> Dict[str, Any]:
        return self._post_tool(
            "resolve_ticket",
            {
                "ticket_id": ticket_id,
                "agent_id": agent_id,
                "resolution_notes": resolution_notes,
            },
        )

    def reidentify_pii(self, ticket_id: str, agent_id: str) -> Dict[str, Any]:
        return self._post_tool("reidentify_pii", {"ticket_id": ticket_id, "agent_id": agent_id})
