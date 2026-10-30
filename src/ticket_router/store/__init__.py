"""Cloud SQL Ticket and Order Storage package.

Exposes domain models, repository interfaces, in-memory fakes, and
PostgreSQL backends.
"""

from ticket_router.store.connector import CloudSqlConnectionManager
from ticket_router.store.fake import (
    FakeCustomerRepo,
    FakeOrderRepo,
    FakeTicketRepo,
)
from ticket_router.store.ids import canonical_ticket_uuid
from ticket_router.store.interfaces import (
    CustomerRepository,
    OrderRepository,
    TicketRepository,
)
from ticket_router.store.models import (
    Customer,
    CustomerIdentity,
    CustomerResolution,
    IdentityEvent,
    Order,
    RedactionMap,
    Ticket,
    TicketCreate,
    TicketEvent,
)
from ticket_router.store.postgres import (
    PostgresCustomerRepo,
    PostgresOrderRepo,
    PostgresTicketRepo,
)

__all__ = [
    "CloudSqlConnectionManager",
    "Customer",
    "CustomerIdentity",
    "CustomerRepository",
    "CustomerResolution",
    "FakeCustomerRepo",
    "FakeOrderRepo",
    "FakeTicketRepo",
    "IdentityEvent",
    "Order",
    "OrderRepository",
    "PostgresCustomerRepo",
    "PostgresOrderRepo",
    "PostgresTicketRepo",
    "RedactionMap",
    "Ticket",
    "TicketCreate",
    "TicketEvent",
    "TicketRepository",
    "canonical_ticket_uuid",
]
