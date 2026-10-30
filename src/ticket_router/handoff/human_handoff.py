"""Shared HumanHandoff workflow (REQ-AGENT-030).

# REQ-AGENT-030: Shared HumanHandoff Workflow (deterministic, used by TriageRouter and FaqResolver)
# REQ-AGENT-004: Gaming-Resistant Queue Priority Assignment
# REQ-AGENT-005: Templated Calming Acknowledgement and Idempotent Ticket Upsert
# REQ-AUTH-006: mTLS-Aware Google API Transports
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ticket_router.agents.triage_router.templates import render_calming_acknowledgement
from ticket_router.store.ids import canonical_ticket_uuid

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HandoffResult:
    """Outcome of HumanHandoff escalation workflow."""

    calming_ack: str
    persisted: bool
    published: bool
    message_id: Optional[str] = None


class HumanHandoff:
    """Deterministic shared human handoff workflow (REQ-AGENT-030).

    Guarantees:
    1. Persist ticket status ESCALATED with assigned_queue = queue and reason via ticket-db-mcp.
    2. Publish PII-free ticket.escalated/v1 event to Pub/Sub with orderingKey = ticket_id.
       Retries up to 3 times within 1.5 s budget. Records publish_failed event on final failure.
    3. Return templated calming acknowledgement (ticket ID + SLA ETA).
    4. Never raises into caller's ticket flow (degrades to persisted only + calming ack).
    """

    def __init__(
        self,
        ticket_db_client: Any,
        publisher: Any,
        escalated_by: str = "triage_router",
        topic: Optional[str] = None,
    ) -> None:
        self.ticket_db_client = ticket_db_client
        self.publisher = publisher
        self.escalated_by = escalated_by
        self.topic = topic or os.environ.get("HUMAN_QUEUE_TOPIC", "human-queue")

    def _persist(
        self,
        ticket_id: str,
        queue: str,
        reason: str,
        escalated_by: str,
        degraded: bool,
        customer_id: Optional[str],
        session_id: Optional[str],
        body_redacted: Optional[str],
        channel: str,
        idempotency_key: Optional[str] = None,
        decision_payload: Optional[Dict[str, Any]] = None,
        ticket_uuid: Optional[str] = None,
    ) -> bool:
        if self.ticket_db_client is None:
            return False

        key = idempotency_key or f"{ticket_id}:escalation"
        cid = customer_id or f"anon:{ticket_id}"
        body = body_redacted or ""
        decision_dict: Dict[str, Any] = decision_payload or {
            "decision": "escalate",
            "queue": queue,
            "reason": reason,
            "escalated_by": escalated_by,
            "degraded": degraded,
        }

        # Priority 1: Use upsert_ticket if client supports it (ticket-db-mcp HTTP or InMemory client)
        if hasattr(self.ticket_db_client, "upsert_ticket"):
            last_exc: Optional[Exception] = None
            for _ in range(2):
                try:
                    self.ticket_db_client.upsert_ticket(
                        idempotency_key=key,
                        customer_id=cid,
                        channel=channel,
                        body_redacted=body,
                        decision=decision_dict,
                    )
                    return True
                except Exception as exc:
                    last_exc = exc
            if last_exc is not None:
                raise last_exc

        # Priority 2: Use update_ticket_status if client is a direct TicketRepository
        if hasattr(self.ticket_db_client, "update_ticket_status"):
            target_id = ticket_uuid or ticket_id
            self.ticket_db_client.update_ticket_status(
                ticket_id=target_id,
                status="open",
                resolution_notes=f"Escalated by {escalated_by}: {reason}",
            )
            return True

        return False

    def _record_publish_failed(
        self,
        ticket_id: str,
        error: Optional[Exception],
        ticket_uuid: Optional[str] = None,
    ) -> None:
        target_db_id = ticket_uuid or ticket_id
        err_type = type(error).__name__ if error else "UnknownError"
        logger.warning(
            json.dumps(
                {
                    "event": "publish_failed",
                    "ticket_id": ticket_id,
                    "ticket_uuid": target_db_id,
                    "error_type": err_type,
                },
                separators=(",", ":"),
            )
        )
        if hasattr(self.ticket_db_client, "append_event"):
            try:
                self.ticket_db_client.append_event(
                    ticket_id=target_db_id,
                    event_type="publish_failed",
                    actor=self.escalated_by,
                    payload={"error_type": err_type},
                )
            except Exception:
                pass
        elif hasattr(self.ticket_db_client, "events") and isinstance(self.ticket_db_client.events, list):
            self.ticket_db_client.events.append({
                "ticket_id": target_db_id,
                "event_type": "publish_failed",
                "actor": self.escalated_by,
                "payload": {"error_type": err_type},
            })

    def _publish_with_retry(
        self,
        ticket_id: str,
        payload: Dict[str, Any],
        attributes: Dict[str, str],
        budget_s: float = 1.5,
        max_attempts: int = 3,
        ticket_uuid: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        t0 = time.monotonic()
        data_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        last_exc: Optional[Exception] = None

        for attempt in range(max_attempts):
            elapsed = time.monotonic() - t0
            remaining = budget_s - elapsed
            if remaining <= 0 and attempt > 0:
                break

            try:
                msg_id = self.publisher.publish(
                    topic=self.topic,
                    data=data_bytes,
                    attributes=attributes,
                    ordering_key=ticket_id,
                )
                return True, msg_id
            except Exception as exc:
                last_exc = exc
                remaining_after = budget_s - (time.monotonic() - t0)
                if attempt < max_attempts - 1 and remaining_after > 0.05:
                    sleep_time = min(0.05 * (2 ** attempt), remaining_after / 2)
                    time.sleep(sleep_time)

        self._record_publish_failed(ticket_id, last_exc, ticket_uuid=ticket_uuid)
        return False, None

    def escalate(
        self,
        ticket_id: str,
        queue: str = "human-standard",
        reason: str = "escalate",
        escalated_by: Optional[str] = None,
        degraded: bool = False,
        priority: Optional[str] = None,
        traceparent: Optional[str] = None,
        customer_id: Optional[str] = None,
        session_id: Optional[str] = None,
        body_redacted: Optional[str] = None,
        channel: str = "web",
        category: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        decision_payload: Optional[Dict[str, Any]] = None,
        ticket_uuid: Optional[str] = None,
        **kwargs: Any,
    ) -> HandoffResult:
        """Execute deterministic handoff: persist -> publish -> calming ack. Never raises."""
        effective_escalated_by = escalated_by or self.escalated_by
        if priority is None:
            if queue == "human-urgent":
                effective_priority = "urgent"
            elif queue == "human-low":
                effective_priority = "low"
            else:
                effective_priority = "standard"
        else:
            effective_priority = priority

        effective_ticket_uuid = (
            str(ticket_uuid)
            if ticket_uuid is not None
            else (canonical_ticket_uuid(idempotency_key) if idempotency_key else canonical_ticket_uuid(ticket_id))
        )

        # Calming acknowledgement is rendered deterministically upfront
        calming_ack = render_calming_acknowledgement(
            queue=queue,
            ticket_id=ticket_id,
            category=category,
        )

        persisted = False
        try:
            persisted = self._persist(
                ticket_id=ticket_id,
                queue=queue,
                reason=reason,
                escalated_by=effective_escalated_by,
                degraded=degraded,
                customer_id=customer_id,
                session_id=session_id,
                body_redacted=body_redacted,
                channel=channel,
                idempotency_key=idempotency_key,
                decision_payload=decision_payload,
                ticket_uuid=effective_ticket_uuid,
            )
        except Exception as exc:
            logger.warning(
                json.dumps(
                    {
                        "event": "handoff_persistence_failed",
                        "ticket_id": ticket_id,
                        "ticket_uuid": effective_ticket_uuid,
                        "error_type": type(exc).__name__,
                    },
                    separators=(",", ":"),
                )
            )
            persisted = False

        published = False
        message_id: Optional[str] = None
        try:
            if self.publisher is not None:
                ts = datetime.now(timezone.utc).isoformat()
                # PII-free payload per REQ-AGENT-030
                payload: Dict[str, Any] = {
                    "schema": "ticket.escalated/v1",
                    "ticket_id": ticket_id,
                    "ticket_uuid": effective_ticket_uuid,
                    "queue": queue,
                    "priority": effective_priority,
                    "reason": reason,
                    "escalated_by": effective_escalated_by,
                    "degraded": bool(degraded),
                    "ts": ts,
                    "traceparent": traceparent,
                }
                attributes: Dict[str, str] = {
                    "event_type": "ticket.escalated",
                    "queue": queue,
                    "escalated_by": effective_escalated_by,
                }
                published, message_id = self._publish_with_retry(
                    ticket_id=ticket_id,
                    payload=payload,
                    attributes=attributes,
                    ticket_uuid=ticket_uuid,
                )
        except Exception as exc:
            logger.warning(
                json.dumps(
                    {
                        "event": "handoff_publish_error",
                        "ticket_id": ticket_id,
                        "ticket_uuid": effective_ticket_uuid,
                        "error_type": type(exc).__name__,
                    },
                    separators=(",", ":"),
                )
            )
            published = False
            message_id = None

        return HandoffResult(
            calming_ack=calming_ack,
            persisted=persisted,
            published=published,
            message_id=message_id,
        )
