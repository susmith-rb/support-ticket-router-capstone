"""Unit tests for canonical identifier utilities.

# REQ-AGENT-005: Idempotent Ticket Upsert
# REQ-STORE-006: Lightweight thread-safe fakes for hermetic unit testing
"""

from __future__ import annotations

import uuid
from uuid import UUID

from ticket_router.store.fake import FakeTicketRepo
from ticket_router.store.ids import (
    canonical_ticket_uuid,
    issue_reference,
    legacy_session_issue_reference,
)
from ticket_router.store.models import TicketCreate


def test_canonical_ticket_uuid() -> None:
    raw_uuid = str(uuid.uuid4())
    assert canonical_ticket_uuid(raw_uuid) == raw_uuid

    parsed_uuid = UUID(raw_uuid)
    assert canonical_ticket_uuid(parsed_uuid) == raw_uuid

    # Non-uuid string determinism
    u1 = canonical_ticket_uuid("sess-123:msg-456")
    u2 = canonical_ticket_uuid("sess-123:msg-456")
    assert u1 == u2
    assert UUID(u1)  # Valid UUID


def test_issue_reference_formatting() -> None:
    test_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    assert issue_reference(test_uuid) == "TICK-A1B2C3D4"

    # UUID object
    assert issue_reference(UUID(test_uuid)) == "TICK-A1B2C3D4"

    # String without dashes
    assert issue_reference("a1b2c3d4e5f6") == "TICK-A1B2C3D4"

    # Already formatted with TICK-
    assert issue_reference("TICK-a1b2c3d4") == "TICK-A1B2C3D4"
    assert issue_reference("TICK-custom-ref") == "TICK-CUSTOMRE"


def test_legacy_session_issue_reference() -> None:
    assert legacy_session_issue_reference("session-xyz-123") == "TICK-session-"
    assert legacy_session_issue_reference("1234567890") == "TICK-12345678"


def test_fake_repo_case_insensitive_issue_reference() -> None:
    repo = FakeTicketRepo()
    ticket = repo.create_ticket(
        TicketCreate(
            ticket_id=uuid.uuid4(),
            customer_id="cust-123",
            channel="web",
            body_redacted="My issue description",
            issue_reference="TICK-ABC12345",
        )
    )

    # Lookup with exact match
    found = repo.get_ticket("TICK-ABC12345")
    assert found is not None
    assert found.ticket_id == ticket.ticket_id

    # Lookup with lowercase
    found_lower = repo.get_ticket("tick-abc12345")
    assert found_lower is not None
    assert found_lower.ticket_id == ticket.ticket_id

    # Update status with lowercase reference
    updated = repo.update_ticket_status(
        ticket_id="tick-abc12345",
        status="in_progress",
    )
    assert updated is not None
    assert updated.status == "in_progress"
