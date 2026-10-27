# tests/store/test_repo.py
# Implements REQ-STORE-006: Repository layer abstraction, in-memory fakes, and RLS customer isolation.
"""Unit tests for ticket and order repositories using in-memory fakes."""

from uuid import uuid4

from ticket_router.store.fake import FakeOrderRepo, FakeTicketRepo
from ticket_router.store.models import Order, TicketCreate


# REQ-STORE-006
def test_ticket_repo_crud_and_idempotency():
    """Verify ticket creation, retrieval, and idempotency on duplicate ticket_id."""
    repo = FakeTicketRepo()

    custom_id = uuid4()
    req = TicketCreate(
        ticket_id=custom_id,
        customer_id="cust_1001",
        body_redacted="My package did not arrive on time.",
        channel="web",
        decision={"intent": "order_inquiry", "confidence": 0.94},
        assigned_queue="tier-1",
    )

    created = repo.create_ticket(req)
    assert created.ticket_id == custom_id
    assert created.customer_id == "cust_1001"
    assert created.status == "open"
    assert created.assigned_queue == "tier-1"
    assert created.decision["intent"] == "order_inquiry"

    # Idempotent re-creation: must return identical existing record without overwriting
    re_created = repo.create_ticket(req)
    assert re_created.ticket_id == created.ticket_id
    assert re_created.created_at == created.created_at

    # Retrieval
    fetched = repo.get_ticket(custom_id)
    assert fetched is not None
    assert fetched.ticket_id == custom_id
    assert fetched.body_redacted == "My package did not arrive on time."

    # String ticket_id lookup
    fetched_by_str = repo.get_ticket(str(custom_id))
    assert fetched_by_str is not None
    assert fetched_by_str.ticket_id == custom_id


# REQ-STORE-006
def test_ticket_repo_claim_and_optimistic_concurrency():
    """Verify atomic ticket claiming prevents dual-agent pickup."""
    repo = FakeTicketRepo()

    req = TicketCreate(
        customer_id="cust_1002",
        body_redacted="Need assistance with return label.",
        assigned_queue="returns",
    )
    ticket = repo.create_ticket(req)

    # First agent claims: succeeds
    claim_1 = repo.claim_ticket(ticket.ticket_id, agent_id="agent_alpha")
    assert claim_1 is True

    updated_ticket = repo.get_ticket(ticket.ticket_id)
    assert updated_ticket.status == "assigned"
    assert updated_ticket.assigned_to == "agent_alpha"

    # Second agent tries to claim already assigned ticket: fails
    claim_2 = repo.claim_ticket(ticket.ticket_id, agent_id="agent_beta")
    assert claim_2 is False

    # Status unchanged
    still_assigned = repo.get_ticket(ticket.ticket_id)
    assert still_assigned.assigned_to == "agent_alpha"


# REQ-STORE-006
def test_ticket_repo_list_open_tickets_filtering():
    """Verify filtering and pagination of open tickets."""
    repo = FakeTicketRepo()

    t1 = repo.create_ticket(
        TicketCreate(
            customer_id="cust_1", body_redacted="Query 1", assigned_queue="queue-a"
        )
    )
    t2 = repo.create_ticket(
        TicketCreate(
            customer_id="cust_2", body_redacted="Query 2", assigned_queue="queue-b"
        )
    )
    t3 = repo.create_ticket(
        TicketCreate(
            customer_id="cust_3", body_redacted="Query 3", assigned_queue="queue-a"
        )
    )

    # Claim t1 so only t2 and t3 remain open
    repo.claim_ticket(t1.ticket_id, "agent_1")

    all_open = repo.list_open_tickets()
    assert len(all_open) == 2
    open_ids = [t.ticket_id for t in all_open]
    assert t2.ticket_id in open_ids
    assert t3.ticket_id in open_ids
    assert t1.ticket_id not in open_ids

    # Filter by queue
    queue_a_open = repo.list_open_tickets(queue="queue-a")
    assert len(queue_a_open) == 1
    assert queue_a_open[0].ticket_id == t3.ticket_id


# REQ-STORE-006
def test_ticket_repo_audit_event_trail():
    """Verify lifecycle events are recorded in chronological order."""
    repo = FakeTicketRepo()

    t = repo.create_ticket(
        TicketCreate(customer_id="cust_1003", body_redacted="Account inquiry")
    )
    # Creation event automatically generated
    events = repo.list_events(t.ticket_id)
    assert len(events) == 1
    assert events[0].event_type == "created"

    # Claim event
    repo.claim_ticket(t.ticket_id, agent_id="human_agent_42")
    events = repo.list_events(t.ticket_id)
    assert len(events) == 2
    assert events[1].event_type == "claimed"
    assert events[1].actor == "human_agent_42"

    # Status update event
    repo.update_ticket_status(
        t.ticket_id, status="resolved", resolution_notes="Customer helped via chat"
    )
    events = repo.list_events(t.ticket_id)
    assert len(events) == 3
    assert events[2].event_type == "status_changed"

    updated = repo.get_ticket(t.ticket_id)
    assert updated.status == "resolved"
    assert updated.resolution_notes == "Customer helped via chat"


# REQ-STORE-006
def test_order_repo_rls_customer_isolation():
    """Verify OrderRepository enforces tenant/customer isolation (simulating Postgres RLS)."""
    orders = [
        Order(
            order_id="ORD-20001",
            customer_id="cust_1001",
            order_status="SHIPPED",
            items=[{"sku": "SKU-HDST-001", "name": "Headphones", "qty": 1, "price_cents": 19999}],
            total_cents=19999,
            tracking_number="TRK-12345",
        ),
        Order(
            order_id="ORD-20002",
            customer_id="cust_1002",
            order_status="DELIVERED",
            items=[{"sku": "SKU-KB-004", "name": "Keyboard", "qty": 1, "price_cents": 14999}],
            total_cents=14999,
            tracking_number="TRK-67890",
        ),
    ]

    order_repo = FakeOrderRepo(orders)

    # 1. Unscoped lookup (admin / internal service)
    order = order_repo.get_order("ORD-20001")
    assert order is not None
    assert order.order_id == "ORD-20001"

    # 2. Scoped lookup with matching customer_id succeeds
    order_matched = order_repo.get_order("ORD-20001", customer_id="cust_1001")
    assert order_matched is not None
    assert order_matched.order_id == "ORD-20001"

    # 3. Scoped lookup with DIFFERENT customer_id is blocked (returns None per RLS)
    order_blocked = order_repo.get_order("ORD-20001", customer_id="cust_1002")
    assert order_blocked is None

    # 4. Non-existent order
    assert order_repo.get_order("ORD-99999") is None

    # 5. List customer orders
    cust_orders = order_repo.list_customer_orders("cust_1001")
    assert len(cust_orders) == 1
    assert cust_orders[0].order_id == "ORD-20001"
