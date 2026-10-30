"""In-memory fakes for TicketRepository and OrderRepository.

Implements REQ-STORE-006: Lightweight thread-safe fakes for hermetic unit testing
without live Cloud SQL PostgreSQL dependencies.
"""

from __future__ import annotations

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
    Customer,
    CustomerIdentity,
    CustomerResolution,
    IdentityEvent,
    Order,
    RedactionMap,
    Ticket,
    TicketCreate,
    TicketEvent,
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
            return self._tickets.get(t_id)

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
    ) -> Optional[Ticket]:
        t_id = self._normalize_uuid(ticket_id)
        with self._lock:
            ticket = self._tickets.get(t_id)
            if ticket is None:
                return None

            now = datetime.now(timezone.utc)
            ticket.status = status
            ticket.updated_at = now
            if resolution_notes is not None:
                ticket.resolution_notes = resolution_notes
            if answer_text is not None:
                ticket.answer_text = answer_text
            if citations is not None:
                ticket.citations = citations

            self._events.setdefault(t_id, []).append(
                TicketEvent(
                    event_id=uuid4(),
                    ticket_id=t_id,
                    event_type="status_changed",
                    actor="system",
                    payload={"status": status, "resolution_notes": resolution_notes},
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

    def add_customer(self, customer: Customer) -> None:
        with self._lock:
            self._customers[customer.customer_id] = customer

    def add_identity(self, identity: CustomerIdentity) -> None:
        with self._lock:
            self._identities[(identity.issuer, identity.subject)] = identity

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

