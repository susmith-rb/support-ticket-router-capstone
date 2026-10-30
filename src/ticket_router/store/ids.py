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
