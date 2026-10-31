"""Audit event helpers for HumanQueue operations.

# REQ-AGENT-009: HumanQueue Workflow and Audited PII Re-Identification
# REQ-AGENT-014: Observability: Distributed Tracing & PII-Free Structured Logging
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List
from uuid import UUID

from ticket_router.store.interfaces import TicketRepository
from ticket_router.store.models import TicketEvent

logger = logging.getLogger("ticket_router.agents.human_queue.audit")


def record_pii_reidentification_event(
    repo: TicketRepository,
    ticket_id: UUID | str,
    operator_id: str,
    fields_unmasked: List[str],
) -> TicketEvent:
    """Record an immutable audit event when an operator unmasks customer PII.

    In accordance with REQ-AGENT-009, REQ-AGENT-014, and AGENTS.md rule 3:
    The audit log records operator_id, ticket_id, timestamp, and field names.
    Actual unmasked PII text is NEVER written to the event log.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "operator_id": operator_id,
        "rep_id": operator_id,
        "ticket_id": str(ticket_id),
        "timestamp": now_iso,
        "fields_unmasked": sorted(fields_unmasked),
    }

    event = repo.append_event(
        ticket_id=ticket_id,
        event_type="pii_reidentified",
        actor=operator_id,
        payload=payload,
    )

    logger.info(
        "PII re-identification audited: ticket_id=%s operator_id=%s fields=%s",
        str(ticket_id),
        operator_id,
        fields_unmasked,
    )
    return event
