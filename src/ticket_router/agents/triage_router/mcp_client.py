"""Ticket-DB Model Context Protocol (MCP) client protocol and implementations.

# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Protocol, Tuple, runtime_checkable

import requests
from requests.adapters import HTTPAdapter

from ticket_router.common.idtoken import get_id_token
from ticket_router.common.observability import get_current_traceparent, start_span
from ticket_router.common.resilience import TICKET_DB_MCP_TIMEOUT_S
from ticket_router.store.ids import canonical_ticket_uuid


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

    def save_redaction_map(
        self,
        ticket_id: str,
        customer_id: str,
        envelope_b64: str,
        key_id: str = "kms-envelope-v1",
    ) -> Dict[str, Any]:
        """Store KMS envelope-encrypted redaction map. Returns {saved: True}."""
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

    def resolve_customer(self, issuer: str, subject: str) -> Dict[str, Any]:
        """Resolve IdP identity (issuer, subject) to customer record."""
        ...

    def get_active_ticket_for_session(
        self,
        session_id: str,
        customer_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve active holding ticket for a conversation session (REQ-AGENT-033)."""
        ...

    def append_customer_followup(
        self,
        ticket_id: str,
        body_redacted: str,
        customer_id: Optional[str] = None,
        traceparent: Optional[str] = None,
        message_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append a customer follow-up audit event to an active ticket (REQ-AGENT-033)."""
        ...


class InMemoryTicketDbMcpClient:
    """In-memory hermetic implementation of ticket-db-mcp client for unit testing."""

    def __init__(self) -> None:
        self.tickets: Dict[str, Dict[str, Any]] = {}  # ticket_id -> ticket
        self.by_idempotency_key: Dict[str, str] = {}  # idempotency_key -> ticket_id
        self.events: list[Dict[str, Any]] = []
        self.identities: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.redaction_maps: Dict[str, list[Dict[str, Any]]] = {}

    def register_identity(
        self,
        issuer: str,
        subject: str,
        customer_id: Optional[str],
        status: str = "active",
        linked: bool = True,
    ) -> None:
        """Register mock customer identity for testing."""
        self.identities[(issuer, subject)] = {
            "customer_id": customer_id,
            "status": status,
            "linked": linked,
        }

    def resolve_customer(self, issuer: str, subject: str) -> Dict[str, Any]:
        """Resolve IdP identity to customer record."""
        if (issuer, subject) in self.identities:
            return dict(self.identities[(issuer, subject)])
        return {"customer_id": None, "status": "unlinked", "linked": False}

    def upsert_ticket(
        self,
        idempotency_key: str,
        customer_id: str,
        channel: str,
        body_redacted: str,
        decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        # Idempotency check: if key exists, return existing ticket
        existing_id = self.by_idempotency_key.get(idempotency_key)
        if existing_id is None and idempotency_key in self.tickets:
            existing_id = idempotency_key
        if existing_id is not None:
            ticket = self.tickets[existing_id]
            # Update decision / channel if changed
            ticket["decision"] = decision
            if decision.get("decision") == "escalate":
                ticket["status"] = "OPEN"
                if decision.get("queue"):
                    ticket["assigned_queue"] = decision.get("queue")
            return {"ticket_id": existing_id, "status": ticket["status"], "deduplicated": True}

        ticket_id = canonical_ticket_uuid(idempotency_key)
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

    def save_redaction_map(
        self,
        ticket_id: str,
        customer_id: str,
        envelope_b64: str,
        key_id: str = "kms-envelope-v1",
        alias_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not hasattr(self, "redaction_maps"):
            self.redaction_maps = {}
        entry = {
            "customer_id": customer_id,
            "envelope_b64": envelope_b64,
            "key_id": key_id,
        }
        self.redaction_maps.setdefault(ticket_id, []).append(entry)
        if alias_id and alias_id != ticket_id:
            self.redaction_maps.setdefault(alias_id, []).append(entry)
        return {"saved": True}

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

    def get_active_ticket_for_session(
        self,
        session_id: str,
        customer_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        matching = []
        for ticket in self.tickets.values():
            t_sess = ticket.get("session_id")
            if not t_sess:
                dec = ticket.get("decision")
                if isinstance(dec, dict):
                    t_sess = dec.get("session_id")
            if not t_sess and ":" in ticket.get("idempotency_key", ""):
                t_sess = ticket["idempotency_key"].split(":", 1)[0]

            if t_sess == session_id:
                if (
            customer_id
            and ticket.get("customer_id")
            and ticket.get("customer_id") != customer_id
        ):
                    continue
                matching.append(ticket)

        if not matching:
            return None

        latest = matching[-1]
        st = (latest.get("status") or "").lower()
        if st in ("resolved", "resolved_auto", "closed", "returned_to_automation"):
            return None

        if st in ("escalated", "queued_for_review", "claimed", "assigned", "in_progress", "open"):
            return dict(latest)

        return None

    def append_customer_followup(
        self,
        ticket_id: str,
        body_redacted: str,
        customer_id: Optional[str] = None,
        traceparent: Optional[str] = None,
        message_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        if ticket_id not in self.tickets:
            raise KeyError(f"Ticket {ticket_id} not found")
        ticket = self.tickets[ticket_id]
        if (
            customer_id
            and ticket.get("customer_id")
            and ticket.get("customer_id") != customer_id
        ):
            raise PermissionError(f"Customer mismatch for ticket {ticket_id}")

        msg_hash = (
            message_hash
            or hashlib.sha256(body_redacted.encode("utf-8")).hexdigest()
        )

        for ev in self.events:
            if ev.get("ticket_id") == ticket_id and ev.get("event_type") == "customer_followup":
                p = ev.get("payload", {})
                ev_body = p.get("body_redacted", "")
                ev_hash = hashlib.sha256(ev_body.encode("utf-8")).hexdigest()
                if ev_hash == msg_hash or ev_body == body_redacted:
                    return {
                        "success": True,
                        "event_id": ev.get("event_id", ""),
                        "deduplicated": True,
                    }

        event_id = str(uuid.uuid4())
        event = {
            "event_id": event_id,
            "ticket_id": ticket_id,
            "event_type": "customer_followup",
            "actor": "customer",
            "payload": {
                "body_redacted": body_redacted,
                "traceparent": traceparent,
            },
        }
        self.events.append(event)
        ticket["updated_at"] = datetime.now(timezone.utc).isoformat()
        return {"success": True, "event_id": event_id, "deduplicated": False}


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

        with start_span(f"mcp.ticket_db.{tool_name}", attributes={"mcp.tool": tool_name}) as span:
            if tp := (getattr(span, "traceparent", None) or get_current_traceparent()):
                headers["traceparent"] = tp

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

    def save_redaction_map(
        self,
        ticket_id: str,
        customer_id: str,
        envelope_b64: str,
        key_id: str = "kms-envelope-v1",
    ) -> Dict[str, Any]:
        return self._post_tool(
            "save_redaction_map",
            {
                "ticket_id": ticket_id,
                "customer_id": customer_id,
                "envelope_b64": envelope_b64,
                "key_id": key_id,
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

    def resolve_customer(self, issuer: str, subject: str) -> Dict[str, Any]:
        return self._post_tool("resolve_customer", {"issuer": issuer, "subject": subject})

    def get_active_ticket_for_session(
        self,
        session_id: str,
        customer_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        resp = self._post_tool(
            "get_active_ticket_for_session",
            {"session_id": session_id, "customer_id": customer_id},
        )
        if resp and resp.get("ticket_id"):
            return resp
        return None

    def append_customer_followup(
        self,
        ticket_id: str,
        body_redacted: str,
        customer_id: Optional[str] = None,
        traceparent: Optional[str] = None,
        message_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._post_tool(
            "append_customer_followup",
            {
                "ticket_id": ticket_id,
                "body_redacted": body_redacted,
                "customer_id": customer_id,
                "traceparent": traceparent,
                "message_hash": message_hash,
            },
        )
