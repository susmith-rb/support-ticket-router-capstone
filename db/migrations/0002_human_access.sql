-- db/migrations/0002_human_access.sql
-- Human operator read-only role and RLS configuration for Cloud SQL PostgreSQL.
-- Implements REQ-STORE-008: Read-only access for human operators via Cloud SQL Studio / Auth Proxy.

-- ----------------------------------------------------------------------------
-- 1. Create analyst_ro Database Role
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analyst_ro') THEN
        CREATE ROLE analyst_ro NOLOGIN;
    END IF;
END
$$;

-- ----------------------------------------------------------------------------
-- 2. Grant Connect, Schema Usage, and Table Permissions
-- Note: redaction_maps is strictly EXCLUDED to keep C3 customer PII restricted
-- to queue_rw under audited re-identification workflows.
-- ----------------------------------------------------------------------------
GRANT CONNECT ON DATABASE support TO analyst_ro;
GRANT USAGE ON SCHEMA public TO analyst_ro;

GRANT SELECT ON TABLE tickets, ticket_events, orders_mock TO analyst_ro;

-- ----------------------------------------------------------------------------
-- 3. Row-Level Security (RLS) Policy for analyst_ro on orders_mock
-- Default-deny requires an explicit policy for analyst_ro to view rows.
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'orders_mock' AND policyname = 'analyst_read_all'
    ) THEN
        CREATE POLICY analyst_read_all ON orders_mock
            FOR SELECT
            TO analyst_ro
            USING (true);
    END IF;
END
$$;

-- ----------------------------------------------------------------------------
-- 4. Grant analyst_ro to All Human IAM Database Users
-- Human IAM database users in Postgres contain '@' and do NOT start with 'sa-'.
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT rolname
        FROM pg_roles
        WHERE rolname LIKE '%@%'
          AND rolname NOT LIKE 'sa-%'
    LOOP
        EXECUTE format('GRANT analyst_ro TO %I', r.rolname);
    END LOOP;
END
$$;
