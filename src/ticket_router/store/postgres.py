"""PostgreSQL implementation of TicketRepository and OrderRepository.

Implements REQ-STORE-006: Concrete storage layer using pg8000 + Cloud SQL Connector,
enforcing idempotent creation, atomic claims, and Postgres RLS for order isolation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from uuid import UUID, uuid4

from ticket_router.store.interfaces import OrderRepository, TicketRepository
from ticket_router.store.models import Order, RedactionMap, Ticket, TicketCreate, TicketEvent

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
        ON CONFLICT (ticket_id) DO UPDATE SET updated_at = tickets.updated_at
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
        t_id = str(ticket_id)
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
        t_id = str(ticket_id)
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
        t_id = str(ticket_id)
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
        t_id = str(ticket_id)
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
        t_id = str(ticket_id)
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
        t_id = str(ticket_id)
        query = """
        SELECT map_id, ticket_id, customer_id, encrypted_mappings, key_id, created_at, expires_at
        FROM redaction_maps
        WHERE ticket_id = %s;
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

    def save_redaction_map(self, redaction_map: RedactionMap) -> RedactionMap:
        query = """
        INSERT INTO redaction_maps (
            map_id, ticket_id, customer_id, encrypted_mappings, key_id, created_at, expires_at
        ) VALUES (
            %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '30 days'
        )
        ON CONFLICT (map_id) DO UPDATE SET
            encrypted_mappings = EXCLUDED.encrypted_mappings,
            key_id = EXCLUDED.key_id
        RETURNING map_id, ticket_id, customer_id, encrypted_mappings, key_id, created_at, expires_at;
        """
        conn = self._connection_factory()
        cursor = conn.cursor()
        try:
            cursor.execute(
                query,
                (
                    str(redaction_map.map_id),
                    str(redaction_map.ticket_id),
                    redaction_map.customer_id,
                    redaction_map.encrypted_mappings,
                    redaction_map.key_id,
                ),
            )
            row = cursor.fetchone()
            conn.commit()
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
