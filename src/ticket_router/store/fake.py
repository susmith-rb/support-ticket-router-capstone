"""In-memory fakes for TicketRepository and OrderRepository.

Implements REQ-STORE-006: Lightweight thread-safe fakes for hermetic unit testing
without live Cloud SQL PostgreSQL dependencies.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from ticket_router.store.interfaces import OrderRepository, TicketRepository
from ticket_router.store.models import Order, RedactionMap, Ticket, TicketCreate, TicketEvent


class FakeTicketRepo(TicketRepository):
    """Thread-safe in-memory fake implementing TicketRepository."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: Dict[UUID, Ticket] = {}
        self._events: Dict[UUID, List[TicketEvent]] = {}
        self._redaction_maps: Dict[UUID, RedactionMap] = {}

    def _normalize_uuid(self, ticket_id: UUID | str) -> UUID:
        if isinstance(ticket_id, str):
            return UUID(ticket_id)
        return ticket_id

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
        t_id = self._normalize_uuid(ticket_id)
        with self._lock:
            return self._redaction_maps.get(t_id)

    def save_redaction_map(self, redaction_map: RedactionMap) -> RedactionMap:
        with self._lock:
            self._redaction_maps[redaction_map.ticket_id] = redaction_map
            return redaction_map


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
