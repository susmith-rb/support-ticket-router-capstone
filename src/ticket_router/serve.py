"""Multi-service entrypoint for Cloud Run containers.

# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
# REQ-AGENT-009: HumanQueue Workflow and Audited PII Re-Identification
# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
# REQ-STORE-002: Cloud SQL Python Connector with IAM database authentication
# REQ-STORE-006: Concrete storage layer for tickets, orders, and redaction maps

Selects target service application via environment variable SERVICE:
- SERVICE=ticket_db       -> ticket-db-mcp server (sa-ticket-mcp, ticket_rw)
- SERVICE=order_lookup    -> order-lookup-mcp server (sa-order-mcp, orders_ro)
- SERVICE=human_queue     -> human-queue agent server (sa-queue, queue_rw)
- SERVICE=faq_resolver    -> faq-resolver agent server (GKE Autopilot / unified container)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

from fastapi import FastAPI

logger = logging.getLogger("ticket_router.serve")


def build_connection_manager(
    instance_connection_name: Optional[str] = None,
    db_name: Optional[str] = None,
    db_iam_user: Optional[str] = None,
) -> Any:
    """Build CloudSqlConnectionManager from environment or explicit arguments.

    Enforces fail-fast validation: requires INSTANCE_CONNECTION_NAME and DB_IAM_USER.
    """
    conn_name = instance_connection_name or os.getenv("INSTANCE_CONNECTION_NAME")
    if not conn_name:
        raise RuntimeError(
            "INSTANCE_CONNECTION_NAME environment variable is required to connect to Cloud SQL"
        )

    parts = conn_name.split(":")
    if len(parts) != 3:
        raise ValueError(
            f"Invalid INSTANCE_CONNECTION_NAME '{conn_name}', expected 'project:region:instance'"
        )
    project_id, region, instance_name = parts

    database = db_name or os.getenv("DB_NAME", "support")
    user = db_iam_user or os.getenv("DB_IAM_USER") or os.getenv("DB_USER")
    if not user:
        raise RuntimeError(
            "DB_IAM_USER environment variable is required for IAM database authentication"
        )

    from ticket_router.store.connector import CloudSqlConnectionManager

    return CloudSqlConnectionManager(
        project_id=project_id,
        region=region,
        instance_name=instance_name,
        database=database,
        user=user,
        enable_iam_auth=True,
    )


def get_app(service_name: Optional[str] = None) -> FastAPI:
    """Resolve and instantiate FastAPI application based on service name and database configuration.

    Fails fast at startup if real database credentials are not provided and ALLOW_FAKE_REPOS!=1.
    """
    svc = (service_name or os.getenv("SERVICE", "ticket_db")).lower().replace("-", "_")
    allow_fake = os.getenv("ALLOW_FAKE_REPOS", "").lower() in ("1", "true")

    if svc == "ticket_db":
        from ticket_router.mcp.ticket_db.server import create_app as create_ticket_db_app

        if allow_fake:
            from ticket_router.store.fake import FakeTicketRepo

            return create_ticket_db_app(repo=FakeTicketRepo())

        conn_mgr = build_connection_manager()
        from ticket_router.store.postgres import PostgresTicketRepo

        ticket_repo = PostgresTicketRepo(connection_factory=conn_mgr.get_connection)
        return create_ticket_db_app(repo=ticket_repo)

    elif svc == "order_lookup":
        from ticket_router.mcp.order_lookup.server import create_app as create_order_lookup_app

        if allow_fake:
            from ticket_router.store.fake import FakeOrderRepo

            return create_order_lookup_app(order_repo=FakeOrderRepo())

        conn_mgr = build_connection_manager()
        from ticket_router.store.postgres import PostgresOrderRepo

        order_repo = PostgresOrderRepo(connection_factory=conn_mgr.get_connection)
        return create_order_lookup_app(order_repo=order_repo)

    elif svc == "human_queue":
        from ticket_router.agents.human_queue.server import create_app as create_human_queue_app

        if allow_fake:
            from ticket_router.store.fake import FakeTicketRepo

            return create_human_queue_app(ticket_repo=FakeTicketRepo())

        conn_mgr = build_connection_manager()
        from ticket_router.store.postgres import PostgresTicketRepo

        ticket_repo = PostgresTicketRepo(connection_factory=conn_mgr.get_connection)
        return create_human_queue_app(ticket_repo=ticket_repo)

    elif svc in ("faq_resolver", "faq"):
        from ticket_router.agents.faq_resolver.app import create_fastapi_app

        return create_fastapi_app()

    else:
        raise ValueError(
            f"Unknown SERVICE '{svc}'. Must be one of: ticket_db, order_lookup, human_queue, faq_resolver"
        )


def main() -> None:
    """Run uvicorn server conforming to Cloud Run container contract."""
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")
    svc_name = os.getenv("SERVICE", "ticket_db")

    logger.info("Starting service '%s' on %s:%s...", svc_name, host, port)
    try:
        app_instance = get_app(svc_name)
    except Exception as exc:
        logger.error("Failed to start service '%s': %s", svc_name, exc)
        sys.exit(1)

    uvicorn.run(app_instance, host=host, port=port)


if __name__ == "__main__":
    main()
