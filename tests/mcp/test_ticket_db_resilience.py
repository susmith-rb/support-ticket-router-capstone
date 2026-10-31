"""Tests for Ticket DB MCP & Cloud SQL Connector resilience and timeouts.

# REQ-STORE-002: Cloud SQL connectivity resilience and fast-fail timeouts.
# REQ-AGENT-033: Hold lookup short bounded timeout and fail-open resilience.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import requests
from fastapi.testclient import TestClient

from ticket_router.agents.triage_router.mcp_client import (
    HttpTicketDbMcpClient,
)
from ticket_router.common.resilience import (
    TICKET_DB_HOLD_LOOKUP_TIMEOUT_S,
    TICKET_DB_MCP_TIMEOUT_S,
)
from ticket_router.mcp.ticket_db.server import create_app as create_ticket_app
from ticket_router.store.connector import CloudSqlConnectionManager
from ticket_router.store.fake import FakeTicketRepo


# REQ-STORE-002
def test_cloud_sql_connection_manager_passes_timeout():
    """Verify CloudSqlConnectionManager passes explicit connect timeout (10s) to Connector."""
    with patch("ticket_router.store.connector._HAS_CONNECTOR", True), \
         patch("ticket_router.store.connector.Connector") as mock_connector_cls:
        mock_connector_inst = MagicMock()
        mock_connector_cls.return_value = mock_connector_inst

        mgr = CloudSqlConnectionManager(
            project_id="test-proj",
            region="us-central1",
            instance_name="test-inst",
            database="test-db",
            ip_type="PUBLIC",
            connect_timeout_s=10.0,
        )

        conn = mgr.get_connection()
        assert conn is not None

        # Connector initialization receives timeout=10
        mock_connector_cls.assert_called_once_with(timeout=10)

        # connector.connect receives timeout=10
        mock_connector_inst.connect.assert_called_once()
        _, kwargs = mock_connector_inst.connect.call_args
        assert kwargs.get("timeout") == 10
        assert kwargs.get("instance_connection_string") == "test-proj:us-central1:test-inst"
        assert kwargs.get("db") == "test-db"


# REQ-STORE-002
def test_ticket_db_fastapi_timeout_error_returns_503():
    """Verify Ticket DB MCP returns HTTP 503 database_timeout when backend raises TimeoutError."""
    class TimingOutTicketRepo(FakeTicketRepo):
        def get_ticket(self, *args, **kwargs):
            raise TimeoutError("Cloud SQL connect timed out after 10s")

        def upsert_ticket(self, *args, **kwargs):
            raise TimeoutError("Cloud SQL connect timed out after 10s")

    app = create_ticket_app(repo=TimingOutTicketRepo())
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/tools/upsert_ticket",
        json={
            "idempotency_key": "idem-1",
            "customer_id": "cust-1",
            "channel": "chat",
            "body_redacted": "hello",
            "decision": {"decision": "escalate"},
        },
    )

    assert resp.status_code == 503
    data = resp.json()
    assert data["detail"] == "database_timeout"
    assert "Cloud SQL connect timed out" in data["error"]


# REQ-AGENT-033
def test_get_active_ticket_for_session_fail_open_on_timeout():
    """Verify get_active_ticket_for_session uses 1.5s timeout and fails open (returns None) on timeout/error."""
    mock_session = MagicMock()
    client = HttpTicketDbMcpClient(
        base_url="http://testserver",
        session=mock_session,
        hold_lookup_timeout_s=TICKET_DB_HOLD_LOOKUP_TIMEOUT_S,
    )
    assert client.hold_lookup_timeout_s == 1.5

    # 1. Timeout returns None (fail-open)
    mock_session.post.side_effect = requests.exceptions.Timeout("request timed out")
    res = client.get_active_ticket_for_session("sess-timeout-1")
    assert res is None

    # 2. ConnectionError returns None (fail-open)
    mock_session.post.side_effect = requests.exceptions.ConnectionError("connection refused")
    res = client.get_active_ticket_for_session("sess-connerror-1")
    assert res is None

    # 3. HTTPError 503 returns None (fail-open)
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("503 Service Unavailable")
    mock_session.post.side_effect = None
    mock_session.post.return_value = mock_resp
    res = client.get_active_ticket_for_session("sess-503-1")
    assert res is None


# REQ-AGENT-033
def test_http_ticket_db_mcp_client_timeout_bounds():
    """Verify HttpTicketDbMcpClient enforces 1.5s for hold lookup and 3.0s default for other tools."""
    mock_session = MagicMock()
    client = HttpTicketDbMcpClient(
        base_url="http://testserver",
        session=mock_session,
    )

    assert client.timeout_s == TICKET_DB_MCP_TIMEOUT_S
    assert client.timeout_s == 3.0
    assert client.hold_lookup_timeout_s == TICKET_DB_HOLD_LOOKUP_TIMEOUT_S
    assert client.hold_lookup_timeout_s == 1.5

    recorded_timeouts = []

    def mock_post(url, *args, **kwargs):
        recorded_timeouts.append(kwargs.get("timeout"))
        resp = MagicMock()
        resp.json.return_value = {"ticket": {"ticket_id": "TICK-1"}}
        return resp

    mock_session.post.side_effect = mock_post

    # Hold lookup uses 1.5s timeout override
    client.get_active_ticket_for_session("sess-record-1")
    assert recorded_timeouts[-1] == 1.5

    # Upsert ticket uses default 3.0s timeout
    client.upsert_ticket(
        idempotency_key="k1",
        customer_id="c1",
        channel="web",
        body_redacted="msg",
        decision={},
    )
    assert recorded_timeouts[-1] == 3.0

    # Save redaction map uses default 3.0s timeout
    client.save_redaction_map(
        ticket_id="TICK-1",
        customer_id="c1",
        envelope_b64="ZW5j",
        key_id="v1",
    )
    assert recorded_timeouts[-1] == 3.0
