"""In-memory fakes for TicketRepository and OrderRepository.

Implements REQ-STORE-006: Lightweight thread-safe fakes for hermetic unit testing
without live Cloud SQL PostgreSQL dependencies.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from ticket_router.store.interfaces import (
    CustomerRepository,
    OrderRepository,
    TicketRepository,
)
from ticket_router.store.models import (
    CUSTOMER_ALLOWED_TRANSITIONS,
    Customer,
    CustomerIdentity,
    CustomerResolution,
    IdentityEvent,
    Order,
    RedactionMap,
    Ticket,
    TicketCreate,
    TicketEvent,
    effective_category,
    is_holding_ticket,
    is_human_owned,
)


class FakeTicketRepo(TicketRepository):
    """Thread-safe in-memory fake implementing TicketRepository."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: Dict[UUID, Ticket] = {}
        self._events: Dict[UUID, List[TicketEvent]] = {}
        self._redaction_maps: Dict[UUID, List[RedactionMap]] = {}

    def _normalize_uuid(self, ticket_id: UUID | str) -> UUID:
        if isinstance(ticket_id, UUID):
            return ticket_id
        try:
            return UUID(ticket_id)
        except (ValueError, AttributeError):
            return uuid.uuid5(uuid.NAMESPACE_URL, f"idempotency:{ticket_id}")

    def create_ticket(self, ticket_create: TicketCreate) -> Ticket:
        """Create ticket idempotently.

        If ticket with specified ticket_id already exists, returns existing record.
        """
        with self._lock:
            if ticket_create.ticket_id and ticket_create.ticket_id in self._tickets:
                return self._tickets[ticket_create.ticket_id]

            now = datetime.now(timezone.utc)
            t_id = ticket_create.ticket_id or uuid4()
            sess_id = ticket_create.session_id
            if not sess_id and isinstance(ticket_create.decision, dict):
                sess_id = ticket_create.decision.get("session_id")
            issue_ref = ticket_create.issue_reference
            if not issue_ref and isinstance(ticket_create.decision, dict):
                issue_ref = ticket_create.decision.get("ticket_id")
            if not issue_ref:
                issue_ref = f"TICK-{str(t_id)[:8]}"

            assigned_q = (ticket_create.assigned_queue or "").strip()
            if ticket_create.current_owner:
                curr_owner = ticket_create.current_owner
            elif assigned_q.lower().startswith("human"):
                curr_owner = f"queue:{assigned_q}"
            else:
                curr_owner = "bot"
            category = ticket_create.category or "support"

            ticket = Ticket(
                ticket_id=t_id,
                customer_id=ticket_create.customer_id,
                body_redacted=ticket_create.body_redacted,
                channel=ticket_create.channel,
                decision=ticket_create.decision,
                status="open",
                assigned_to=ticket_create.assigned_to,
                assigned_queue=ticket_create.assigned_queue,
                created_at=now,
                updated_at=now,
                session_id=sess_id,
                issue_reference=issue_ref,
                category=category,
                current_owner=curr_owner,
            )
            self._tickets[t_id] = ticket
            self._events[t_id] = [
                TicketEvent(
                    event_id=uuid4(),
                    ticket_id=t_id,
                    event_type="created",
                    actor="router-agent",
                    payload={"channel": ticket_create.channel},
                    created_at=now,
                )
            ]
            return ticket

    def get_ticket(self, ticket_id: UUID | str) -> Optional[Ticket]:
        t_id = self._normalize_uuid(ticket_id)
        with self._lock:
            ticket = self._tickets.get(t_id)
            if ticket is not None:
                return ticket
            if isinstance(ticket_id, str):
                ref_norm = ticket_id.strip().upper()
                for t in self._tickets.values():
                    if t.issue_reference and t.issue_reference.strip().upper() == ref_norm:
                        return t
            return None

    def list_open_tickets(
        self, limit: int = 50, queue: Optional[str] = None
    ) -> List[Ticket]:
        with self._lock:
            res = [
                t
                for t in self._tickets.values()
                if t.status == "open"
                and (queue is None or t.assigned_queue == queue)
            ]
            res.sort(key=lambda x: x.created_at)
            return res[:limit]

    def claim_ticket(self, ticket_id: UUID | str, agent_id: str) -> bool:
        """Atomically claim ticket. Returns False if already claimed or resolved."""
        t_id = self._normalize_uuid(ticket_id)
        with self._lock:
            ticket = self._tickets.get(t_id)
            if ticket is None or ticket.status != "open":
                return False

            now = datetime.now(timezone.utc)
            ticket.status = "assigned"
            ticket.assigned_to = agent_id
            ticket.updated_at = now

            self._events.setdefault(t_id, []).append(
                TicketEvent(
                    event_id=uuid4(),
                    ticket_id=t_id,
                    event_type="claimed",
                    actor=agent_id,
                    payload={"assigned_to": agent_id},
                    created_at=now,
                )
            )
            return True

    def append_event(
        self,
        ticket_id: UUID | str,
        event_type: str,
        actor: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> TicketEvent:
        t_id = self._normalize_uuid(ticket_id)
        with self._lock:
            if t_id not in self._tickets:
                raise KeyError(f"Ticket not found: {t_id}")

            now = datetime.now(timezone.utc)
            event = TicketEvent(
                event_id=uuid4(),
                ticket_id=t_id,
                event_type=event_type,
                actor=actor,
                payload=payload,
                created_at=now,
            )
            self._events.setdefault(t_id, []).append(event)
            return event

    def list_events(self, ticket_id: UUID | str) -> List[TicketEvent]:
        t_id = self._normalize_uuid(ticket_id)
        with self._lock:
            return list(self._events.get(t_id, []))

    def update_ticket_status(
        self,
        ticket_id: UUID | str,
        status: str,
        resolution_notes: Optional[str] = None,
        answer_text: Optional[str] = None,
        citations: Optional[List[Dict[str, Any]]] = None,
        from_status: Optional[str] = None,
        actor: Optional[str] = None,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        trace: Optional[str] = None,
        customer_id: Optional[str] = None,
    ) -> Optional[Ticket]:
        t_id = self._normalize_uuid(ticket_id)
        with self._lock:
            ticket = self._tickets.get(t_id)
            if ticket is None:
                for t in self._tickets.values():
                    if t.issue_reference == str(ticket_id):
                        ticket = t
                        t_id = t.ticket_id
                        break
            if ticket is None:
                raise KeyError(f"Ticket not found: {t_id}")

            # Customer scoping / RLS validation (BEFORE checking target state)
            is_customer_update = (customer_id is not None) or (status in ("resolved_by_customer", "reopened"))
            if is_customer_update:
                if customer_id is None:
                    raise PermissionError("Customer ID is required for customer status updates")
                if str(customer_id).startswith("anon:"):
                    raise PermissionError("Anonymous callers cannot update ticket status")
                if ticket.customer_id != customer_id:
                    raise PermissionError("Customer mismatch: cannot update another customer's ticket")
                if actor and actor.startswith(("rep:", "queue:")):
                    raise PermissionError("Customer cannot impersonate representative or queue actor")

            # Idempotency: if already in target status, return existing ticket (no duplicate event)
            if ticket.status == status:
                return ticket

            # Idempotency key deduplication
            if idempotency_key:
                for ev in self._events.get(t_id, []):
                    if (
                        ev.event_type == "status_changed"
                        and isinstance(ev.payload, dict)
                        and ev.payload.get("idempotency_key") == idempotency_key
                    ):
                        return ticket

            # Deterministic backend validation for customer transitions
            if is_customer_update:
                cat = effective_category(ticket)
                if cat in ("security", "fraud"):
                    raise ValueError(f"Cannot update status of {cat} tickets")

                if is_human_owned(ticket):
                    raise ValueError("Cannot update status of human-owned tickets")

                allowed = CUSTOMER_ALLOWED_TRANSITIONS.get(ticket.status, [])
                if status not in allowed:
                    raise ValueError(f"Invalid transition from {ticket.status} to {status}")

            # Optimistic check on from_status
            if from_status and ticket.status != from_status:
                raise ValueError(f"Status conflict: current status is {ticket.status}, expected {from_status}")

            now = datetime.now(timezone.utc)
            old_status = ticket.status
            ticket.status = status
            ticket.updated_at = now
            if resolution_notes is not None:
                ticket.resolution_notes = resolution_notes
            if answer_text is not None:
                ticket.answer_text = answer_text
            if citations is not None:
                ticket.citations = citations

            # Audit event status_changed with NO message text
            payload = {
                "from": from_status or old_status,
                "to": status,
                "actor": actor or "system",
                "reason": reason or resolution_notes or "status_update",
                "trace_id": trace,
            }
            if idempotency_key:
                payload["idempotency_key"] = idempotency_key

            self._events.setdefault(t_id, []).append(
                TicketEvent(
                    event_id=uuid4(),
                    ticket_id=t_id,
                    event_type="status_changed",
                    actor=actor or "system",
                    payload=payload,
                    created_at=now,
                )
            )
            return ticket

    def get_redaction_map(self, ticket_id: UUID | str) -> Optional[RedactionMap]:
        maps = self.get_redaction_maps(ticket_id)
        return maps[0] if maps else None

    def get_redaction_maps(self, ticket_id: UUID | str) -> List[RedactionMap]:
        t_id = self._normalize_uuid(ticket_id)
        with self._lock:
            maps = list(self._redaction_maps.get(t_id, []))
            maps.sort(key=lambda m: m.created_at, reverse=True)
            return maps

    def save_redaction_map(self, redaction_map: RedactionMap) -> RedactionMap:
        with self._lock:
            t_id = self._normalize_uuid(redaction_map.ticket_id)
            if t_id not in self._redaction_maps:
                self._redaction_maps[t_id] = []
            self._redaction_maps[t_id].append(redaction_map)
            return redaction_map

    def purge_expired_redaction_maps(self) -> int:
        now = datetime.now(timezone.utc)
        deleted_count = 0
        with self._lock:
            for t_id, maps in list(self._redaction_maps.items()):
                active_maps = []
                for m in maps:
                    if m.expires_at and m.expires_at < now:
                        deleted_count += 1
                    else:
                        active_maps.append(m)
                if active_maps:
                    self._redaction_maps[t_id] = active_maps
                else:
                    self._redaction_maps.pop(t_id, None)
        return deleted_count

    def list_customer_tickets(
        self, customer_id: str, limit: int = 5
    ) -> List[Ticket]:
        """List recent tickets belonging to a customer, newest first (REQ-HIST-003)."""
        if not customer_id or str(customer_id).startswith("anon:"):
            raise PermissionError("Anonymous callers cannot list customer tickets")
        with self._lock:
            matching = [
                t for t in self._tickets.values()
                if t.customer_id == customer_id
            ]
            matching.sort(key=lambda t: t.created_at, reverse=True)
            return matching[:limit]

    def get_active_ticket_for_session(
        self, session_id: str, customer_id: Optional[str] = None
    ) -> Optional[Ticket]:
        """Fetch the most recent active holding ticket for a conversation session (REQ-AGENT-033)."""
        with self._lock:
            matching: List[Ticket] = []
            for t in self._tickets.values():
                t_session = t.session_id
                if not t_session and isinstance(t.decision, dict):
                    t_session = t.decision.get("session_id")
                if t_session == session_id:
                    if customer_id and t.customer_id != customer_id:
                        continue
                    matching.append(t)

            if not matching:
                return None

            # Sort by created_at descending (most recent first)
            matching.sort(key=lambda x: x.created_at, reverse=True)
            for cand in matching:
                cand_cat = effective_category(cand)
                if cand_cat in ("security", "fraud"):
                    continue
                if not is_human_owned(cand):
                    continue
                if is_holding_ticket(cand.status, cand.assigned_queue, cand_cat, cand.current_owner, cand.decision):
                    return cand
            return None

    def get_pending_confirmation(
        self, session_id: str, customer_id: Optional[str] = None
    ) -> Optional[Ticket]:
        """Fetch newest bot-owned support ticket in session with pending resolution confirmation (REQ-TURN-005)."""
        with self._lock:
            matching: List[Ticket] = []
            for t in self._tickets.values():
                t_session = t.session_id
                if not t_session and isinstance(t.decision, dict):
                    t_session = t.decision.get("session_id")
                if t_session == session_id:
                    if customer_id and t.customer_id != customer_id:
                        continue
                    if t.status not in ("open", "answered"):
                        continue
                    cat = effective_category(t)
                    if cat in ("security", "fraud"):
                        continue
                    if is_human_owned(t):
                        continue
                    raw_owner = t.current_owner
                    if raw_owner and raw_owner != "bot":
                        continue
                    assigned_q = (t.assigned_queue or "").lower()
                    if assigned_q.startswith("human"):
                        continue
                    matching.append(t)

            if not matching:
                return None

            # Sort by created_at descending (most recent first)
            matching.sort(key=lambda x: x.created_at, reverse=True)
            for cand in matching:
                cand_tid = self._normalize_uuid(cand.ticket_id)
                events = self._events.get(cand_tid, [])
                conf_events = [ev for ev in events if ev.event_type == "resolution_confirmation_requested"]
                if not conf_events:
                    continue
                latest_conf = max(ev.created_at for ev in conf_events)
                status_events = [ev for ev in events if ev.event_type == "status_changed"]
                if status_events:
                    latest_status = max(ev.created_at for ev in status_events)
                    if latest_conf < latest_status:
                        continue
                return cand
            return None

    def append_customer_followup(
        self,
        ticket_id: UUID | str,
        body_redacted: str,
        customer_id: Optional[str] = None,
        traceparent: Optional[str] = None,
        message_hash: Optional[str] = None,
    ) -> Tuple[TicketEvent, bool]:
        """Append a customer follow-up audit event to an active ticket (REQ-AGENT-033)."""
        t_id = self._normalize_uuid(ticket_id)
        with self._lock:
            if t_id not in self._tickets:
                raise KeyError(f"Ticket not found: {t_id}")
            ticket = self._tickets[t_id]
            if customer_id and ticket.customer_id != customer_id:
                raise PermissionError(f"Customer mismatch for ticket {t_id}")

            msg_hash = (
                message_hash
                or hashlib.sha256(body_redacted.encode("utf-8")).hexdigest()
            )
            # Idempotency check on (ticket_id, message_hash)
            events = self._events.get(t_id, [])
            for ev in events:
                if ev.event_type == "customer_followup" and isinstance(ev.payload, dict):
                    ev_body = ev.payload.get("body_redacted", "")
                    ev_hash = hashlib.sha256(ev_body.encode("utf-8")).hexdigest()
                    if ev_hash == msg_hash or ev_body == body_redacted:
                        return ev, False

            now = datetime.now(timezone.utc)
            payload = {
                "body_redacted": body_redacted,
                "traceparent": traceparent,
            }
            event = TicketEvent(
                event_id=uuid4(),
                ticket_id=t_id,
                event_type="customer_followup",
                actor="customer",
                payload=payload,
                created_at=now,
            )
            self._events.setdefault(t_id, []).append(event)
            ticket.updated_at = now
            return event, True


class FakeOrderRepo(OrderRepository):
    """In-memory fake implementing OrderRepository with RLS customer isolation."""

    def __init__(self, orders: Optional[List[Order]] = None) -> None:
        self._lock = threading.Lock()
        self._orders: Dict[str, Order] = {}
        if orders:
            for o in orders:
                self._orders[o.order_id] = o

    def add_order(self, order: Order) -> None:
        with self._lock:
            self._orders[order.order_id] = order

    def get_order(
        self, order_id: str, customer_id: Optional[str] = None
    ) -> Optional[Order]:
        """Fetch order with simulated Postgres RLS customer isolation."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return None
            # Simulate RLS policy: if customer_id specified, must match row customer_id
            if customer_id is not None and order.customer_id != customer_id:
                return None
            return order

    def list_customer_orders(
        self, customer_id: str, limit: int = 10
    ) -> List[Order]:
        with self._lock:
            orders = [
                o for o in self._orders.values() if o.customer_id == customer_id
            ]
            orders.sort(key=lambda x: x.created_at, reverse=True)
            return orders[:limit]


class FakeCustomerRepo(CustomerRepository):
    """In-memory fake implementing CustomerRepository for testing."""

    def __init__(
        self,
        customers: Optional[List[Customer]] = None,
        identities: Optional[List[CustomerIdentity]] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._customers: Dict[str, Customer] = {}
        self._identities: Dict[Tuple[str, str], CustomerIdentity] = {}
        self._events: List[IdentityEvent] = []

        if customers:
            for c in customers:
                self._customers[c.customer_id] = c
        if identities:
            for i in identities:
                self._identities[(i.issuer, i.subject)] = i

    def resolve(self, issuer: str, subject: str) -> CustomerResolution:
        with self._lock:
            ident = self._identities.get((issuer, subject))
            if ident is None:
                return CustomerResolution(
                    customer_id=None,
                    status="unlinked",
                    linked=False,
                    email_verified=False,
                )
            cust = self._customers.get(ident.customer_id)
            if cust is None:
                return CustomerResolution(
                    customer_id=None,
                    status="unlinked",
                    linked=False,
                    email_verified=False,
                )
            return CustomerResolution(
                customer_id=cust.customer_id,
                status=cust.status,
                linked=True,
                email_verified=ident.email_verified,
            )

    def append_identity_event(
        self,
        customer_id: Optional[str],
        issuer: Optional[str],
        event_type: str,
        actor: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> IdentityEvent:
        with self._lock:
            now = datetime.now(timezone.utc)
            event = IdentityEvent(
                event_id=uuid4(),
                customer_id=customer_id,
                issuer=issuer,
                event_type=event_type,
                actor=actor,
                payload=payload or {},
                created_at=now,
            )
            self._events.append(event)
            return event

    def link_identity(
        self,
        issuer: str,
        subject: str,
        customer_id: str,
        actor: str,
        email_verified: bool = False,
    ) -> None:
        with self._lock:
            now = datetime.now(timezone.utc)
            if customer_id not in self._customers:
                self._customers[customer_id] = Customer(
                    customer_id=customer_id,
                    status="active",
                    created_at=now,
                )
            self._identities[(issuer, subject)] = CustomerIdentity(
                issuer=issuer,
                subject=subject,
                customer_id=customer_id,
                email_verified=email_verified,
                linked_at=now,
            )
            self._events.append(
                IdentityEvent(
                    event_id=uuid4(),
                    customer_id=customer_id,
                    issuer=issuer,
                    event_type="identity_linked",
                    actor=actor,
                    payload={"subject": subject, "email_verified": email_verified},
                    created_at=now,
                )
            )

    def unlink_identity(self, issuer: str, subject: str, actor: str) -> bool:
        with self._lock:
            ident = self._identities.pop((issuer, subject), None)
            if ident is None:
                return False
            now = datetime.now(timezone.utc)
            self._events.append(
                IdentityEvent(
                    event_id=uuid4(),
                    customer_id=ident.customer_id,
                    issuer=issuer,
                    event_type="identity_unlinked",
                    actor=actor,
                    payload={"subject": subject},
                    created_at=now,
                )
            )
            return True

    def suspend_customer(self, customer_id: str, actor: str) -> bool:
        with self._lock:
            cust = self._customers.get(customer_id)
            if cust is None:
                return False
            cust.status = "suspended"
            now = datetime.now(timezone.utc)
            self._events.append(
                IdentityEvent(
                    event_id=uuid4(),
                    customer_id=customer_id,
                    issuer=None,
                    event_type="customer_suspended",
                    actor=actor,
                    payload={},
                    created_at=now,
                )
            )
            return True

    def get_customer(self, customer_id: str) -> Optional[Customer]:
        with self._lock:
            return self._customers.get(customer_id)

    def list_events(self, customer_id: Optional[str] = None) -> List[IdentityEvent]:
        with self._lock:
            if customer_id is None:
                return list(self._events)
            return [e for e in self._events if e.customer_id == customer_id]

