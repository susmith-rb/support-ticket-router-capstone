"""Explicit conversation state tracking assembled from backend ticket store.

# REQ-TURN-002: Explicit Conversation State Assembly from Ticket Store
# REQ-TURN-005: Customer Resolution Confirmation Protocol Driven by Explicit State
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ticket_router.store.models import effective_category, is_human_owned

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingConfirmation:
    """Explicit marker indicating that an affirmative resolution confirmation prompt is pending."""
    issue_reference: str
    ticket_uuid: str
    asked_at: Optional[str] = None


@dataclass(frozen=True)
class ActiveTicketInfo:
    """Compact summary of an active human-owned support ticket in this session."""
    issue_reference: str
    ticket_uuid: str
    status: str
    category: str
    owner: str


@dataclass(frozen=True)
class TurnSummary:
    """Compact summary of the previous turn's intent and decision."""
    intent: str
    decision: str


@dataclass(frozen=True)
class ConversationState:
    """Explicit, PII-free conversation state assembled every turn from the ticket store.

    Guarantees:
    - Zero customer message text is persisted or stored in state.
    - Confirmation handling is driven strictly by pending_confirmation.
    """
    pending_confirmation: Optional[PendingConfirmation] = None
    active_tickets: List[ActiveTicketInfo] = field(default_factory=list)
    last_turn: Optional[TurnSummary] = None

    def to_compact_dict(self) -> Dict[str, Any]:
        """Return a compact JSON-serializable dictionary for prompts and logging."""
        return {
            "pending_confirmation": (
                {
                    "issue_reference": self.pending_confirmation.issue_reference,
                    "ticket_uuid": self.pending_confirmation.ticket_uuid,
                    "asked_at": self.pending_confirmation.asked_at,
                }
                if self.pending_confirmation
                else None
            ),
            "active_tickets": [
                {
                    "issue_reference": t.issue_reference,
                    "ticket_uuid": t.ticket_uuid,
                    "status": t.status,
                    "category": t.category,
                    "owner": t.owner,
                }
                for t in self.active_tickets
            ],
            "last_turn": (
                {
                    "intent": self.last_turn.intent,
                    "decision": self.last_turn.decision,
                }
                if self.last_turn
                else None
            ),
        }

    def to_prompt_str(self) -> str:
        """Render a compact single-line JSON string for LLM prompts."""
        return json.dumps(self.to_compact_dict(), separators=(",", ":"))


def assemble_conversation_state(
    mcp_client: Any,
    session_id: str,
    customer_id: Optional[str] = None,
    customer_verified: bool = False,
    last_turn: Optional[TurnSummary] = None,
) -> ConversationState:
    """Assemble authoritative conversation state from the ticket database for session (REQ-TURN-002).

    Inspects active tickets and recent ticket events to discover pending confirmation prompts
    and active human holding tickets without storing raw customer messages.
    """
    pending_conf: Optional[PendingConfirmation] = None
    active_tickets: List[ActiveTicketInfo] = []
    extracted_last_turn: Optional[TurnSummary] = last_turn

    # 1. Query pending resolution confirmation (REQ-TURN-005)
    conf_ticket: Optional[Dict[str, Any]] = None
    if mcp_client is not None and hasattr(mcp_client, "get_pending_confirmation"):
        try:
            conf_resp = mcp_client.get_pending_confirmation(
                session_id=session_id,
                customer_id=customer_id,
            )
            if isinstance(conf_resp, dict):
                conf_ticket = conf_resp.get("ticket") or conf_resp
        except Exception as exc:
            logger.warning(
                "get_pending_confirmation failed during state assembly: %s", exc
            )
            conf_ticket = None

    if isinstance(conf_ticket, dict):
        tid_uuid = str(
            conf_ticket.get("ticket_id") or conf_ticket.get("id") or ""
        )
        issue_ref = str(conf_ticket.get("issue_reference") or tid_uuid)
        asked_at = conf_ticket.get("updated_at") or conf_ticket.get("created_at")
        if tid_uuid:
            pending_conf = PendingConfirmation(
                issue_reference=issue_ref,
                ticket_uuid=tid_uuid,
                asked_at=asked_at,
            )

    # 2. Query active holding tickets (REQ-AGENT-033)
    cand_ticket: Optional[Dict[str, Any]] = None
    if mcp_client is not None:
        try:
            cand_ticket = mcp_client.get_active_ticket_for_session(
                session_id=session_id,
                customer_id=customer_id,
            )
        except Exception as exc:
            logger.warning(
                "get_active_ticket_for_session failed during state assembly: %s", exc
            )
            cand_ticket = None

    if isinstance(cand_ticket, dict):
        t_data = (
            cand_ticket.get("ticket")
            if isinstance(cand_ticket.get("ticket"), dict)
            else cand_ticket
        )
        status = str(t_data.get("status") or "open").lower()
        cat = effective_category(t_data)
        raw_owner = t_data.get("current_owner")
        assigned_queue = str(
            t_data.get("assigned_queue") or t_data.get("queue") or ""
        )

        if raw_owner:
            owner = str(raw_owner)
        elif is_human_owned(t_data) or assigned_queue.startswith("human"):
            owner = f"queue:{assigned_queue}" if assigned_queue else "queue:human-standard"
        else:
            owner = "bot"

        tid_uuid = str(t_data.get("ticket_id") or t_data.get("id") or "")
        issue_ref = str(t_data.get("issue_reference") or tid_uuid)

        # Fallback check for pending resolution confirmation on bot ticket if not already found
        if (
            pending_conf is None
            and t_data.get("resolution_confirmation_requested")
            and status in ("open", "answered")
            and cat not in ("security", "fraud")
            and owner == "bot"
            and tid_uuid
        ):
            pending_conf = PendingConfirmation(
                issue_reference=issue_ref,
                ticket_uuid=tid_uuid,
                asked_at=t_data.get("updated_at") or t_data.get("created_at"),
            )

        # Check for active human-owned holding tickets
        is_held = (
            is_human_owned(t_data)
            or assigned_queue.startswith("human")
            or owner != "bot"
        )
        if (
            is_held
            and status
            not in ("resolved", "resolved_auto", "resolved_by_customer", "closed")
            and tid_uuid
        ):
            active_tickets.append(
                ActiveTicketInfo(
                    issue_reference=issue_ref,
                    ticket_uuid=tid_uuid,
                    status=status,
                    category=cat,
                    owner=owner,
                )
            )

        # Extract last turn summary if not already provided
        if extracted_last_turn is None:
            dec = t_data.get("decision")
            if isinstance(dec, str):
                try:
                    dec = json.loads(dec)
                except Exception:
                    dec = None
            if isinstance(dec, dict):
                p_intent = dec.get("predicted_intent")
                p_dec = dec.get("decision")
                if p_intent and p_dec:
                    extracted_last_turn = TurnSummary(
                        intent=str(p_intent), decision=str(p_dec)
                    )

    # Also extract last_turn from conf_ticket if not already set
    if extracted_last_turn is None and isinstance(conf_ticket, dict):
        dec = conf_ticket.get("decision")
        if isinstance(dec, str):
            try:
                dec = json.loads(dec)
            except Exception:
                dec = None
        if isinstance(dec, dict):
            p_intent = dec.get("predicted_intent")
            p_dec = dec.get("decision")
            if p_intent and p_dec:
                extracted_last_turn = TurnSummary(
                    intent=str(p_intent), decision=str(p_dec)
                )

    return ConversationState(
        pending_confirmation=pending_conf,
        active_tickets=active_tickets,
        last_turn=extracted_last_turn,
    )
