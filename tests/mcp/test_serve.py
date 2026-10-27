"""Tests for multi-service entrypoint and database wiring in serve.py.

# REQ-AGENT-001: Multi-Agent Package Structure and Deployment Topology
# REQ-AGENT-009: HumanQueue Workflow and Audited PII Re-Identification
# REQ-AGENT-010: MCP Tool Contracts, JSON Schemas, and Role-Based DB Access
# REQ-STORE-002: Cloud SQL Python Connector with IAM database authentication
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ticket_router.serve import build_connection_manager, get_app
from ticket_router.store.postgres import PostgresOrderRepo, PostgresTicketRepo


# REQ-AGENT-001
def test_serve_startup_fails_without_env_and_without_fake(monkeypatch):
    """Verify fail-fast startup behavior when required database credentials are missing."""
    monkeypatch.delenv("ALLOW_FAKE_REPOS", raising=False)
    monkeypatch.delenv("INSTANCE_CONNECTION_NAME", raising=False)
    monkeypatch.delenv("DB_IAM_USER", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)

    with pytest.raises(RuntimeError, match="INSTANCE_CONNECTION_NAME"):
        get_app("ticket_db")

    with pytest.raises(RuntimeError, match="INSTANCE_CONNECTION_NAME"):
        get_app("order_lookup")

    with pytest.raises(RuntimeError, match="INSTANCE_CONNECTION_NAME"):
        get_app("human_queue")


# REQ-AGENT-001
def test_serve_startup_succeeds_with_allow_fake_repos(monkeypatch):
    """Verify that setting ALLOW_FAKE_REPOS=1 boots lightweight fakes for all services."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ALLOW_FAKE_REPOS", "1")

    # 1. ticket-db-mcp
    app_ticket = get_app("ticket_db")
    assert app_ticket.title == "ticket-db-mcp"
    assert app_ticket.state.ticket_repo is not None
    client_ticket = TestClient(app_ticket)
    assert client_ticket.get("/health").status_code == 200
    assert client_ticket.get("/healthz").status_code == 200

    # 2. order-lookup-mcp
    app_order = get_app("order_lookup")
    assert app_order.title == "order-lookup-mcp"
    assert app_order.state.order_repo is not None
    client_order = TestClient(app_order)
    assert client_order.get("/health").status_code == 200
    assert client_order.get("/healthz").status_code == 200

    # 3. human-queue
    app_queue = get_app("human_queue")
    assert app_queue.title == "human-queue"
    assert app_queue.state.agent is not None
    client_queue = TestClient(app_queue)
    assert client_queue.get("/health").status_code == 200
    assert client_queue.get("/healthz").status_code == 200

    # 4. faq_resolver
    app_faq = get_app("faq_resolver")
    assert app_faq.title == "FaqResolverAgent Service"
    client_faq = TestClient(app_faq)
    assert client_faq.get("/health").status_code == 200
    assert client_faq.get("/healthz").status_code == 200


# REQ-AGENT-001, REQ-STORE-002
def test_serve_builds_postgres_repos_when_env_present(monkeypatch):
    """Verify that real Cloud SQL Postgres repos are wired when connection parameters exist."""
    monkeypatch.delenv("ALLOW_FAKE_REPOS", raising=False)
    monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "susmith-fde-ticket-router:us-central1:ticket-store")
    monkeypatch.setenv("DB_NAME", "support")
    monkeypatch.setenv("DB_IAM_USER", "sa-ticket-mcp")

    mock_conn = MagicMock()
    mock_mgr = MagicMock()
    mock_mgr.get_connection.return_value = mock_conn

    with patch("ticket_router.store.connector.CloudSqlConnectionManager", return_value=mock_mgr):
        # 1. ticket_db wires PostgresTicketRepo
        app_ticket = get_app("ticket_db")
        assert isinstance(app_ticket.state.ticket_repo, PostgresTicketRepo)

        # 2. order_lookup wires PostgresOrderRepo
        app_order = get_app("order_lookup")
        assert isinstance(app_order.state, object)
        assert isinstance(app_order.state.order_repo, PostgresOrderRepo)

        # 3. human_queue wires PostgresTicketRepo into HumanQueueAgent
        app_queue = get_app("human_queue")
        assert isinstance(app_queue.state.agent.repo, PostgresTicketRepo)


# REQ-AGENT-001
def test_build_connection_manager_validations(monkeypatch):
    """Verify validation of connection string parts and IAM user requirement."""
    monkeypatch.delenv("ALLOW_FAKE_REPOS", raising=False)

    # Missing connection name
    monkeypatch.delenv("INSTANCE_CONNECTION_NAME", raising=False)
    with pytest.raises(RuntimeError, match="INSTANCE_CONNECTION_NAME"):
        build_connection_manager()

    # Malformed connection name
    with pytest.raises(ValueError, match="Invalid INSTANCE_CONNECTION_NAME"):
        build_connection_manager(instance_connection_name="bad-string")

    # Missing user
    monkeypatch.delenv("DB_IAM_USER", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    with pytest.raises(RuntimeError, match="DB_IAM_USER"):
        build_connection_manager(instance_connection_name="proj:reg:inst")


# REQ-AGENT-001
def test_serve_unknown_service_raises_value_error():
    """Verify unknown SERVICE name raises ValueError."""
    with pytest.raises(ValueError, match="Unknown SERVICE 'invalid_service'"):
        get_app("invalid_service")
