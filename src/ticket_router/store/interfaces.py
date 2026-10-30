"""Abstract repository interfaces for ticket and order stores.

Implements REQ-STORE-006: Pure interface contracts separating business logic
from storage backends (in-memory fakes vs Cloud SQL PostgreSQL).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from uuid import UUID

from ticket_router.store.models import (
    Customer,
    CustomerResolution,
    IdentityEvent,
    Order,
    RedactionMap,
    Ticket,
    TicketCreate,
    TicketEvent,
)


class TicketRepository(ABC):
    """Abstract interface for ticket lifecycle and audit event operations."""

    @abstractmethod
    def create_ticket(self, ticket_create: TicketCreate) -> Ticket:
        """Create a new ticket idempotently.

        If a ticket with the given ticket_id already exists, returns the existing record.
        """

    @abstractmethod
    def get_ticket(self, ticket_id: UUID | str) -> Optional[Ticket]:
        """Fetch ticket by ID."""

    @abstractmethod
    def list_open_tickets(
        self, limit: int = 50, queue: Optional[str] = None
    ) -> List[Ticket]:
        """List open tickets eligible for human queue or automated pickup."""

    @abstractmethod
    def claim_ticket(self, ticket_id: UUID | str, agent_id: str) -> bool:
        """Atomically claim an open ticket for a human agent or workflow.

        Enforces optimistic concurrency: only transitions if current status is 'open'.
        Returns True if claimed successfully, False if already claimed or resolved.
        """

    @abstractmethod
    def append_event(
        self,
        ticket_id: UUID | str,
        event_type: str,
        actor: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> TicketEvent:
        """Append an immutable audit event for a ticket."""

    @abstractmethod
    def list_events(self, ticket_id: UUID | str) -> List[TicketEvent]:
        """List all audit events for a ticket in chronological order."""

    @abstractmethod
    def update_ticket_status(
        self,
        ticket_id: UUID | str,
        status: str,
        resolution_notes: Optional[str] = None,
        answer_text: Optional[str] = None,
        citations: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Ticket]:
        """Update ticket status, resolution notes, and optional grounded answer."""

    @abstractmethod
    def get_redaction_map(self, ticket_id: UUID | str) -> Optional[RedactionMap]:
        """Fetch redaction map for a ticket (held by queue_rw role)."""

    @abstractmethod
    def get_redaction_maps(self, ticket_id: UUID | str) -> List[RedactionMap]:
        """Fetch all redaction maps for a ticket, newest first (held by queue_rw role)."""

    @abstractmethod
    def save_redaction_map(self, redaction_map: RedactionMap) -> RedactionMap:
        """Store or insert customer PII redaction map with 30-day TTL."""

    @abstractmethod
    def purge_expired_redaction_maps(self) -> int:
        """Purge expired redaction maps (expires_at < now). Returns count of deleted maps."""


class OrderRepository(ABC):
    """Abstract interface for customer order lookups (used by order-lookup MCP server)."""

    @abstractmethod
    def get_order(
        self, order_id: str, customer_id: Optional[str] = None
    ) -> Optional[Order]:
        """Fetch an order by order_id.

        If customer_id is provided, enforces tenant isolation (simulating Postgres RLS).
        Returns None if order does not exist or customer_id does not match.
        """

    @abstractmethod
    def list_customer_orders(
        self, customer_id: str, limit: int = 10
    ) -> List[Order]:
        """List recent orders belonging to a customer."""


class CustomerRepository(ABC):
    """Abstract interface for customer identity resolution and lifecycle operations."""

    @abstractmethod
    def resolve(self, issuer: str, subject: str) -> CustomerResolution:
        """Resolve an IdP (issuer, subject) identity pair to a customer_id and verify active status.

        Returns CustomerResolution with:
        - customer_id: str or None
        - status: "active", "suspended", or "unlinked"
        - linked: bool (True if mapping exists)
        - email_verified: bool
        Never raises for unlinked identities.
        """

    @abstractmethod
    def append_identity_event(
        self,
        customer_id: Optional[str],
        issuer: Optional[str],
        event_type: str,
        actor: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> IdentityEvent:
        """Append an immutable audit event for customer identity lifecycle."""

    @abstractmethod
    def link_identity(
        self,
        issuer: str,
        subject: str,
        customer_id: str,
        actor: str,
        email_verified: bool = False,
    ) -> None:
        """Administratively link an IdP (issuer, subject) to a customer_id and log audit event."""

    @abstractmethod
    def unlink_identity(self, issuer: str, subject: str, actor: str) -> bool:
        """Administratively unlink an IdP identity mapping and log audit event."""

    @abstractmethod
    def suspend_customer(self, customer_id: str, actor: str) -> bool:
        """Administratively suspend a customer account and log audit event."""

    @abstractmethod
    def get_customer(self, customer_id: str) -> Optional[Customer]:
        """Fetch customer master record by customer_id."""

