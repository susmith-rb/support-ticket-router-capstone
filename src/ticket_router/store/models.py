"""Data models for Cloud SQL Support Ticket Store.

Implements REQ-STORE-006: Strong typing for tickets, audit events, orders,
and redaction maps according to TDD §3 and §5.2 data classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

# Ticket Status Semantics (REQ-AGENT-033)
# Holding statuses: keep a session attached to human review (automation does not re-route)
TICKET_STATUS_ESCALATED = "escalated"
TICKET_STATUS_QUEUED_FOR_REVIEW = "queued_for_review"
TICKET_STATUS_CLAIMED = "claimed"
TICKET_STATUS_ASSIGNED = "assigned"  # real status set by Postgres claim_ticket
TICKET_STATUS_IN_PROGRESS = "in_progress"  # real status set by in-memory claim_ticket
TICKET_STATUS_OPEN = "open"  # real status in tickets table before claim

# NOTE: "open" is NOT a holding status on its own. Live data (2026-10-31) shows auto-answered
# tickets are also "open"; an open ticket holds the conversation only when it was routed to a
# human queue (assigned_queue "human-*"). See is_holding_ticket().
HOLDING_STATUSES = (
    TICKET_STATUS_ESCALATED,
    TICKET_STATUS_QUEUED_FOR_REVIEW,
    TICKET_STATUS_CLAIMED,
    TICKET_STATUS_ASSIGNED,
    TICKET_STATUS_IN_PROGRESS,
)

# Releasing statuses: release the session so subsequent messages route normally
TICKET_STATUS_RESOLVED = "resolved"
TICKET_STATUS_RESOLVED_AUTO = "resolved_auto"
TICKET_STATUS_CLOSED = "closed"
TICKET_STATUS_RETURNED_TO_AUTOMATION = "returned_to_automation"

RELEASING_STATUSES = (
    TICKET_STATUS_RESOLVED,
    TICKET_STATUS_RESOLVED_AUTO,
    TICKET_STATUS_CLOSED,
    TICKET_STATUS_RETURNED_TO_AUTOMATION,
)


def is_holding_ticket(status: Optional[str], assigned_queue: Optional[str]) -> bool:
    """True when a ticket holds its conversation for humans (REQ-AGENT-033).

    Releasing statuses never hold. Explicit human-workflow statuses always hold. A plain
    "open" ticket holds only when it was escalated to a human queue (assigned_queue human-*).
    """
    st = (status or "").strip().lower()
    if st in RELEASING_STATUSES:
        return False
    if st in HOLDING_STATUSES:
        return True
    queue = (assigned_queue or "").strip().lower()
    return st == TICKET_STATUS_OPEN and queue.startswith("human")


@dataclass
class TicketCreate:
    """Request payload to create a support ticket in Cloud SQL."""

    customer_id: str
    body_redacted: str
    channel: str = "web"
    decision: Optional[Dict[str, Any]] = None
    assigned_to: Optional[str] = None
    assigned_queue: Optional[str] = None
    ticket_id: Optional[UUID] = None
    session_id: Optional[str] = None


@dataclass
class Ticket:
    """Full ticket record representing a row in Cloud SQL tickets table."""

    ticket_id: UUID
    customer_id: str
    body_redacted: str
    channel: str = "web"
    decision: Optional[Dict[str, Any]] = None
    status: str = "open"
    assigned_to: Optional[str] = None
    assigned_queue: Optional[str] = None
    answer_text: Optional[str] = None
    citations: Optional[List[Dict[str, Any]]] = None
    resolution_notes: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: Optional[str] = None


@dataclass
class TicketEvent:
    """Audit log event representing a row in ticket_events table."""

    event_id: UUID
    ticket_id: UUID
    event_type: str
    actor: str
    payload: Optional[Dict[str, Any]] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Order:
    """Order mock record representing a row in orders_mock table."""

    order_id: str
    customer_id: str
    order_status: str
    items: List[Dict[str, Any]]
    total_cents: int
    currency: str = "USD"
    tracking_number: Optional[str] = None
    shipping_carrier: Optional[str] = None
    shipping_address_city: Optional[str] = None
    shipping_address_state: Optional[str] = None
    estimated_delivery: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RedactionMap:
    """Confidential PII redaction map with 30-day TTL in redaction_maps table."""

    map_id: UUID
    ticket_id: UUID
    customer_id: str
    encrypted_mappings: bytes
    key_id: str = "kms-envelope-v1"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None


@dataclass
class Customer:
    """Master customer record in customers table."""

    customer_id: str
    status: str = "active"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class CustomerIdentity:
    """Identity Platform identity mapping in customer_identities table."""

    issuer: str
    subject: str
    customer_id: str
    email_verified: bool = False
    linked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class IdentityEvent:
    """Immutable audit event in identity_events table."""

    event_id: UUID
    customer_id: Optional[str]
    issuer: Optional[str]
    event_type: str
    actor: str
    payload: Optional[Dict[str, Any]] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class CustomerResolution:
    """Resolution result of an IdP identity mapping to a customer record."""

    customer_id: Optional[str]
    status: str  # "active" | "suspended" | "unlinked"
    linked: bool
    email_verified: bool = False
