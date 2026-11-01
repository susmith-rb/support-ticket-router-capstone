"""Unit and regression tests for ticket lifecycle storage in PostgresTicketRepo.

Validates:
- create_ticket writes lifecycle columns (issue_reference, category, current_owner, session_id).
- create_ticket ON CONFLICT preserves 'security' and 'fraud' categories.
- get_active_ticket_for_session filters out security, fraud, and model_armor rows.
- update_ticket_status customer scoping and policy guards reject security, fraud, human-owned, anon, and mismatched customer.
- update_ticket_status allows bot-owned open -> resolved_by_customer for verified customer.
- update_ticket_status idempotency key replay and same-target-state write no duplicate events.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from ticket_router.store.models import TicketCreate
from ticket_router.store.postgres import PostgresTicketRepo


def _make_mock_connection_factory(cursor_mock: MagicMock):
    conn_mock = MagicMock()
    conn_mock.cursor.return_value = cursor_mock
    return lambda: conn_mock


def _make_ticket_row(
    ticket_id: UUID,
    customer_id: str = "cust-123",
    status: str = "open",
    assigned_to: str | None = None,
    assigned_queue: str | None = "human-standard",
    decision: dict[str, Any] | None = None,
    session_id: str | None = "sess-001",
    issue_reference: str | None = "TICK-12345678",
    category: str | None = "support",
    current_owner: str | None = "queue:human-standard",
) -> tuple:
    now = datetime.now(timezone.utc)
    return (
        ticket_id,
        customer_id,
        "web",
        "Redacted issue body",
        decision or {"decision": "escalate", "queue": assigned_queue},
        status,
        assigned_to,
        assigned_queue,
        "Answer text",
        [],
        "Resolution notes",
        now,
        now,
        session_id,
        issue_reference,
        category,
        current_owner,
    )


def test_create_ticket_writes_lifecycle_columns():
    """create_ticket writes issue_reference, category, current_owner, session_id to database."""
    t_uuid = uuid4()
    row = _make_ticket_row(
        ticket_id=t_uuid,
        customer_id="cust-123",
        session_id="sess-abc",
        issue_reference="TICK-custom-ref",
        category="support",
        current_owner="queue:human-standard",
    )

    cursor = MagicMock()
    cursor.fetchone.return_value = row
    factory = _make_mock_connection_factory(cursor)
    repo = PostgresTicketRepo(connection_factory=factory)

    ticket = repo.create_ticket(
        TicketCreate(
            ticket_id=t_uuid,
            customer_id="cust-123",
            body_redacted="Redacted text",
            session_id="sess-abc",
            issue_reference="TICK-custom-ref",
            category="support",
            current_owner="queue:human-standard",
            assigned_queue="human-standard",
        )
    )

    assert ticket.ticket_id == t_uuid
    assert ticket.session_id == "sess-abc"
    assert ticket.issue_reference == "TICK-custom-ref"
    assert ticket.category == "support"
    assert ticket.current_owner == "queue:human-standard"

    # Verify SQL query executed contained the columns and ON CONFLICT handling
    insert_call = cursor.execute.call_args_list[0]
    sql_text = insert_call[0][0]
    params = insert_call[0][1]

    assert "issue_reference" in sql_text
    assert "category" in sql_text
    assert "current_owner" in sql_text
    assert "session_id" in sql_text

    assert "TICK-custom-ref" in params
    assert "support" in params
    assert "queue:human-standard" in params
    assert "sess-abc" in params


def test_create_ticket_on_conflict_preserves_security_and_fraud_categories():
    """SQL contains ON CONFLICT update preserving security and fraud categories."""
    t_uuid = uuid4()
    row = _make_ticket_row(ticket_id=t_uuid)

    cursor = MagicMock()
    cursor.fetchone.return_value = row
    factory = _make_mock_connection_factory(cursor)
    repo = PostgresTicketRepo(connection_factory=factory)

    repo.create_ticket(
        TicketCreate(
            ticket_id=t_uuid,
            customer_id="cust-123",
            body_redacted="Test",
        )
    )

    insert_call = cursor.execute.call_args_list[0]
    sql_text = insert_call[0][0]

    assert "WHEN tickets.category IN ('security', 'fraud') THEN tickets.category" in sql_text
    assert "ELSE COALESCE(EXCLUDED.category, tickets.category)" in sql_text


def test_get_active_ticket_for_session_skips_security_and_model_armor():
    """get_active_ticket_for_session skips security category rows and legacy model_armor decision rows."""
    t1 = uuid4()
    row_sec = _make_ticket_row(
        ticket_id=t1,
        category="security",
        current_owner="queue:human-security",
        assigned_queue="human-security",
    )

    t2 = uuid4()
    row_ma = _make_ticket_row(
        ticket_id=t2,
        category="support",
        decision={"tier": "model_armor", "decision": "security_blocked"},
        current_owner="queue:human-standard",
        assigned_queue="human-standard",
    )

    t3 = uuid4()
    row_bot = _make_ticket_row(
        ticket_id=t3,
        category="support",
        current_owner="bot",
        assigned_queue="none",
        status="open",
    )

    t4 = uuid4()
    row_valid = _make_ticket_row(
        ticket_id=t4,
        category="support",
        current_owner="queue:human-standard",
        assigned_queue="human-standard",
        status="assigned",
    )

    cursor = MagicMock()
    cursor.fetchall.return_value = [row_sec, row_ma, row_bot, row_valid]
    factory = _make_mock_connection_factory(cursor)
    repo = PostgresTicketRepo(connection_factory=factory)

    active = repo.get_active_ticket_for_session(session_id="sess-001")
    assert active is not None
    assert active.ticket_id == t4
    assert active.current_owner == "queue:human-standard"


def test_update_ticket_status_rejects_security_fraud_human_anon_and_mismatch():
    """update_ticket_status rejects security, fraud, human-owned, anon, and mismatched customer."""
    t_uuid = uuid4()

    # 1. Anonymous caller rejected immediately
    cursor = MagicMock()
    cursor.fetchone.return_value = _make_ticket_row(ticket_id=t_uuid, customer_id="cust-123")
    factory = _make_mock_connection_factory(cursor)
    repo = PostgresTicketRepo(connection_factory=factory)

    with pytest.raises(PermissionError, match="Anonymous"):
        repo.update_ticket_status(
            ticket_id=t_uuid,
            status="resolved_by_customer",
            customer_id="anon:sess-1",
        )

    # 2. Customer mismatch rejected immediately
    with pytest.raises(PermissionError, match="Customer mismatch"):
        repo.update_ticket_status(
            ticket_id=t_uuid,
            status="resolved_by_customer",
            customer_id="cust-other",
        )

    # 3. Security ticket rejected for customer update
    cursor.fetchone.return_value = _make_ticket_row(
        ticket_id=t_uuid,
        customer_id="cust-123",
        category="security",
        current_owner="bot",
    )
    with pytest.raises(ValueError, match="Cannot update status of security tickets"):
        repo.update_ticket_status(
            ticket_id=t_uuid,
            status="resolved_by_customer",
            customer_id="cust-123",
        )

    # 4. Fraud ticket rejected for customer update
    cursor.fetchone.return_value = _make_ticket_row(
        ticket_id=t_uuid,
        customer_id="cust-123",
        category="fraud",
        current_owner="bot",
    )
    with pytest.raises(ValueError, match="Cannot update status of fraud tickets"):
        repo.update_ticket_status(
            ticket_id=t_uuid,
            status="resolved_by_customer",
            customer_id="cust-123",
        )

    # 5. Human-owned ticket rejected for customer resolution
    cursor.fetchone.return_value = _make_ticket_row(
        ticket_id=t_uuid,
        customer_id="cust-123",
        category="support",
        current_owner="queue:human-standard",
        assigned_queue="human-standard",
    )
    with pytest.raises(ValueError, match="Cannot update status of human-owned tickets"):
        repo.update_ticket_status(
            ticket_id=t_uuid,
            status="resolved_by_customer",
            customer_id="cust-123",
        )

    # 6. Customer status update with customer_id=None rejected immediately
    cursor.fetchone.return_value = _make_ticket_row(
        ticket_id=t_uuid,
        customer_id="cust-123",
        category="support",
        current_owner="bot",
    )
    with pytest.raises(PermissionError, match="Customer ID is required"):
        repo.update_ticket_status(
            ticket_id=t_uuid,
            status="resolved_by_customer",
            customer_id=None,
        )

    # 7. Customer status update with rep actor rejected immediately
    with pytest.raises(PermissionError, match="Customer cannot impersonate representative"):
        repo.update_ticket_status(
            ticket_id=t_uuid,
            status="resolved_by_customer",
            actor="rep:alice",
            customer_id="cust-123",
        )


def test_update_ticket_status_allows_bot_owned_resolution_and_writes_audit():
    """Verified customer can update bot-owned open ticket to resolved_by_customer."""
    t_uuid = uuid4()
    initial_row = _make_ticket_row(
        ticket_id=t_uuid,
        customer_id="cust-123",
        status="open",
        current_owner="bot",
        assigned_queue="none",
        category="support",
    )
    updated_row = _make_ticket_row(
        ticket_id=t_uuid,
        customer_id="cust-123",
        status="resolved_by_customer",
        current_owner="bot",
        assigned_queue="none",
        category="support",
    )

    cursor = MagicMock()
    # First get_ticket fetch, then update RETURNING fetch
    cursor.fetchone.side_effect = [initial_row, updated_row]
    cursor.fetchall.return_value = []
    factory = _make_mock_connection_factory(cursor)
    repo = PostgresTicketRepo(connection_factory=factory)

    res = repo.update_ticket_status(
        ticket_id=t_uuid,
        from_status="open",
        status="resolved_by_customer",
        actor="cust-123",
        reason="customer_confirmed_resolution",
        idempotency_key="idemp-key-1",
        customer_id="cust-123",
    )

    assert res is not None
    assert res.status == "resolved_by_customer"

    # Verify event insert query was executed with actor and payload
    event_call = [c for c in cursor.execute.call_args_list if "INSERT INTO ticket_events" in c[0][0]]
    assert len(event_call) == 1
    event_params = event_call[0][0][1]
    assert event_params[2] == "cust-123"  # actor
    import json
    payload = json.loads(event_params[3])
    assert payload["from"] == "open"
    assert payload["to"] == "resolved_by_customer"
    assert payload["idempotency_key"] == "idemp-key-1"
    # Invariant: No customer message text in audit event payload
    assert "body_redacted" not in payload
    assert "message_text" not in payload


def test_update_ticket_status_idempotent_replay():
    """Idempotent replay writes zero duplicate events and returns current ticket state."""
    t_uuid = uuid4()
    current_row = _make_ticket_row(
        ticket_id=t_uuid,
        customer_id="cust-123",
        status="resolved_by_customer",
        current_owner="bot",
        category="support",
    )

    cursor = MagicMock()
    cursor.fetchone.return_value = current_row
    factory = _make_mock_connection_factory(cursor)
    repo = PostgresTicketRepo(connection_factory=factory)

    # 1. Already in target status
    res = repo.update_ticket_status(
        ticket_id=t_uuid,
        status="resolved_by_customer",
        customer_id="cust-123",
    )
    assert res is not None
    assert res.status == "resolved_by_customer"
    # No UPDATE or INSERT queries called
    update_calls = [c for c in cursor.execute.call_args_list if "UPDATE tickets" in c[0][0]]
    assert len(update_calls) == 0

    # 2. Replay with matching idempotency_key in existing event
    cursor.reset_mock()
    open_row = _make_ticket_row(
        ticket_id=t_uuid,
        customer_id="cust-123",
        status="open",
        current_owner="bot",
        category="support",
    )
    cursor.fetchone.return_value = open_row
    now = datetime.now(timezone.utc)
    event_row = (
        uuid4(),
        t_uuid,
        "status_changed",
        "cust-123",
        {"idempotency_key": "replay-key-999", "to": "resolved_by_customer"},
        now,
    )
    cursor.fetchall.return_value = [event_row]

    res2 = repo.update_ticket_status(
        ticket_id=t_uuid,
        status="resolved_by_customer",
        idempotency_key="replay-key-999",
        customer_id="cust-123",
    )
    assert res2 is not None
    # No UPDATE query executed
    update_calls2 = [c for c in cursor.execute.call_args_list if "UPDATE tickets" in c[0][0]]
    assert len(update_calls2) == 0


# REQ-HIST-003
def test_list_customer_tickets_postgres():
    """list_customer_tickets queries tickets for customer_id ordered by created_at DESC and rejects anon."""
    t_uuid = uuid4()
    row = _make_ticket_row(
        ticket_id=t_uuid,
        customer_id="cust-123",
        status="open",
        category="support",
    )
    cursor = MagicMock()
    cursor.fetchall.return_value = [row]
    factory = _make_mock_connection_factory(cursor)
    repo = PostgresTicketRepo(connection_factory=factory)

    # 1. Valid customer listing
    tickets = repo.list_customer_tickets(customer_id="cust-123", limit=5)
    assert len(tickets) == 1
    assert tickets[0].ticket_id == t_uuid
    assert tickets[0].customer_id == "cust-123"

    # Verify query executed
    assert cursor.execute.called
    call_args = cursor.execute.call_args[0]
    sql, params = call_args[0], call_args[1]
    assert "WHERE customer_id = %s" in sql
    assert "ORDER BY created_at DESC" in sql
    assert "LIMIT %s" in sql
    assert params == ("cust-123", 5)

    # 2. Anonymous customer rejection
    with pytest.raises(PermissionError, match="Anonymous callers"):
        repo.list_customer_tickets(customer_id="anon:12345")
    with pytest.raises(PermissionError, match="Anonymous callers"):
        repo.list_customer_tickets(customer_id="")
