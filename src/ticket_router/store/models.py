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
TICKET_STATUS_RESOLVED_BY_CUSTOMER = "resolved_by_customer"
TICKET_STATUS_CLOSED = "closed"
TICKET_STATUS_RETURNED_TO_AUTOMATION = "returned_to_automation"

# Other active statuses
TICKET_STATUS_ANSWERED = "answered"
TICKET_STATUS_REOPENED = "reopened"

RELEASING_STATUSES = (
    TICKET_STATUS_RESOLVED,
    TICKET_STATUS_RESOLVED_AUTO,
    TICKET_STATUS_RESOLVED_BY_CUSTOMER,
    TICKET_STATUS_CLOSED,
    TICKET_STATUS_RETURNED_TO_AUTOMATION,
)

CUSTOMER_ALLOWED_TRANSITIONS: Dict[str, List[str]] = {
    "open": ["resolved_by_customer"],
    "answered": ["resolved_by_customer"],
    "resolved_by_customer": ["reopened"],
}


def effective_category(
    ticket_or_fields: Any = None,
    *,
    category: Optional[str] = None,
    decision: Optional[Dict[str, Any]] = None,
) -> str:
    """Return canonical effective category ('security', 'fraud', or other) with defense in depth.

    Handles Ticket dataclasses, dicts, or keyword arguments. Treats rows as security if:
    - Explicit category is 'security' or 'fraud'
    - decision.tier == 'model_armor'
    - decision.reason in ('model_armor_security_trigger', 'prompt_injection_or_jailbreak')
    - decision.details.category in ('security', 'fraud')
    """
    if ticket_or_fields is not None:
        if hasattr(ticket_or_fields, "category"):
            category = getattr(ticket_or_fields, "category", None)
            decision = getattr(ticket_or_fields, "decision", None)
        elif isinstance(ticket_or_fields, dict):
            category = ticket_or_fields.get("category")
            decision = ticket_or_fields.get("decision")

    cat = (category or "").strip().lower()
    if cat in ("security", "fraud"):
        return cat

    if isinstance(decision, str):
        try:
            import json
            decision = json.loads(decision)
        except Exception:
            decision = None

    if isinstance(decision, dict):
        if decision.get("tier") == "model_armor":
            return "security"
        if decision.get("reason") in (
            "model_armor_security_trigger",
            "prompt_injection_or_jailbreak",
        ):
            return "security"
        details = decision.get("details")
        if isinstance(details, dict):
            det_cat = (details.get("category") or "").strip().lower()
            if det_cat in ("security", "fraud"):
                return det_cat

    return cat or "support"


def is_human_owned(
    ticket_or_fields: Any = None,
    *,
    current_owner: Optional[str] = None,
    assigned_to: Optional[str] = None,
    assigned_queue: Optional[str] = None,
) -> bool:
    """True when ticket is owned by a human rep or human queue.

    Human-owned = current_owner startswith 'rep:' or 'queue:human', OR assigned_to set,
    OR assigned_queue startswith 'human'.
    """
    if ticket_or_fields is not None:
        if hasattr(ticket_or_fields, "current_owner"):
            current_owner = getattr(ticket_or_fields, "current_owner", None)
            assigned_to = getattr(ticket_or_fields, "assigned_to", None)
            assigned_queue = getattr(ticket_or_fields, "assigned_queue", None)
        elif isinstance(ticket_or_fields, dict):
            current_owner = ticket_or_fields.get("current_owner")
            assigned_to = ticket_or_fields.get("assigned_to")
            assigned_queue = ticket_or_fields.get("assigned_queue")

    owner = (current_owner or "").strip().lower()
    if owner.startswith(("rep:", "queue:human")):
        return True
    if assigned_to and str(assigned_to).strip() and str(assigned_to).strip().lower() not in ("none", ""):
        return True
    queue = (assigned_queue or "").strip().lower()
    if queue.startswith("human"):
        return True
    return False


def is_holding_ticket(
    status: Optional[str],
    assigned_queue: Optional[str],
    category: Optional[str] = None,
    current_owner: Optional[str] = None,
    decision: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when a ticket holds its conversation for humans (REQ-AGENT-033, REQ-AGENT-037).

    Security/fraud tickets never hold. Bot-owned tickets never hold. Releasing statuses
    never hold. Human-owned tickets in holding statuses or open human queues hold.
    """
    eff_cat = effective_category(category=category, decision=decision)
    if eff_cat in ("security", "fraud"):
        return False
    st = (status or "").strip().lower()
    if st in RELEASING_STATUSES:
        return False
    queue = (assigned_queue or "").strip().lower()
    owner = (current_owner or "").strip().lower()
    if owner == "bot" and not queue.startswith("human"):
        return False
    if st in HOLDING_STATUSES:
        return True
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
    issue_reference: Optional[str] = None
    category: str = "support"
    current_owner: str = "bot"


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
    issue_reference: Optional[str] = None
    category: str = "support"
    current_owner: str = "bot"


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
