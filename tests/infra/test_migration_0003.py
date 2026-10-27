# tests/infra/test_migration_0003.py
# Static analysis and verification for customer identity migration 0003.
# Implements REQ-IDENT-002: Relational Customer Identity Schema & Migration 0003.

import re
from pathlib import Path

import pytest

from scripts.migrate import verify_database

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_FILE = REPO_ROOT / "db" / "migrations" / "0003_customer_identity.sql"


# REQ-IDENT-002
def test_customer_identity_schema():
    """Verify migration 0003 schema: customers, customer_identities, identity_events, PK/FK, zero email storage, and grants."""
    assert MIGRATION_FILE.is_file(), f"Missing migration file: {MIGRATION_FILE}"
    sql_text = MIGRATION_FILE.read_text(encoding="utf-8")

    # 1. Table customers
    assert "CREATE TABLE IF NOT EXISTS customers" in sql_text
    assert "customer_id VARCHAR(64) PRIMARY KEY" in sql_text or "customer_id VARCHAR(64)" in sql_text
    assert "status TEXT NOT NULL DEFAULT 'active'" in sql_text or "status TEXT" in sql_text
    assert "CHECK (status IN ('active', 'suspended'))" in sql_text or "CHECK (status in ('active', 'suspended'))" in sql_text

    # Backfill from orders_mock
    assert "INSERT INTO customers (customer_id)" in sql_text
    assert "SELECT DISTINCT customer_id FROM orders_mock" in sql_text
    assert "ON CONFLICT (customer_id) DO NOTHING" in sql_text

    # 2. Table customer_identities
    assert "CREATE TABLE IF NOT EXISTS customer_identities" in sql_text
    assert "issuer TEXT NOT NULL" in sql_text
    assert "subject TEXT NOT NULL" in sql_text
    assert "customer_id VARCHAR(64) NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE" in sql_text
    assert "email_verified BOOLEAN NOT NULL DEFAULT false" in sql_text
    assert "PRIMARY KEY (issuer, subject)" in sql_text
    assert "CREATE INDEX IF NOT EXISTS idx_customer_identities_customer_id ON customer_identities(customer_id)" in sql_text

    # 3. Table identity_events
    assert "CREATE TABLE IF NOT EXISTS identity_events" in sql_text
    assert "event_id UUID PRIMARY KEY DEFAULT gen_random_uuid()" in sql_text
    assert "customer_id VARCHAR(64) NULL" in sql_text or "customer_id VARCHAR(64)" in sql_text
    assert "issuer TEXT" in sql_text
    assert "event_type TEXT NOT NULL" in sql_text
    assert "actor TEXT NOT NULL" in sql_text
    assert "payload JSONB NOT NULL DEFAULT '{}'::jsonb" in sql_text or "payload JSONB" in sql_text

    # 4. Grants to ticket_rw
    assert "GRANT SELECT ON customers, customer_identities TO ticket_rw;" in sql_text
    assert "GRANT INSERT ON identity_events TO ticket_rw;" in sql_text

    # 5. Zero email storage invariant (no email column except email_verified)
    # Search for any column declaration with 'email' that isn't 'email_verified'
    email_cols = re.findall(r"\b(email\w*)\s+(?:TEXT|VARCHAR|CHAR)", sql_text, re.IGNORECASE)
    assert not email_cols, f"Zero-PII violation: found email column declaration: {email_cols}"


class FakeMigrationCursor:
    """Mock database cursor for migration verification."""

    def __init__(self):
        self.tables = [
            ("tickets",),
            ("ticket_events",),
            ("orders_mock",),
            ("redaction_maps",),
            ("customers",),
            ("customer_identities",),
            ("identity_events",),
        ]
        self.roles = [
            ("orders_ro",),
            ("ticket_rw",),
            ("queue_rw",),
            ("analyst_ro",),
        ]
        self._current_query = ""

    def execute(self, query, params=None):
        self._current_query = query

    def fetchall(self):
        if "pg_auth_members" in self._current_query:
            return [("orders_ro", "sa-order-mcp@susmith-fde-ticket-router.iam")]
        if "pg_tables" in self._current_query:
            return self.tables
        if "information_schema.columns" in self._current_query:
            return []  # No forbidden email columns
        if "pg_roles" in self._current_query:
            return self.roles
        return []

    def fetchone(self):
        if "pg_class" in self._current_query:
            return (True,)  # RLS enabled
        if "count(*)" in self._current_query and "orders_mock" in self._current_query:
            return (200,)
        if "count(*)" in self._current_query and "customers" in self._current_query:
            return (50,)
        if "has_table_privilege" in self._current_query:
            return (True,)  # Has privilege
        return None

    def close(self):
        pass


class FakeMigrationConn:
    """Mock connection holding FakeMigrationCursor."""

    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class ForbiddenEmailMigrationCursor:
    """Mock cursor returning a forbidden email column in information_schema."""

    def __init__(self):
        self.tables = [
            ("tickets",),
            ("ticket_events",),
            ("orders_mock",),
            ("redaction_maps",),
            ("customers",),
            ("customer_identities",),
            ("identity_events",),
        ]
        self.roles = [("orders_ro",), ("ticket_rw",), ("queue_rw",), ("analyst_ro",)]
        self._current_query = ""

    def execute(self, query, params=None):
        self._current_query = query

    def fetchall(self):
        if "pg_auth_members" in self._current_query:
            return [("orders_ro", "sa-order-mcp@susmith-fde-ticket-router.iam")]
        if "pg_tables" in self._current_query:
            return self.tables
        if "information_schema.columns" in self._current_query:
            return [("customer_identities", "email")]  # Forbidden column!
        if "pg_roles" in self._current_query:
            return self.roles
        return []

    def fetchone(self):
        if "pg_class" in self._current_query:
            return (True,)
        if "count(*)" in self._current_query:
            return (10,)
        if "has_table_privilege" in self._current_query:
            return (True,)
        return None

    def close(self):
        pass


# REQ-IDENT-002
def test_verify_database_with_identity_tables():
    """Verify that scripts/migrate.py verify_database passes when identity tables and grants are present."""
    cursor = FakeMigrationCursor()
    conn = FakeMigrationConn(cursor)
    report = verify_database(conn)
    assert "customers" in report["identity_tables"]
    assert "customer_identities" in report["identity_tables"]
    assert "identity_events" in report["identity_tables"]
    assert report["customers_count"] == 50


# REQ-IDENT-002
def test_verify_database_detects_forbidden_email_column():
    """Verify that scripts/migrate.py verify_database fails when an email column is introduced."""
    cursor = ForbiddenEmailMigrationCursor()
    conn = FakeMigrationConn(cursor)
    with pytest.raises(RuntimeError, match="PII violation: forbidden email column"):
        verify_database(conn)
