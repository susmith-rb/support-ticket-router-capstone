"""Regression tests for pg8000 native type decoding in PostgresTicketRepo and PostgresOrderRepo.

# REQ-STORE-006: Concrete storage layer for tickets, orders, and redaction maps
# REQ-AGENT-009: HumanQueue Workflow and Audited PII Re-Identification
# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List
from unittest.mock import MagicMock
from uuid import uuid4

from ticket_router.store.models import RedactionMap, TicketCreate
from ticket_router.store.postgres import (
    PostgresOrderRepo,
    PostgresTicketRepo,
    _decode_json,
    _parse_datetime,
)


def _make_mock_connection_factory(cursor_mock: MagicMock):
    conn_mock = MagicMock()
    conn_mock.cursor.return_value = cursor_mock
    return lambda: conn_mock


# REQ-STORE-006
def test_decode_json_tolerant():
    """Verify _decode_json handles dict, list, string, bytes, and None."""
    assert _decode_json(None) is None
    assert _decode_json({"a": 1}) == {"a": 1}
    assert _decode_json([1, 2, 3]) == [1, 2, 3]
    assert _decode_json('{"key": "value"}') == {"key": "value"}
    assert _decode_json(b'{"key": "bytes"}') == {"key": "bytes"}
    assert _decode_json("") is None
    assert _decode_json("   ") is None
    assert _decode_json("invalid-json") is None


# REQ-STORE-006
def test_parse_datetime_timezone_safety():
    """Verify _parse_datetime handles timezone-aware and naive datetimes safely."""
    now_utc = datetime.now(timezone.utc)
    parsed_utc = _parse_datetime(now_utc)
    assert parsed_utc.tzinfo is not None

    naive = datetime(2026, 10, 27, 3, 0, 0)
    parsed_naive = _parse_datetime(naive)
    assert parsed_naive.tzinfo == timezone.utc

    iso_str = "2026-10-27T03:00:00Z"
    parsed_str = _parse_datetime(iso_str)
    assert parsed_str.tzinfo is not None


# REQ-STORE-006
def test_postgres_ticket_repo_reads_native_pg8000_dict_types():
    """Verify PostgresTicketRepo methods decode native dict/list rows without calling json.loads on dicts."""
    now = datetime.now(timezone.utc)
    t_uuid = uuid4()
    decision_dict = {"decision": "escalate", "queue": "human-urgent", "confidence": 0.95}
    citations_list: List[dict[str, Any]] = [{"source": "faq", "quote": "Refund policy"}]

    # pg8000 returns native Python types for UUID, JSONB, and TIMESTAMPTZ
    ticket_row = (
        t_uuid,
        "cust-123",
        "web",
        "Redacted text",
        decision_dict,  # ALREADY A DICT from pg8000 jsonb
        "open",
        "agent-alice",
        "human-urgent",
        "Answer text",
        citations_list,  # ALREADY A LIST from pg8000 jsonb
        "Resolution notes",
        now,
        now,
    )

    cursor = MagicMock()
    cursor.fetchone.return_value = ticket_row
    cursor.fetchall.return_value = [ticket_row]
    factory = _make_mock_connection_factory(cursor)
    repo = PostgresTicketRepo(connection_factory=factory)

    # 1. create_ticket
    created = repo.create_ticket(
        TicketCreate(
            ticket_id=t_uuid,
            customer_id="cust-123",
            body_redacted="Redacted text",
            decision=decision_dict,
        )
    )
    assert created.ticket_id == t_uuid
    assert created.decision == decision_dict
    assert created.citations == citations_list

    # 2. get_ticket
    fetched = repo.get_ticket(t_uuid)
    assert fetched is not None
    assert fetched.ticket_id == t_uuid
    assert fetched.decision == decision_dict
    assert fetched.citations == citations_list

    # 3. list_open_tickets
    open_tickets = repo.list_open_tickets(limit=10, queue="human-urgent")
    assert len(open_tickets) == 1
    assert open_tickets[0].ticket_id == t_uuid
    assert open_tickets[0].decision == decision_dict

    # 4. update_ticket_status
    updated = repo.update_ticket_status(
        ticket_id=t_uuid,
        status="resolved",
        resolution_notes="Resolved.",
    )
    assert updated is not None
    assert updated.decision == decision_dict

    # 5. claim_ticket
    cursor.fetchone.return_value = (t_uuid,)
    claimed = repo.claim_ticket(ticket_id=t_uuid, agent_id="agent-alice")
    assert claimed is True


# REQ-STORE-006
def test_postgres_event_repo_reads_native_pg8000_dict_payload():
    """Verify TicketEvent reading handles native dict payloads from pg8000."""
    now = datetime.now(timezone.utc)
    e_uuid = uuid4()
    t_uuid = uuid4()
    payload_dict = {"assigned_to": "agent-bob", "meta": {"foo": "bar"}}

    event_row = (
        e_uuid,
        t_uuid,
        "claimed",
        "agent-bob",
        payload_dict,  # ALREADY A DICT from pg8000
        now,
    )

    cursor = MagicMock()
    cursor.fetchone.return_value = event_row
    cursor.fetchall.return_value = [event_row]
    factory = _make_mock_connection_factory(cursor)
    repo = PostgresTicketRepo(connection_factory=factory)

    # append_event
    event = repo.append_event(ticket_id=t_uuid, event_type="claimed", actor="agent-bob", payload=payload_dict)
    assert event.event_id == e_uuid
    assert event.payload == payload_dict

    # list_events
    events = repo.list_events(ticket_id=t_uuid)
    assert len(events) == 1
    assert events[0].event_id == e_uuid
    assert events[0].payload == payload_dict


# REQ-STORE-006, REQ-AGENT-009
def test_postgres_redaction_map_reads_native_bytea():
    """Verify get_redaction_map and save_redaction_map handle native bytea and memoryview."""
    now = datetime.now(timezone.utc)
    m_uuid = uuid4()
    t_uuid = uuid4()
    secret_bytes = b'{"email": "secret@example.com"}'

    # Row with memoryview (as sometimes returned by DB-API drivers for BYTEA)
    map_row = (
        m_uuid,
        t_uuid,
        "cust-555",
        memoryview(secret_bytes),
        "secret-manager-v1",
        now,
        now,
    )

    cursor = MagicMock()
    cursor.fetchone.return_value = map_row
    factory = _make_mock_connection_factory(cursor)
    repo = PostgresTicketRepo(connection_factory=factory)

    # get_redaction_map
    rmap = repo.get_redaction_map(t_uuid)
    assert rmap is not None
    assert rmap.map_id == m_uuid
    assert rmap.encrypted_mappings == secret_bytes

    # save_redaction_map
    saved = repo.save_redaction_map(
        RedactionMap(
            map_id=m_uuid,
            ticket_id=t_uuid,
            customer_id="cust-555",
            encrypted_mappings=secret_bytes,
        )
    )
    assert saved.map_id == m_uuid
    assert saved.encrypted_mappings == secret_bytes


# REQ-STORE-006, REQ-AGENT-010
def test_postgres_order_repo_reads_native_list_items():
    """Verify PostgresOrderRepo handles native list items from pg8000."""
    now = datetime.now(timezone.utc)
    items_list = [{"item_id": "item-1", "sku": "WIDGET-01", "quantity": 2}]

    order_row = (
        "ord-777",
        "cust-888",
        "shipped",
        items_list,  # ALREADY A LIST from pg8000
        "TRK12345",
        "FedEx",
        4999,
        "USD",
        "Austin",
        "TX",
        now,
        now,
        now,
    )

    cursor = MagicMock()
    cursor.fetchone.return_value = order_row
    cursor.fetchall.return_value = [order_row]
    factory = _make_mock_connection_factory(cursor)
    repo = PostgresOrderRepo(connection_factory=factory)

    # get_order
    order = repo.get_order(order_id="ord-777", customer_id="cust-888")
    assert order is not None
    assert order.items == items_list
    assert order.total_cents == 4999

    # list_customer_orders
    orders = repo.list_customer_orders(customer_id="cust-888", limit=5)
    assert len(orders) == 1
    assert orders[0].items == items_list
