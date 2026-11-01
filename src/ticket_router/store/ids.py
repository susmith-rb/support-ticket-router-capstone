"""Canonical identifier utilities for support tickets.

# REQ-AGENT-005: Idempotent Ticket Upsert
# REQ-STORE-009: KMS Envelope-Encrypted Redaction Maps
"""

from __future__ import annotations

import uuid
from typing import Union
from uuid import UUID

NAMESPACE_TICKET = uuid.NAMESPACE_URL


def canonical_ticket_uuid(idempotency_key: Union[str, UUID]) -> str:
    """Compute deterministic canonical ticket UUID from an idempotency key.

    If idempotency_key is already a valid UUID string or UUID instance, it is returned
    as a standardized UUID string. Otherwise, uuid5(NAMESPACE_URL, f"idempotency:{idempotency_key}")
    is computed to guarantee exactly one UUID derivation across all services.
    """
    if isinstance(idempotency_key, UUID):
        return str(idempotency_key)
    try:
        return str(UUID(str(idempotency_key)))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(NAMESPACE_TICKET, f"idempotency:{idempotency_key}"))


def issue_reference(ticket_uuid: Union[str, UUID]) -> str:
    """Format a ticket UUID or identifier as a canonical customer-facing reference.

    Canonical format: TICK-{clean_hex[:8].upper()}
    """
    s = str(ticket_uuid).strip()
    if s.upper().startswith("TICK-"):
        token = s[5:].replace("-", "").strip()
        return f"TICK-{token[:8].upper()}" if token else s.upper()
    clean_hex = s.replace("-", "").strip()
    return f"TICK-{clean_hex[:8].upper()}"


def legacy_session_issue_reference(session_id: str) -> str:
    """Derive a legacy fallback reference from a session identifier.

    Format: TICK-{session_id[:8]}
    """
    return f"TICK-{str(session_id)[:8]}"

