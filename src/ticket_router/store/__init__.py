"""Cloud SQL Ticket and Order Storage package.

Exposes domain models, repository interfaces, in-memory fakes, and
PostgreSQL backends.
"""

from ticket_router.store.connector import CloudSqlConnectionManager
from ticket_router.store.fake import FakeOrderRepo, FakeTicketRepo
from ticket_router.store.interfaces import OrderRepository, TicketRepository
from ticket_router.store.models import (
    Order,
    RedactionMap,
    Ticket,
    TicketCreate,
    TicketEvent,
)
from ticket_router.store.postgres import PostgresOrderRepo, PostgresTicketRepo

__all__ = [
    "TicketCreate",
    "Ticket",
    "TicketEvent",
    "Order",
    "RedactionMap",
    "TicketRepository",
    "OrderRepository",
    "FakeTicketRepo",
    "FakeOrderRepo",
    "PostgresTicketRepo",
    "PostgresOrderRepo",
    "CloudSqlConnectionManager",
]
