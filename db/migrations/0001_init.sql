-- db/migrations/0001_init.sql
-- PostgreSQL 16 schema initialization for Support Ticket Router.
-- Implements REQ-STORE-004: Idempotent DDL, audit events, orders mock, redaction maps (30 d TTL), roles, and RLS.

-- Required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ----------------------------------------------------------------------------
-- Database Roles
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'orders_ro') THEN
        CREATE ROLE orders_ro NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_rw') THEN
        CREATE ROLE ticket_rw NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'queue_rw') THEN
        CREATE ROLE queue_rw NOLOGIN;
    END IF;
END
$$;

-- ----------------------------------------------------------------------------
-- Table: tickets (System of Record for Support Tickets)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id VARCHAR(64) NOT NULL,
    channel VARCHAR(32) NOT NULL DEFAULT 'web',
    body_redacted TEXT NOT NULL,
    decision JSONB,
    status VARCHAR(32) NOT NULL DEFAULT 'open',
    assigned_to VARCHAR(64),
    assigned_queue VARCHAR(64),
    answer_text TEXT,
    citations JSONB,
    resolution_notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tickets_customer_id ON tickets(customer_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_created_at ON tickets(created_at);
CREATE INDEX IF NOT EXISTS idx_tickets_assigned_to ON tickets(assigned_to);

-- ----------------------------------------------------------------------------
-- Table: ticket_events (Append-only Audit Trail)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ticket_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id UUID NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    event_type VARCHAR(64) NOT NULL,
    actor VARCHAR(64) NOT NULL,
    payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket_id ON ticket_events(ticket_id);
CREATE INDEX IF NOT EXISTS idx_ticket_events_created_at ON ticket_events(created_at);

-- ----------------------------------------------------------------------------
-- Table: orders_mock (Order Lookup System for MCP Server)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders_mock (
    order_id VARCHAR(64) PRIMARY KEY,
    customer_id VARCHAR(64) NOT NULL,
    order_status VARCHAR(32) NOT NULL,
    items JSONB NOT NULL,
    tracking_number VARCHAR(64),
    shipping_carrier VARCHAR(32),
    total_cents INTEGER NOT NULL,
    currency VARCHAR(3) NOT NULL DEFAULT 'USD',
    shipping_address_city VARCHAR(64),
    shipping_address_state VARCHAR(32),
    estimated_delivery TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders_mock(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_order_status ON orders_mock(order_status);

-- ----------------------------------------------------------------------------
-- Table: redaction_maps (C3 Confidential PII Token Mappings with 30-Day TTL)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS redaction_maps (
    map_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id UUID NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    customer_id VARCHAR(64) NOT NULL,
    encrypted_mappings BYTEA NOT NULL,
    key_id VARCHAR(64) NOT NULL DEFAULT 'secret-manager-v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (CURRENT_TIMESTAMP + INTERVAL '30 days')
);

CREATE INDEX IF NOT EXISTS idx_redaction_maps_ticket_id ON redaction_maps(ticket_id);
CREATE INDEX IF NOT EXISTS idx_redaction_maps_expires_at ON redaction_maps(expires_at);

-- ----------------------------------------------------------------------------
-- Row-Level Security (RLS) on orders_mock
-- ----------------------------------------------------------------------------
ALTER TABLE orders_mock ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS order_customer_isolation ON orders_mock;
CREATE POLICY order_customer_isolation ON orders_mock
    FOR SELECT TO orders_ro
    USING (customer_id = nullif(current_setting('app.customer_id', true), ''));

-- ----------------------------------------------------------------------------
-- Grant Permissions to Database Roles
-- ----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO orders_ro, ticket_rw, queue_rw;

-- orders_ro: Read-only access to orders_mock under RLS
GRANT SELECT ON orders_mock TO orders_ro;

-- ticket_rw: Full CRUD on tickets and ticket_events; NO access to redaction_maps
GRANT SELECT, INSERT, UPDATE, DELETE ON tickets TO ticket_rw;
GRANT SELECT, INSERT ON ticket_events TO ticket_rw;

-- queue_rw: Read tickets & redaction_maps; column-level UPDATE on ticket status and notes
GRANT SELECT ON tickets TO queue_rw;
GRANT UPDATE (status, assigned_to, updated_at, resolution_notes) ON tickets TO queue_rw;
GRANT SELECT, INSERT ON ticket_events TO queue_rw;
GRANT SELECT ON redaction_maps TO queue_rw;

-- ----------------------------------------------------------------------------
-- Bind DB Roles to Cloud SQL IAM Service Accounts (when principals exist in Postgres)
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    r record;
BEGIN
    -- Grant orders_ro to sa-order-mcp
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname LIKE '%sa-order-mcp%' OR rolname = 'sa-order-mcp' LOOP
        EXECUTE 'GRANT orders_ro TO ' || quote_ident(r.rolname);
    END LOOP;

    -- Grant ticket_rw to sa-ticket-mcp
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname LIKE '%sa-ticket-mcp%' OR rolname = 'sa-ticket-mcp' LOOP
        EXECUTE 'GRANT ticket_rw TO ' || quote_ident(r.rolname);
    END LOOP;

    -- Grant queue_rw to sa-queue
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname LIKE '%sa-queue%' OR rolname = 'sa-queue' LOOP
        EXECUTE 'GRANT queue_rw TO ' || quote_ident(r.rolname);
    END LOOP;
END
$$;
