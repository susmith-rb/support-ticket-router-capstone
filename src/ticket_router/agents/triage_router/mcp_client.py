"""Ticket-DB Model Context Protocol (MCP) client protocol and implementations.

# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Protocol, Tuple, runtime_checkable

import requests
from requests.adapters import HTTPAdapter

from ticket_router.common.idtoken import get_id_token
from ticket_router.common.observability import get_current_traceparent, start_span
from ticket_router.common.resilience import (
    TICKET_DB_HOLD_LOOKUP_TIMEOUT_S,
    TICKET_DB_MCP_TIMEOUT_S,
)
from ticket_router.store.ids import canonical_ticket_uuid

logger = logging.getLogger(__name__)


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

    def update_ticket_status(
        self,
        ticket_id: str,
        from_status: str,
        to_status: str,
        actor: str,
        reason: str,
        idempotency_key: str,
        trace: Optional[str] = None,
        customer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update ticket lifecycle status with deterministic backend validation (REQ-STORE-011, REQ-LIFE-002)."""
        ...

    def get_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve ticket by ID or UUID."""
        ...

    def list_customer_tickets(
        self,
        customer_id: str,
        limit: int = 5,
        customer_verified: bool = True,
        headers: Optional[Dict[str, str]] = None,
    ) -> list[Dict[str, Any]]:
        """List recent tickets for a customer (REQ-HIST-003)."""
        ...

    def record_resolution_confirmation_requested(
        self, ticket_id: str, prompt: Optional[str] = None
    ) -> Dict[str, Any]:
        """Record that an affirmative resolution confirmation prompt was asked for a ticket."""
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
        is_esc = decision.get("decision") == "escalate"
        status = "OPEN" if is_esc else "answered"
        from ticket_router.store.models import effective_category

        assigned_queue = decision.get("queue") if is_esc else None
        category = (
            (decision.get("details", {}) or {}).get("category")
            if isinstance(decision.get("details"), dict)
            else None
        ) or effective_category(decision=decision) or "support"
        raw_q = (assigned_queue or "").strip().lower()
        if raw_q.startswith("human"):
            current_owner = f"queue:{assigned_queue}"
        else:
            current_owner = "bot"
        issue_reference = decision.get("ticket_id") or f"TICK-{str(ticket_id)[:8]}"
        sess_id = idempotency_key.split(":", 1)[0] if ":" in idempotency_key else None

        record = {
            "ticket_id": ticket_id,
            "idempotency_key": idempotency_key,
            "session_id": sess_id,
            "customer_id": customer_id,
            "channel": channel,
            "body_redacted": body_redacted,
            "decision": decision,
            "status": status,
            "category": category,
            "current_owner": current_owner,
            "assigned_queue": assigned_queue,
            "issue_reference": issue_reference,
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

    def _lookup_ticket(self, ticket_id: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        if ticket_id in self.tickets:
            return ticket_id, self.tickets[ticket_id]
        for k, t in self.tickets.items():
            if (
                t.get("ticket_id") == ticket_id
                or str(t.get("id")) == ticket_id
                or (isinstance(t.get("decision"), dict) and t["decision"].get("ticket_id") == ticket_id)
            ):
                return k, t
        return None

    def claim_ticket(self, ticket_id: str, agent_id: str) -> Dict[str, Any]:
        match = self._lookup_ticket(ticket_id)
        if not match:
            return {"success": False, "error": "not_found"}
        _, ticket = match
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
        match = self._lookup_ticket(ticket_id)
        if not match:
            return {"status": "not_found"}
        _, ticket = match
        ticket["status"] = "RESOLVED"
        ticket["resolution_notes"] = resolution_notes
        return {"status": "RESOLVED"}

    def reidentify_pii(self, ticket_id: str, agent_id: str) -> Dict[str, Any]:
        match = self._lookup_ticket(ticket_id)
        if not match:
            return {"unmasked_fields": {}, "error": "not_found"}
        _, ticket = match
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

        for cand in reversed(matching):
            st = (cand.get("status") or "").lower()
            if st in ("resolved", "resolved_auto", "resolved_by_customer", "closed", "returned_to_automation"):
                continue
            cat = cand.get("category")
            if not cat:
                dec = cand.get("decision")
                if isinstance(dec, dict):
                    cat = dec.get("category")
            if cat in ("security", "fraud"):
                continue
            if st in ("escalated", "queued_for_review", "claimed", "assigned", "in_progress", "open", "answered"):
                t = dict(cand)
                actual_tid = cand.get("ticket_id") or cand.get("id")
                t["resolution_confirmation_requested"] = any(
                    ev.get("ticket_id") in (actual_tid, cand.get("ticket_id"))
                    and ev.get("event_type") == "resolution_confirmation_requested"
                    for ev in self.events
                )
                return t

        return None

    def update_ticket_status(
        self,
        ticket_id: str,
        from_status: str,
        to_status: str,
        actor: str,
        reason: str,
        idempotency_key: str,
        trace: Optional[str] = None,
        customer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        match = self._lookup_ticket(ticket_id)
        if not match:
            raise KeyError(f"Ticket {ticket_id} not found")
        actual_tid, ticket = match

        is_customer_update = (customer_id is not None) or (to_status in ("resolved_by_customer", "reopened"))
        if is_customer_update:
            if customer_id is None:
                raise PermissionError("Customer ID is required for customer status updates")
            if str(customer_id).startswith("anon:"):
                raise PermissionError("Anonymous callers cannot transition ticket status")
            if ticket.get("customer_id") != customer_id:
                raise PermissionError(
                    f"Customer mismatch: caller {customer_id} does not own ticket {ticket.get('customer_id')}"
                )
            if actor and actor.startswith(("rep:", "queue:")):
                raise PermissionError("Customer cannot impersonate representative or queue actor")

        if ticket.get("status") == to_status:
            return {"success": True, "ticket_id": ticket_id, "status": to_status, "idempotent": True}

        if idempotency_key:
            for ev in self.events:
                if (
                    ev.get("ticket_id") in (ticket_id, actual_tid)
                    and ev.get("event_type") == "status_changed"
                    and isinstance(ev.get("payload"), dict)
                    and ev["payload"].get("idempotency_key") == idempotency_key
                ):
                    return {
                        "success": True,
                        "ticket_id": ticket_id,
                        "status": ticket.get("status", to_status),
                        "idempotent": True,
                    }

        # Deterministic backend validation for customer transitions
        if is_customer_update:
            from ticket_router.store.models import effective_category, is_human_owned

            cat = effective_category(ticket)
            if cat in ("security", "fraud"):
                raise PermissionError(f"Customer confirmation not allowed on security or fraud tickets: {cat}")

            if is_human_owned(ticket):
                owner = ticket.get("current_owner", "")
                raise PermissionError(f"Customer confirmation not allowed on human-assigned tickets (current_owner={owner})")

            allowed = {
                "open": ["resolved_by_customer"],
                "answered": ["resolved_by_customer"],
                "resolved_by_customer": ["reopened"],
            }.get((ticket.get("status") or "").lower(), [])
            if to_status not in allowed:
                raise ValueError(f"Invalid transition from {ticket.get('status')} to {to_status}")

        if from_status and ticket.get("status") != from_status:
            raise ValueError(f"Status conflict: current status is {ticket.get('status')}, expected {from_status}")

        ticket["status"] = to_status
        ev_id = f"ev-{uuid.uuid4().hex[:12]}"
        self.events.append({
            "event_id": ev_id,
            "ticket_id": ticket_id,
            "event_type": "status_changed",
            "actor": actor,
            "payload": {
                "from": from_status,
                "to": to_status,
                "actor": actor,
                "reason": reason,
                "trace_id": trace,
                "idempotency_key": idempotency_key,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"success": True, "ticket_id": ticket_id, "status": to_status, "idempotent": False}

    def append_customer_followup(
        self,
        ticket_id: str,
        body_redacted: str,
        customer_id: Optional[str] = None,
        traceparent: Optional[str] = None,
        message_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        match = self._lookup_ticket(ticket_id)
        if not match:
            raise KeyError(f"Ticket {ticket_id} not found")
        actual_tid, ticket = match
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
            if ev.get("ticket_id") in (ticket_id, actual_tid) and ev.get("event_type") == "customer_followup":
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

    def get_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        match = self._lookup_ticket(ticket_id)
        if match:
            t = dict(match[1])
            actual_tid = match[0]
            t["resolution_confirmation_requested"] = any(
                ev.get("ticket_id") in (ticket_id, actual_tid)
                and ev.get("event_type") == "resolution_confirmation_requested"
                for ev in self.events
            )
            return t
        return None

    def record_resolution_confirmation_requested(
        self, ticket_id: str, prompt: Optional[str] = None
    ) -> Dict[str, Any]:
        match = self._lookup_ticket(ticket_id)
        actual_tid = match[0] if match else ticket_id
        ev_id = f"ev-{uuid.uuid4().hex[:12]}"
        self.events.append({
            "event_id": ev_id,
            "ticket_id": actual_tid,
            "event_type": "resolution_confirmation_requested",
            "actor": "router",
            "payload": {"prompt": prompt} if prompt else {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"success": True, "ticket_id": ticket_id}

    def list_customer_tickets(
        self,
        customer_id: str,
        limit: int = 5,
        customer_verified: bool = True,
        headers: Optional[Dict[str, str]] = None,
    ) -> list[Dict[str, Any]]:
        """List recent tickets for a customer (REQ-HIST-003)."""
        eff_headers = headers or {}
        header_cust = eff_headers.get("X-Customer-ID") or eff_headers.get("X-Verified-Customer-Id")
        if header_cust:
            if str(header_cust).startswith("anon:"):
                raise PermissionError("Anonymous customers cannot list tickets")
            if customer_id and customer_id != header_cust:
                raise ValueError("customer_id mismatch (BOLA violation)")
            cust_ver = str(eff_headers.get("X-Customer-Verified", "true")).lower()
            if cust_ver != "true":
                raise PermissionError("Unverified customer identity")
        else:
            if not customer_verified:
                raise PermissionError("Unverified customer identity")
            if str(customer_id).startswith("anon:"):
                raise PermissionError("Anonymous customers cannot list tickets")

        matching: list[Dict[str, Any]] = []
        for t in self.tickets.values():
            if t.get("customer_id") == customer_id:
                matching.append(t)

        def _get_sort_key(item: Dict[str, Any]) -> str:
            return str(item.get("created_at") or "")

        matching.sort(key=_get_sort_key, reverse=True)
        results: list[Dict[str, Any]] = []
        for t in matching[:limit]:
            ref = t.get("issue_reference") or f"TICK-{str(t.get('ticket_id'))[:8]}"
            cat = t.get("category", "support")
            st = "under review" if cat in ("security", "fraud") else t.get("status", "open")
            created_at = t.get("created_at") or datetime.now(timezone.utc).isoformat()
            results.append({
                "issue_reference": ref,
                "status": st,
                "created_at": created_at,
                "category": cat,
            })
        return results


class HttpTicketDbMcpClient:
    """HTTP client calling ticket-db-mcp server on Cloud Run (REQ-AGENT-010)."""

    def __init__(
        self,
        base_url: str,
        timeout_s: float = TICKET_DB_MCP_TIMEOUT_S,
        token_provider: Optional[Callable[[str], Optional[str]]] = None,
        session: Optional[Any] = None,
        audience: Optional[str] = None,
        hold_lookup_timeout_s: float = TICKET_DB_HOLD_LOOKUP_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.audience = audience or self.base_url
        self.timeout_s = timeout_s
        self.hold_lookup_timeout_s = hold_lookup_timeout_s
        self.token_provider = token_provider or get_id_token
        self.session = session if session is not None else _create_pooled_session()

    def _post_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        timeout_s: Optional[float] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/tools/{tool_name}"
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        token = None
        try:
            token = self.token_provider(self.audience)
        except TypeError:
            token = self.token_provider()
        except Exception:
            token = None
        if token:
            req_headers["Authorization"] = f"Bearer {token}"

        effective_timeout = timeout_s if timeout_s is not None else self.timeout_s

        with start_span(f"mcp.ticket_db.{tool_name}", attributes={"mcp.tool": tool_name}) as span:
            if tp := (getattr(span, "traceparent", None) or get_current_traceparent()):
                req_headers["traceparent"] = tp

            data_bytes = json.dumps(arguments).encode("utf-8")
            if hasattr(self.session, "app"):
                resp = self.session.post(url, content=data_bytes, headers=req_headers, timeout=effective_timeout)
            else:
                resp = self.session.post(url, data=data_bytes, headers=req_headers, timeout=effective_timeout)
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
        try:
            resp = self._post_tool(
                "get_active_ticket_for_session",
                {"session_id": session_id, "customer_id": customer_id},
                timeout_s=self.hold_lookup_timeout_s,
            )
            if resp and resp.get("ticket_id"):
                return resp
            return None
        except Exception as exc:
            logger.warning(
                json.dumps(
                    {
                        "event": "get_active_ticket_failed_open",
                        "session_id": session_id,
                        "error_type": type(exc).__name__,
                    },
                    separators=(",", ":"),
                )
            )
            return None

    def append_customer_followup(
        self,
        ticket_id: str,
        body_redacted: str,
        customer_id: Optional[str] = None,
        traceparent: Optional[str] = None,
        message_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        req_headers = {}
        if customer_id:
            req_headers["X-Customer-ID"] = customer_id
            req_headers["X-Customer-Verified"] = "false" if str(customer_id).startswith("anon:") else "true"
        return self._post_tool(
            "append_customer_followup",
            {
                "ticket_id": ticket_id,
                "body_redacted": body_redacted,
                "customer_id": customer_id,
                "traceparent": traceparent,
                "message_hash": message_hash,
            },
            headers=req_headers if req_headers else None,
        )

    def update_ticket_status(
        self,
        ticket_id: str,
        from_status: str,
        to_status: str,
        actor: str,
        reason: str,
        idempotency_key: str,
        trace: Optional[str] = None,
        customer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        req_headers = {}
        if customer_id:
            req_headers["X-Customer-ID"] = customer_id
            req_headers["X-Customer-Verified"] = "false" if str(customer_id).startswith("anon:") else "true"
        return self._post_tool(
            "update_ticket_status",
            {
                "ticket_id": ticket_id,
                "from_status": from_status,
                "to_status": to_status,
                "actor": actor,
                "reason": reason,
                "idempotency_key": idempotency_key,
                "trace": trace,
                "customer_id": customer_id,
            },
            headers=req_headers if req_headers else None,
        )

    def record_resolution_confirmation_requested(
        self, ticket_id: str, prompt: Optional[str] = None
    ) -> Dict[str, Any]:
        return self._post_tool(
            "record_resolution_confirmation_requested",
            {"ticket_id": ticket_id, "prompt": prompt},
        )

    def get_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        try:
            resp = self._post_tool(
                "get_ticket",
                {"ticket_id": ticket_id},
                timeout_s=self.hold_lookup_timeout_s,
            )
            if resp and resp.get("ticket_id"):
                return resp
            return None
        except Exception:
            return None

    def list_customer_tickets(
        self,
        customer_id: str,
        limit: int = 5,
        customer_verified: bool = True,
        id_token: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> list[Dict[str, Any]]:
        """Query list_customer_tickets tool over HTTP (REQ-HIST-003)."""
        req_headers: Dict[str, str] = {
            "X-Customer-ID": customer_id,
            "X-Customer-Verified": "true" if customer_verified else "false",
        }
        if id_token:
            req_headers["Authorization"] = f"Bearer {id_token}"
        if headers:
            req_headers.update(headers)

        payload = {"customer_id": customer_id, "limit": limit}
        resp = self._post_tool(
            "list_customer_tickets",
            payload,
            headers=req_headers,
        )
        return resp.get("tickets", [])
