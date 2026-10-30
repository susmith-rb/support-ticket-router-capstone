"""HumanQueueAgent: Queue operations, priority scheduling, and audited PII re-identification.

# REQ-AGENT-009: HumanQueue Workflow and Audited PII Re-Identification
# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
# REQ-AGENT-004: Gaming-Resistant Queue Priority Assignment

Dedicated Cloud SQL IAM database role 'queue_rw' is held by sa-queue@, permitting
read access to tickets and redaction_maps, and column-level updates to status/notes.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from ticket_router.agents.human_queue.audit import (
    record_pii_reidentification_event,
)
from ticket_router.agents.triage_router.mcp_client import (
    TicketDbMcpClientProtocol,
)
from ticket_router.store.fake import FakeTicketRepo
from ticket_router.store.interfaces import TicketRepository
from ticket_router.store.models import Ticket

logger = logging.getLogger("ticket_router.agents.human_queue")

# Queue priority order: P1 Urgent -> P2 Standard -> Others (REQ-AGENT-004)
_QUEUE_PRIORITY_RANK = {
    "human-urgent": 1,
    "human-standard": 2,
    "auto": 3,
}


def _queue_sort_key(ticket: Ticket) -> tuple[int, datetime]:
    """Sort key for queue ordering: highest priority first, then FIFO by created_at."""
    rank = _QUEUE_PRIORITY_RANK.get(ticket.assigned_queue or "human-standard", 99)
    # created_at ascending for FIFO within priority
    return (rank, ticket.created_at)


class HumanQueueAgent:
    """HumanQueueAgent providing operator queue visibility, claiming, and audited unmasking."""

    def __init__(
        self,
        ticket_repo: Optional[TicketRepository] = None,
        mcp_client: Optional[TicketDbMcpClientProtocol] = None,
        envelope_cipher: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        self.repo: TicketRepository = ticket_repo if ticket_repo is not None else FakeTicketRepo()
        self.mcp_client: Optional[TicketDbMcpClientProtocol] = mcp_client
        if envelope_cipher is not None:
            self.cipher = envelope_cipher
        else:
            kms_key = os.environ.get("REDACTION_KMS_KEY")
            if kms_key:
                from ticket_router.security.envelope import EnvelopeCipher

                self.cipher = EnvelopeCipher(kms_key)
            else:
                self.cipher = None

    def list_open_queue(
        self,
        limit: int = 50,
        queue: Optional[str] = None,
    ) -> List[Ticket]:
        """List open tickets eligible for human review, sorted strictly by business priority.

        REQ-AGENT-004 & REQ-AGENT-009: Urgent (P1) tickets surface before Standard (P2),
        ordered by oldest creation timestamp (FIFO) within each priority tier.
        """
        open_tickets = self.repo.list_open_tickets(limit=1000, queue=queue)
        open_tickets.sort(key=_queue_sort_key)
        return open_tickets[:limit]

    def queue_depths(self) -> Dict[str, int]:
        """Return the count of open tickets grouped by queue (REQ-AGENT-009)."""
        all_open = self.repo.list_open_tickets(limit=1000)
        depths: Dict[str, int] = {
            "human-urgent": 0,
            "human-standard": 0,
        }
        for t in all_open:
            q_name = t.assigned_queue or "human-standard"
            depths[q_name] = depths.get(q_name, 0) + 1
        return depths

    def claim_ticket(self, ticket_id: UUID | str, operator_id: str) -> Dict[str, Any]:
        """Claim a ticket for an active human operator (REQ-AGENT-009).

        Enforces optimistic concurrency: only transitions if ticket is currently open.
        """
        # If MCP client is provided and preferred, delegate
        if self.mcp_client is not None:
            return self.mcp_client.claim_ticket(str(ticket_id), operator_id)

        try:
            target_uuid = UUID(str(ticket_id))
        except ValueError:
            return {"success": False, "error": "not_found"}

        ticket = self.repo.get_ticket(target_uuid)
        if ticket is None:
            return {"success": False, "error": "not_found"}

        if ticket.assigned_to and ticket.assigned_to != operator_id:
            return {"success": False, "error": "already_claimed"}

        if ticket.status != "open" and ticket.assigned_to != operator_id:
            return {"success": False, "error": "already_claimed"}

        claimed = self.repo.claim_ticket(target_uuid, operator_id)
        if not claimed and ticket.assigned_to != operator_id:
            return {"success": False, "error": "already_claimed"}

        return {"success": True}

    def resolve_ticket(
        self,
        ticket_id: UUID | str,
        operator_id: str,
        resolution_notes: str,
    ) -> Dict[str, Any]:
        """Mark ticket as resolved with operator notes and emit audit event (REQ-AGENT-009)."""
        if self.mcp_client is not None:
            return self.mcp_client.resolve_ticket(
                ticket_id=str(ticket_id),
                agent_id=operator_id,
                resolution_notes=resolution_notes,
            )

        try:
            target_uuid = UUID(str(ticket_id))
        except ValueError:
            return {"status": "not_found"}

        ticket = self.repo.get_ticket(target_uuid)
        if ticket is None:
            return {"status": "not_found"}

        self.repo.update_ticket_status(
            ticket_id=target_uuid,
            status="resolved",
            resolution_notes=resolution_notes,
        )
        self.repo.append_event(
            ticket_id=target_uuid,
            event_type="resolved",
            actor=operator_id,
            payload={"resolution_notes": resolution_notes},
        )
        return {"status": "RESOLVED"}

    def reidentify_pii(
        self,
        ticket_id: UUID | str,
        operator_id: str,
    ) -> Dict[str, Any]:
        """Request audited unmasking of customer PII (REQ-AGENT-009).

        Security Invariant:
        Customer PII unmasking from redaction_maps is restricted exclusively to
        the human operator who has actively claimed the ticket.
        Raises PermissionError if called by an unauthorized operator.

        Audit Invariant:
        Emits an immutable audit event to ticket_events logging operator_id,
        ticket_id, timestamp, and unmasked field names.
        """
        # If MCP client is provided, delegate
        if self.mcp_client is not None:
            resp = self.mcp_client.reidentify_pii(str(ticket_id), operator_id)
            if resp.get("error") == "unauthorized_not_claimed":
                raise PermissionError(
                    f"Operator '{operator_id}' is not authorized to unmask PII on ticket '{ticket_id}' (ticket not claimed by caller)."
                )
            return resp

        try:
            target_uuid = UUID(str(ticket_id))
        except ValueError as err:
            raise KeyError(f"Ticket '{ticket_id}' not found.") from err

        ticket = self.repo.get_ticket(target_uuid)
        if ticket is None:
            raise KeyError(f"Ticket '{ticket_id}' not found.")

        # Enforce claim authorization check
        if ticket.assigned_to != operator_id:
            logger.warning(
                "Access Denied: Operator '%s' attempted to unmask PII on ticket '%s' assigned to '%s'",
                operator_id,
                str(ticket_id),
                ticket.assigned_to,
            )
            raise PermissionError(
                f"Operator '{operator_id}' is not authorized to unmask PII on ticket '{ticket_id}' (ticket claimed by '{ticket.assigned_to}')."
            )

        # Retrieve unmasked customer attributes from repo redaction_maps (REQ-STORE-009)
        unmasked: Dict[str, Any] = {}
        maps: List[Any] = []
        if hasattr(self.repo, "get_redaction_maps"):
            maps = self.repo.get_redaction_maps(target_uuid)
        elif hasattr(self.repo, "get_redaction_map"):
            single_map = self.repo.get_redaction_map(target_uuid)
            if single_map is not None:
                maps = [single_map]

        if maps:
            for rmap in reversed(maps):
                raw_data = rmap.encrypted_mappings
                if self.cipher is not None:
                    try:
                        decrypted = self.cipher.decrypt(
                            raw_data, aad=str(target_uuid).encode("utf-8")
                        )
                        parsed = json.loads(decrypted.decode("utf-8"))
                        if isinstance(parsed, dict):
                            unmasked.update(parsed)
                    except Exception as err:
                        logger.warning(
                            "Failed to decrypt redaction map %s for ticket %s: %s",
                            getattr(rmap, "map_id", "unknown"),
                            str(ticket_id),
                            err,
                        )
                else:
                    try:
                        parsed = json.loads(
                            raw_data.decode("utf-8")
                            if isinstance(raw_data, (bytes, memoryview))
                            else str(raw_data)
                        )
                        if isinstance(parsed, dict):
                            unmasked.update(parsed)
                    except Exception:
                        logger.warning(
                            "No cipher configured and redaction map not valid JSON for ticket %s",
                            str(ticket_id),
                        )

        # Record immutable audit event (REQ-AGENT-009)
        record_pii_reidentification_event(
            repo=self.repo,
            ticket_id=target_uuid,
            operator_id=operator_id,
            fields_unmasked=list(unmasked.keys()),
        )

        return {"unmasked_fields": unmasked}
