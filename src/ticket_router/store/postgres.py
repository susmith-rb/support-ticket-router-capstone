"""PostgreSQL implementation of TicketRepository and OrderRepository.

Implements REQ-STORE-006: Concrete storage layer using pg8000 + Cloud SQL Connector,
enforcing idempotent creation, atomic claims, and Postgres RLS for order isolation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from ticket_router.store.interfaces import CustomerRepository, OrderRepository, TicketRepository
from ticket_router.store.models import (
    Customer,
    CustomerResolution,
    IdentityEvent,
    Order,
    RedactionMap,
    Ticket,
    TicketCreate,
    TicketEvent,
    is_holding_ticket,
)

logger = logging.getLogger(__name__)


def _decode_json(val: Any) -> Any:
    """Tolerantly decode JSON/JSONB database values across drivers and mocks.

    pg8000 natively decodes JSONB columns into Python dict/list.
    Other drivers or mock rows may return serialized strings or bytes.
    """
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, (str, bytes, bytearray)):
        if isinstance(val, (bytes, bytearray)):
            val = val.decode("utf-8", errors="replace")
        val_str = val.strip()
        if not val_str:
            return None
        try:
            return json.loads(val_str)
        except Exception:
            return None
    return val


def _parse_datetime(dt_val: Any) -> datetime:
    if isinstance(dt_val, datetime):
        if dt_val.tzinfo is None:
            return dt_val.replace(tzinfo=timezone.utc)
        return dt_val
    if isinstance(dt_val, str):
        return datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
    return datetime.now(timezone.utc)


class PostgresTicketRepo(TicketRepository):
    """PostgreSQL storage backend for tickets and audit events."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connection_factory = connection_factory

    def _normalize_uuid(self, ticket_id: UUID | str) -> str:
        if isinstance(ticket_id, UUID):
            return str(ticket_id)
        try:
            return str(UUID(ticket_id))
        except (ValueError, AttributeError):
            return str(uuid5(NAMESPACE_URL, f"idempotency:{ticket_id}"))

    def create_ticket(self, ticket_create: TicketCreate) -> Ticket:
        """Create ticket idempotently.

        If a ticket with ticket_id already exists, returns the existing record.
        """
        ticket_id = ticket_create.ticket_id or uuid4()
        decision_json = (
            json.dumps(ticket_create.decision) if ticket_create.decision else None
        )

        query = """
        INSERT INTO tickets (
            ticket_id, customer_id, channel, body_redacted, decision,
            status, assigned_to, assigned_queue, created_at, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s::jsonb, 'open', %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        )
        ON CONFLICT (ticket_id) DO UPDATE SET
            updated_at = CURRENT_TIMESTAMP,
            assigned_queue = COALESCE(EXCLUDED.assigned_queue, tickets.assigned_queue),
            decision = COALESCE(EXCLUDED.decision, tickets.decision)
        RETURNING ticket_id, customer_id, channel, body_redacted, decision,
                  status, assigned_to, assigned_queue, answer_text, citations,
                  resolution_notes, created_at, updated_at;
        """
        event_query = """
        INSERT INTO ticket_events (
            event_id, ticket_id, event_type, actor, payload, created_at
        ) VALUES (
            %s, %s, 'created', 'router-agent', %s::jsonb, CURRENT_TIMESTAMP
        );
        """

        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(
                query,
                (
                    str(ticket_id),
                    ticket_create.customer_id,
                    ticket_create.channel,
                    ticket_create.body_redacted,
                    decision_json,
                    ticket_create.assigned_to,
                    ticket_create.assigned_queue,
                ),
            )
            row = cursor.fetchone()

            # Record creation audit event
            event_id = uuid4()
            event_payload = json.dumps({"channel": ticket_create.channel})
            cursor.execute(event_query, (str(event_id), str(ticket_id), event_payload))

            conn.commit()

            return Ticket(
                ticket_id=UUID(str(row[0])),
                customer_id=str(row[1]),
                channel=str(row[2]),
                body_redacted=str(row[3]),
                decision=_decode_json(row[4]),
                status=str(row[5]),
                assigned_to=str(row[6]) if row[6] else None,
                assigned_queue=str(row[7]) if row[7] else None,
                answer_text=str(row[8]) if row[8] else None,
                citations=_decode_json(row[9]),
                resolution_notes=str(row[10]) if row[10] else None,
                created_at=_parse_datetime(row[11]),
                updated_at=_parse_datetime(row[12]),
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def get_ticket(self, ticket_id: UUID | str) -> Optional[Ticket]:
        t_id = self._normalize_uuid(ticket_id)
        query = """
        SELECT ticket_id, customer_id, channel, body_redacted, decision,
               status, assigned_to, assigned_queue, answer_text, citations,
               resolution_notes, created_at, updated_at
        FROM tickets
        WHERE ticket_id = %s;
        """
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(query, (t_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return Ticket(
                ticket_id=UUID(str(row[0])),
                customer_id=str(row[1]),
                channel=str(row[2]),
                body_redacted=str(row[3]),
                decision=_decode_json(row[4]),
                status=str(row[5]),
                assigned_to=str(row[6]) if row[6] else None,
                assigned_queue=str(row[7]) if row[7] else None,
                answer_text=str(row[8]) if row[8] else None,
                citations=_decode_json(row[9]),
                resolution_notes=str(row[10]) if row[10] else None,
                created_at=_parse_datetime(row[11]),
                updated_at=_parse_datetime(row[12]),
            )
        finally:
            cursor.close()
            conn.close()

    def list_open_tickets(
        self, limit: int = 50, queue: Optional[str] = None
    ) -> List[Ticket]:
        query = """
        SELECT ticket_id, customer_id, channel, body_redacted, decision,
               status, assigned_to, assigned_queue, answer_text, citations,
               resolution_notes, created_at, updated_at
        FROM tickets
        WHERE status = 'open'
        """
        params: List[Any] = []
        if queue:
            query += " AND assigned_queue = %s"
            params.append(queue)
        query += " ORDER BY created_at ASC LIMIT %s;"
        params.append(limit)

        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            return [
                Ticket(
                    ticket_id=UUID(str(row[0])),
                    customer_id=str(row[1]),
                    channel=str(row[2]),
                    body_redacted=str(row[3]),
                    decision=_decode_json(row[4]),
                    status=str(row[5]),
                    assigned_to=str(row[6]) if row[6] else None,
                    assigned_queue=str(row[7]) if row[7] else None,
                    answer_text=str(row[8]) if row[8] else None,
                    citations=_decode_json(row[9]),
                    resolution_notes=str(row[10]) if row[10] else None,
                    created_at=_parse_datetime(row[11]),
                    updated_at=_parse_datetime(row[12]),
                )
                for row in rows
            ]
        finally:
            cursor.close()
            conn.close()

    def claim_ticket(self, ticket_id: UUID | str, agent_id: str) -> bool:
        """Atomically claim ticket. Returns False if already claimed or resolved."""
        t_id = self._normalize_uuid(ticket_id)
        update_query = """
        UPDATE tickets
        SET status = 'assigned', assigned_to = %s, updated_at = CURRENT_TIMESTAMP
        WHERE ticket_id = %s AND status = 'open'
        RETURNING ticket_id;
        """
        event_query = """
        INSERT INTO ticket_events (
            event_id, ticket_id, event_type, actor, payload, created_at
        ) VALUES (
            %s, %s, 'claimed', %s, %s::jsonb, CURRENT_TIMESTAMP
        );
        """
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(update_query, (agent_id, t_id))
            row = cursor.fetchone()
            if not row:
                conn.commit()
                return False

            event_id = uuid4()
            payload = json.dumps({"assigned_to": agent_id})
            cursor.execute(event_query, (str(event_id), t_id, agent_id, payload))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def append_event(
        self,
        ticket_id: UUID | str,
        event_type: str,
        actor: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> TicketEvent:
        t_id = self._normalize_uuid(ticket_id)
        event_id = uuid4()
        payload_json = json.dumps(payload) if payload else None

        query = """
        INSERT INTO ticket_events (
            event_id, ticket_id, event_type, actor, payload, created_at
        ) VALUES (
            %s, %s, %s, %s, %s::jsonb, CURRENT_TIMESTAMP
        )
        RETURNING event_id, ticket_id, event_type, actor, payload, created_at;
        """
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(query, (str(event_id), t_id, event_type, actor, payload_json))
            row = cursor.fetchone()
            conn.commit()
            return TicketEvent(
                event_id=UUID(str(row[0])),
                ticket_id=UUID(str(row[1])),
                event_type=str(row[2]),
                actor=str(row[3]),
                payload=_decode_json(row[4]),
                created_at=_parse_datetime(row[5]),
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def list_events(self, ticket_id: UUID | str) -> List[TicketEvent]:
        t_id = self._normalize_uuid(ticket_id)
        query = """
        SELECT event_id, ticket_id, event_type, actor, payload, created_at
        FROM ticket_events
        WHERE ticket_id = %s
        ORDER BY created_at ASC;
        """
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(query, (t_id,))
            rows = cursor.fetchall()
            return [
                TicketEvent(
                    event_id=UUID(str(row[0])),
                    ticket_id=UUID(str(row[1])),
                    event_type=str(row[2]),
                    actor=str(row[3]),
                    payload=_decode_json(row[4]),
                    created_at=_parse_datetime(row[5]),
                )
                for row in rows
            ]
        finally:
            cursor.close()
            conn.close()

    def update_ticket_status(
        self,
        ticket_id: UUID | str,
        status: str,
        resolution_notes: Optional[str] = None,
        answer_text: Optional[str] = None,
        citations: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Ticket]:
        t_id = self._normalize_uuid(ticket_id)
        citations_json = json.dumps(citations) if citations else None
        query = """
        UPDATE tickets
        SET status = %s,
            resolution_notes = COALESCE(%s, resolution_notes),
            answer_text = COALESCE(%s, answer_text),
            citations = COALESCE(%s::jsonb, citations),
            updated_at = CURRENT_TIMESTAMP
        WHERE ticket_id = %s
        RETURNING ticket_id, customer_id, channel, body_redacted, decision,
                  status, assigned_to, assigned_queue, answer_text, citations,
                  resolution_notes, created_at, updated_at;
        """
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(
                query, (status, resolution_notes, answer_text, citations_json, t_id)
            )
            row = cursor.fetchone()
            if not row:
                conn.commit()
                return None

            event_query = """
            INSERT INTO ticket_events (event_id, ticket_id, event_type, actor, payload, created_at)
            VALUES (%s, %s, 'status_changed', 'system', %s::jsonb, CURRENT_TIMESTAMP);
            """
            cursor.execute(
                event_query,
                (
                    str(uuid4()),
                    t_id,
                    json.dumps({"status": status, "resolution_notes": resolution_notes}),
                ),
            )
            conn.commit()

            return Ticket(
                ticket_id=UUID(str(row[0])),
                customer_id=str(row[1]),
                channel=str(row[2]),
                body_redacted=str(row[3]),
                decision=_decode_json(row[4]),
                status=str(row[5]),
                assigned_to=str(row[6]) if row[6] else None,
                assigned_queue=str(row[7]) if row[7] else None,
                answer_text=str(row[8]) if row[8] else None,
                citations=_decode_json(row[9]),
                resolution_notes=str(row[10]) if row[10] else None,
                created_at=_parse_datetime(row[11]),
                updated_at=_parse_datetime(row[12]),
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def get_redaction_map(self, ticket_id: UUID | str) -> Optional[RedactionMap]:
        t_id = self._normalize_uuid(ticket_id)
        query = """
        SELECT map_id, ticket_id, customer_id, encrypted_mappings, key_id, created_at, expires_at
        FROM redaction_maps
        WHERE ticket_id = %s
        ORDER BY created_at DESC
        LIMIT 1;
        """
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(query, (t_id,))
            row = cursor.fetchone()
            if not row:
                return None
            encrypted_data = row[3]
            if isinstance(encrypted_data, memoryview):
                encrypted_data = bytes(encrypted_data)
            elif isinstance(encrypted_data, str):
                encrypted_data = encrypted_data.encode("utf-8")
            return RedactionMap(
                map_id=UUID(str(row[0])),
                ticket_id=UUID(str(row[1])),
                customer_id=str(row[2]),
                encrypted_mappings=encrypted_data,
                key_id=str(row[4]),
                created_at=_parse_datetime(row[5]),
                expires_at=_parse_datetime(row[6]) if row[6] else None,
            )
        finally:
            cursor.close()
            conn.close()

    def get_redaction_maps(self, ticket_id: UUID | str) -> List[RedactionMap]:
        t_id = self._normalize_uuid(ticket_id)
        query = """
        SELECT map_id, ticket_id, customer_id, encrypted_mappings, key_id, created_at, expires_at
        FROM redaction_maps
        WHERE ticket_id = %s
        ORDER BY created_at DESC;
        """
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(query, (t_id,))
            rows = cursor.fetchall()
            results: List[RedactionMap] = []
            for row in rows:
                encrypted_data = row[3]
                if isinstance(encrypted_data, memoryview):
                    encrypted_data = bytes(encrypted_data)
                elif isinstance(encrypted_data, str):
                    encrypted_data = encrypted_data.encode("utf-8")
                results.append(
                    RedactionMap(
                        map_id=UUID(str(row[0])),
                        ticket_id=UUID(str(row[1])),
                        customer_id=str(row[2]),
                        encrypted_mappings=encrypted_data,
                        key_id=str(row[4]),
                        created_at=_parse_datetime(row[5]),
                        expires_at=_parse_datetime(row[6]) if row[6] else None,
                    )
                )
            return results
        finally:
            cursor.close()
            conn.close()

    def save_redaction_map(self, redaction_map: RedactionMap) -> RedactionMap:
        # Plain INSERT without RETURNING or ON CONFLICT to respect ticket_rw INSERT-only grant (REQ-STORE-009)
        query = """
        INSERT INTO redaction_maps (
            map_id, ticket_id, customer_id, encrypted_mappings, key_id, created_at, expires_at
        ) VALUES (
            %s, %s, %s, %s, %s, COALESCE(%s, CURRENT_TIMESTAMP), COALESCE(%s, CURRENT_TIMESTAMP + INTERVAL '30 days')
        );
        """
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(
                query,
                (
                    str(redaction_map.map_id),
                    self._normalize_uuid(redaction_map.ticket_id),
                    redaction_map.customer_id,
                    redaction_map.encrypted_mappings,
                    redaction_map.key_id,
                    redaction_map.created_at,
                    redaction_map.expires_at,
                ),
            )
            conn.commit()
            return redaction_map
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def purge_expired_redaction_maps(self) -> int:
        # Purge expired redaction maps (REQ-STORE-010) under queue_rw role
        query = """
        DELETE FROM redaction_maps
        WHERE expires_at < CURRENT_TIMESTAMP;
        """
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(query)
            deleted_count = cursor.rowcount if cursor.rowcount is not None else 0
            conn.commit()
            return deleted_count
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def get_active_ticket_for_session(
        self, session_id: str, customer_id: Optional[str] = None
    ) -> Optional[Ticket]:
        """Fetch the most recent active holding ticket for a conversation session (REQ-AGENT-033)."""
        query = """
        SELECT ticket_id, customer_id, channel, body_redacted, decision,
               status, assigned_to, assigned_queue, answer_text, citations,
               resolution_notes, created_at, updated_at
        FROM tickets
        WHERE (session_id = %s OR decision->>'session_id' = %s)
        """
        params: List[Any] = [session_id, session_id]
        if customer_id:
            query += " AND customer_id = %s"
            params.append(customer_id)
        query += " ORDER BY created_at DESC;"

        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            if not rows:
                return None

            latest_row = rows[0]
            if is_holding_ticket(
                str(latest_row[5]) if latest_row[5] else None,
                str(latest_row[7]) if latest_row[7] else None,
            ):
                return Ticket(
                    ticket_id=UUID(str(latest_row[0])),
                    customer_id=str(latest_row[1]),
                    channel=str(latest_row[2]),
                    body_redacted=str(latest_row[3]),
                    decision=_decode_json(latest_row[4]),
                    status=str(latest_row[5]),
                    assigned_to=str(latest_row[6]) if latest_row[6] else None,
                    assigned_queue=str(latest_row[7]) if latest_row[7] else None,
                    answer_text=str(latest_row[8]) if latest_row[8] else None,
                    citations=_decode_json(latest_row[9]),
                    resolution_notes=str(latest_row[10]) if latest_row[10] else None,
                    created_at=_parse_datetime(latest_row[11]),
                    updated_at=_parse_datetime(latest_row[12]),
                    session_id=session_id,
                )
            return None
        finally:
            cursor.close()
            conn.close()

    def append_customer_followup(
        self,
        ticket_id: UUID | str,
        body_redacted: str,
        customer_id: Optional[str] = None,
        traceparent: Optional[str] = None,
        message_hash: Optional[str] = None,
    ) -> Tuple[TicketEvent, bool]:
        """Append a customer follow-up audit event to an active ticket (REQ-AGENT-033)."""
        t_id = self._normalize_uuid(ticket_id)
        msg_hash = (
            message_hash
            or hashlib.sha256(body_redacted.encode("utf-8")).hexdigest()
        )

        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            # Verify ticket existence and customer scoping
            ticket_query = "SELECT ticket_id, customer_id FROM tickets WHERE ticket_id = %s;"
            cursor.execute(ticket_query, (t_id,))
            t_row = cursor.fetchone()
            if not t_row:
                raise KeyError(f"Ticket not found: {t_id}")
            if customer_id and str(t_row[1]) != customer_id:
                raise PermissionError(f"Customer mismatch for ticket {t_id}")

            # Idempotency check: scan existing customer_followup events for this ticket
            check_events_query = """
            SELECT event_id, ticket_id, event_type, actor, payload, created_at
            FROM ticket_events
            WHERE ticket_id = %s AND event_type = 'customer_followup';
            """
            cursor.execute(check_events_query, (t_id,))
            for ev_row in cursor.fetchall():
                ev_payload = _decode_json(ev_row[4]) or {}
                if isinstance(ev_payload, dict):
                    ev_body = ev_payload.get("body_redacted", "")
                    ev_hash = hashlib.sha256(ev_body.encode("utf-8")).hexdigest()
                    if ev_hash == msg_hash or ev_body == body_redacted:
                        return (
                            TicketEvent(
                                event_id=UUID(str(ev_row[0])),
                                ticket_id=UUID(str(ev_row[1])),
                                event_type=str(ev_row[2]),
                                actor=str(ev_row[3]),
                                payload=ev_payload,
                                created_at=_parse_datetime(ev_row[5]),
                            ),
                            False,
                        )

            # Insert new customer_followup event
            event_id = uuid4()
            payload = {
                "body_redacted": body_redacted,
                "traceparent": traceparent,
            }
            insert_query = """
            INSERT INTO ticket_events (event_id, ticket_id, event_type, actor, payload, created_at)
            VALUES (%s, %s, 'customer_followup', 'customer', %s::jsonb, CURRENT_TIMESTAMP)
            RETURNING event_id, ticket_id, event_type, actor, payload, created_at;
            """
            cursor.execute(insert_query, (str(event_id), t_id, json.dumps(payload)))
            ins_row = cursor.fetchone()

            # Update ticket updated_at
            cursor.execute("UPDATE tickets SET updated_at = CURRENT_TIMESTAMP WHERE ticket_id = %s;", (t_id,))
            conn.commit()

            return (
                TicketEvent(
                    event_id=UUID(str(ins_row[0])),
                    ticket_id=UUID(str(ins_row[1])),
                    event_type=str(ins_row[2]),
                    actor=str(ins_row[3]),
                    payload=_decode_json(ins_row[4]),
                    created_at=_parse_datetime(ins_row[5]),
                ),
                True,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()


class PostgresOrderRepo(OrderRepository):
    """PostgreSQL storage backend for orders, enforcing Postgres RLS for tenant isolation."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connection_factory = connection_factory

    def get_order(
        self, order_id: str, customer_id: Optional[str] = None
    ) -> Optional[Order]:
        """Fetch order by ID enforcing transaction-local customer_id setting."""
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            if customer_id:
                cursor.execute(
                    "SELECT set_config('app.customer_id', %s, true);", (customer_id,)
                )

            query = """
            SELECT order_id, customer_id, order_status, items, tracking_number,
                   shipping_carrier, total_cents, currency, shipping_address_city,
                   shipping_address_state, estimated_delivery, created_at, updated_at
            FROM orders_mock
            WHERE order_id = %s;
            """
            cursor.execute(query, (order_id,))
            row = cursor.fetchone()
            if not row:
                return None

            return Order(
                order_id=str(row[0]),
                customer_id=str(row[1]),
                order_status=str(row[2]),
                items=_decode_json(row[3]) or [],
                tracking_number=str(row[4]) if row[4] else None,
                shipping_carrier=str(row[5]) if row[5] else None,
                total_cents=int(row[6]),
                currency=str(row[7]),
                shipping_address_city=str(row[8]) if row[8] else None,
                shipping_address_state=str(row[9]) if row[9] else None,
                estimated_delivery=_parse_datetime(row[10]) if row[10] else None,
                created_at=_parse_datetime(row[11]),
                updated_at=_parse_datetime(row[12]),
            )
        finally:
            cursor.close()
            conn.close()

    def list_customer_orders(
        self, customer_id: str, limit: int = 10
    ) -> List[Order]:
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT set_config('app.customer_id', %s, true);", (customer_id,)
            )

            query = """
            SELECT order_id, customer_id, order_status, items, tracking_number,
                   shipping_carrier, total_cents, currency, shipping_address_city,
                   shipping_address_state, estimated_delivery, created_at, updated_at
            FROM orders_mock
            WHERE customer_id = %s
            ORDER BY created_at DESC
            LIMIT %s;
            """
            cursor.execute(query, (customer_id, limit))
            rows = cursor.fetchall()

            return [
                Order(
                    order_id=str(row[0]),
                    customer_id=str(row[1]),
                    order_status=str(row[2]),
                    items=_decode_json(row[3]) or [],
                    tracking_number=str(row[4]) if row[4] else None,
                    shipping_carrier=str(row[5]) if row[5] else None,
                    total_cents=int(row[6]),
                    currency=str(row[7]),
                    shipping_address_city=str(row[8]) if row[8] else None,
                    shipping_address_state=str(row[9]) if row[9] else None,
                    estimated_delivery=_parse_datetime(row[10]) if row[10] else None,
                    created_at=_parse_datetime(row[11]),
                    updated_at=_parse_datetime(row[12]),
                )
                for row in rows
            ]
        finally:
            cursor.close()
            conn.close()


class PostgresCustomerRepo(CustomerRepository):
    """PostgreSQL implementation of CustomerRepository for identity mapping and audit trail."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connection_factory = connection_factory

    def resolve(self, issuer: str, subject: str) -> CustomerResolution:
        """Resolve an IdP (issuer, subject) identity pair to a customer_id and verify active status.

        Joins customer_identities -> customers.
        Never raises for unlinked identities.
        """
        query = """
        SELECT ci.customer_id, c.status, ci.email_verified
        FROM customer_identities ci
        JOIN customers c ON ci.customer_id = c.customer_id
        WHERE ci.issuer = %s AND ci.subject = %s;
        """
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(query, (issuer, subject))
            row = cursor.fetchone()
            if row is None:
                return CustomerResolution(
                    customer_id=None,
                    status="unlinked",
                    linked=False,
                    email_verified=False,
                )

            customer_id = str(row[0])
            status = str(row[1]).lower() if row[1] else "active"
            email_verified = bool(row[2])
            return CustomerResolution(
                customer_id=customer_id,
                status=status,
                linked=True,
                email_verified=email_verified,
            )
        finally:
            cursor.close()
            conn.close()

    def append_identity_event(
        self,
        customer_id: Optional[str],
        issuer: Optional[str],
        event_type: str,
        actor: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> IdentityEvent:
        """Append an immutable audit event for customer identity lifecycle."""
        event_id = uuid4()
        payload_json = json.dumps(payload or {})
        query = """
        INSERT INTO identity_events (
            event_id, customer_id, issuer, event_type, actor, payload, created_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s::jsonb, CURRENT_TIMESTAMP
        )
        RETURNING event_id, customer_id, issuer, event_type, actor, payload, created_at;
        """
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(
                query,
                (
                    str(event_id),
                    customer_id,
                    issuer,
                    event_type,
                    actor,
                    payload_json,
                ),
            )
            conn.commit()
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("Failed to insert identity event row")
            return IdentityEvent(
                event_id=UUID(str(row[0])),
                customer_id=str(row[1]) if row[1] else None,
                issuer=str(row[2]) if row[2] else None,
                event_type=str(row[3]),
                actor=str(row[4]),
                payload=_decode_json(row[5]) or {},
                created_at=_parse_datetime(row[6]),
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def link_identity(
        self,
        issuer: str,
        subject: str,
        customer_id: str,
        actor: str,
        email_verified: bool = False,
    ) -> None:
        """Administratively link an IdP (issuer, subject) to a customer_id, upserting customer record and logging audit event."""
        upsert_customer_sql = """
        INSERT INTO customers (customer_id, status, created_at)
        VALUES (%s, 'active', CURRENT_TIMESTAMP)
        ON CONFLICT (customer_id) DO NOTHING;
        """
        upsert_identity_sql = """
        INSERT INTO customer_identities (issuer, subject, customer_id, email_verified, linked_at)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (issuer, subject) DO UPDATE
        SET customer_id = EXCLUDED.customer_id,
            email_verified = EXCLUDED.email_verified,
            linked_at = CURRENT_TIMESTAMP;
        """
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(upsert_customer_sql, (customer_id,))
            cursor.execute(
                upsert_identity_sql,
                (issuer, subject, customer_id, email_verified),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

        # Write identity event with no token/PII
        self.append_identity_event(
            customer_id=customer_id,
            issuer=issuer,
            event_type="link_identity",
            actor=actor,
            payload={"subject": subject, "email_verified": email_verified},
        )

    def unlink_identity(self, issuer: str, subject: str, actor: str) -> bool:
        """Administratively unlink an IdP identity mapping and log audit event."""
        delete_sql = """
        DELETE FROM customer_identities
        WHERE issuer = %s AND subject = %s
        RETURNING customer_id;
        """
        conn = self._connection_factory()
        cursor = conn.cursor()
        deleted_cust_id: Optional[str] = None
        try:
            cursor.execute(delete_sql, (issuer, subject))
            row = cursor.fetchone()
            if row:
                deleted_cust_id = str(row[0])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

        if deleted_cust_id is not None:
            self.append_identity_event(
                customer_id=deleted_cust_id,
                issuer=issuer,
                event_type="unlink_identity",
                actor=actor,
                payload={"subject": subject},
            )
            return True
        return False

    def suspend_customer(self, customer_id: str, actor: str) -> bool:
        """Administratively suspend a customer account and log audit event."""
        update_sql = """
        UPDATE customers
        SET status = 'suspended'
        WHERE customer_id = %s
        RETURNING customer_id;
        """
        conn = self._connection_factory()
        cursor = conn.cursor()
        suspended: bool = False
        try:
            cursor.execute(update_sql, (customer_id,))
            row = cursor.fetchone()
            if row:
                suspended = True
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

        if suspended:
            self.append_identity_event(
                customer_id=customer_id,
                issuer=None,
                event_type="suspend_customer",
                actor=actor,
                payload={"customer_id": customer_id},
            )
            return True
        return False

    def get_customer(self, customer_id: str) -> Optional[Customer]:
        """Fetch customer master record by customer_id."""
        query = "SELECT customer_id, status, created_at FROM customers WHERE customer_id = %s;"
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(query, (customer_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return Customer(
                customer_id=str(row[0]),
                status=str(row[1]),
                created_at=_parse_datetime(row[2]),
            )
        finally:
            cursor.close()
            conn.close()

