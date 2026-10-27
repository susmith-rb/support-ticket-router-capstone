"""Data models for Cloud SQL Support Ticket Store.

Implements REQ-STORE-006: Strong typing for tickets, audit events, orders,
and redaction maps according to TDD §3 and §5.2 data classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID


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
    key_id: str = "secret-manager-v1"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
