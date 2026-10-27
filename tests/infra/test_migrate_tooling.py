# tests/infra/test_migrate_tooling.py
# Static and unit testing of migration runner, password resolution, and DB verification.
# Implements REQ-STORE-007: Migration runner password resolution, OAuth credentials, and DB verification.

import base64
import logging
from unittest.mock import MagicMock, patch

import pytest

from scripts.migrate import (
    get_connector,
    resolve_password,
    verify_database,
)


# REQ-STORE-007
def test_resolve_password_cli_precedence(monkeypatch):
    """Explicit CLI password takes highest precedence over env or Secret Manager."""
    monkeypatch.setenv("DB_PASSWORD", "env_secret_123")
    pwd = resolve_password(cli_password="cli_secret_override")
    assert pwd == "cli_secret_override"


# REQ-STORE-007
def test_resolve_password_env_precedence(monkeypatch):
    """DB_PASSWORD env variable takes precedence when no CLI argument is given."""
    monkeypatch.setenv("DB_PASSWORD", "env_secret_123")
    pwd = resolve_password(cli_password=None)
    assert pwd == "env_secret_123"


# REQ-STORE-007
def test_resolve_password_from_secret_manager_rest(monkeypatch, caplog):
    """When no password passed or in env, resolves via Secret Manager REST and never logs password."""
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    secret_val = "high_entropy_db_pass_98765"
    b64_val = base64.b64encode(secret_val.encode("utf-8")).decode("utf-8")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"payload": {"data": b64_val}}

    with patch("ticket_router.common.auth.get_access_token", return_value="fake_access_token"), \
         patch("requests.get", return_value=mock_resp) as mock_get, \
         caplog.at_level(logging.INFO):
        pwd = resolve_password(
            cli_password=None,
            secret_id="cloudsql-ticket-store-admin-password",
            project_id="test-project",
            version="latest",
        )
        assert pwd == secret_val
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert "Authorization" in kwargs["headers"]
        assert kwargs["headers"]["Authorization"] == "Bearer fake_access_token"

        # Invariant: Never log password
        for record in caplog.records:
            assert secret_val not in record.message


# REQ-STORE-007
def test_connector_credentials_token_path(monkeypatch):
    """When GOOGLE_OAUTH_ACCESS_TOKEN is set, passes OAuth Credentials to Connector."""
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "short_lived_oauth_token_abc")

    mock_connector_mod = MagicMock()
    mock_connector_cls = MagicMock()
    mock_connector_mod.Connector = mock_connector_cls

    mock_oauth_creds_mod = MagicMock()
    mock_oauth_creds_cls = MagicMock()
    mock_creds_instance = MagicMock()
    mock_oauth_creds_cls.return_value = mock_creds_instance
    mock_oauth_creds_mod.Credentials = mock_oauth_creds_cls

    with patch.dict(
        "sys.modules",
        {
            "google.cloud.sql.connector": mock_connector_mod,
            "google.oauth2.credentials": mock_oauth_creds_mod,
        },
    ):
        get_connector()
        mock_oauth_creds_cls.assert_called_once_with(token="short_lived_oauth_token_abc")
        mock_connector_cls.assert_called_once_with(credentials=mock_creds_instance)


# REQ-STORE-007
def test_connector_credentials_default_path(monkeypatch):
    """When GOOGLE_OAUTH_ACCESS_TOKEN is not set, uses default ADC."""
    monkeypatch.delenv("GOOGLE_OAUTH_ACCESS_TOKEN", raising=False)

    mock_connector_mod = MagicMock()
    mock_connector_cls = MagicMock()
    mock_connector_mod.Connector = mock_connector_cls

    with patch.dict("sys.modules", {"google.cloud.sql.connector": mock_connector_mod}):
        get_connector()
        mock_connector_cls.assert_called_once_with()


class FakeCursor:
    """Mock database cursor for verify_database tests."""

    def __init__(
        self,
        tables=None,
        roles=None,
        rls_enabled=True,
        orders_count=200,
        memberships=None,
    ):
        self.tables = tables or [
            ("tickets",),
            ("ticket_events",),
            ("orders_mock",),
            ("redaction_maps",),
        ]
        self.roles = roles or [
            ("orders_ro",),
            ("ticket_rw",),
            ("queue_rw",),
            ("analyst_ro",),
            ("sa-order-mcp@susmith-fde-ticket-router.iam",),
            ("sa-ticket-mcp@susmith-fde-ticket-router.iam",),
            ("sa-queue@susmith-fde-ticket-router.iam",),
        ]
        self.rls_enabled = rls_enabled
        self.orders_count = orders_count
        self.memberships = memberships or [
            ("orders_ro", "sa-order-mcp@susmith-fde-ticket-router.iam"),
            ("ticket_rw", "sa-ticket-mcp@susmith-fde-ticket-router.iam"),
            ("queue_rw", "sa-queue@susmith-fde-ticket-router.iam"),
        ]
        self._current_query = None

    def execute(self, query, params=None):
        self._current_query = query

    def fetchall(self):
        if "pg_auth_members" in self._current_query:
            return self.memberships
        if "pg_tables" in self._current_query:
            return self.tables
        if "pg_roles" in self._current_query:
            return self.roles
        return []

    def fetchone(self):
        if "pg_class" in self._current_query:
            return (self.rls_enabled,)
        if "count(*)" in self._current_query:
            return (self.orders_count,)
        return None

    def close(self):
        pass


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


# REQ-STORE-007
def test_verify_database_success():
    """Verify database check passes when all tables, roles, RLS, and IAM memberships match."""
    cursor = FakeCursor()
    conn = FakeConn(cursor)

    report = verify_database(conn)
    assert "tickets" in report["tables"]
    assert "orders_ro" in report["roles"]
    assert report["orders_mock_rls"] is True
    assert report["orders_mock_count"] == 200
    assert "sa-order-mcp@susmith-fde-ticket-router.iam" in report["memberships"]["orders_ro"]
    assert "sa-ticket-mcp@susmith-fde-ticket-router.iam" in report["memberships"]["ticket_rw"]
    assert "sa-queue@susmith-fde-ticket-router.iam" in report["memberships"]["queue_rw"]


# REQ-STORE-007
def test_verify_database_missing_table_raises():
    """Missing required table raises RuntimeError."""
    cursor = FakeCursor(tables=[("tickets",), ("orders_mock",)])
    conn = FakeConn(cursor)

    with pytest.raises(RuntimeError, match="Missing required table"):
        verify_database(conn)


# REQ-STORE-007
def test_verify_database_missing_role_raises():
    """Missing required role raises RuntimeError."""
    cursor = FakeCursor(roles=[("orders_ro",), ("ticket_rw",)])
    conn = FakeConn(cursor)

    with pytest.raises(RuntimeError, match="Missing required database role"):
        verify_database(conn)


# REQ-STORE-007
def test_verify_database_rls_disabled_raises():
    """Disabled RLS on orders_mock raises RuntimeError."""
    cursor = FakeCursor(rls_enabled=False)
    conn = FakeConn(cursor)

    with pytest.raises(RuntimeError, match="Row-Level Security is NOT enabled"):
        verify_database(conn)


# REQ-STORE-007
def test_verify_database_missing_iam_membership_raises():
    """Unbound IAM user without corresponding role grant raises RuntimeError."""
    cursor = FakeCursor(
        memberships=[
            ("ticket_rw", "sa-ticket-mcp@susmith-fde-ticket-router.iam"),
            ("queue_rw", "sa-queue@susmith-fde-ticket-router.iam"),
            # Missing orders_ro grant for sa-order-mcp
        ]
    )
    conn = FakeConn(cursor)

    with pytest.raises(RuntimeError, match="is not granted orders_ro"):
        verify_database(conn)


# REQ-STORE-008
def test_verify_database_human_iam_user_membership():
    """Verify database check passes when human IAM user has analyst_ro grant."""
    cursor = FakeCursor(
        roles=[
            ("orders_ro",),
            ("ticket_rw",),
            ("queue_rw",),
            ("analyst_ro",),
            ("sa-order-mcp@susmith-fde-ticket-router.iam",),
            ("sa-ticket-mcp@susmith-fde-ticket-router.iam",),
            ("sa-queue@susmith-fde-ticket-router.iam",),
            ("susmith@gcp.altostrat.com",),
        ],
        memberships=[
            ("orders_ro", "sa-order-mcp@susmith-fde-ticket-router.iam"),
            ("ticket_rw", "sa-ticket-mcp@susmith-fde-ticket-router.iam"),
            ("queue_rw", "sa-queue@susmith-fde-ticket-router.iam"),
            ("analyst_ro", "susmith@gcp.altostrat.com"),
        ],
    )
    conn = FakeConn(cursor)
    report = verify_database(conn)
    assert "susmith@gcp.altostrat.com" in report["memberships"]["analyst_ro"]


# REQ-STORE-008
def test_verify_database_human_iam_user_missing_analyst_ro_raises():
    """Human IAM user without analyst_ro raises RuntimeError."""
    cursor = FakeCursor(
        roles=[
            ("orders_ro",),
            ("ticket_rw",),
            ("queue_rw",),
            ("analyst_ro",),
            ("sa-order-mcp@susmith-fde-ticket-router.iam",),
            ("sa-ticket-mcp@susmith-fde-ticket-router.iam",),
            ("sa-queue@susmith-fde-ticket-router.iam",),
            ("susmith@gcp.altostrat.com",),
        ],
        memberships=[
            ("orders_ro", "sa-order-mcp@susmith-fde-ticket-router.iam"),
            ("ticket_rw", "sa-ticket-mcp@susmith-fde-ticket-router.iam"),
            ("queue_rw", "sa-queue@susmith-fde-ticket-router.iam"),
            # Missing analyst_ro grant for susmith@gcp.altostrat.com
        ],
    )
    conn = FakeConn(cursor)
    with pytest.raises(RuntimeError, match="is not granted analyst_ro"):
        verify_database(conn)

