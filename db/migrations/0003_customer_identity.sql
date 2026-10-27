-- db/migrations/0003_customer_identity.sql
-- Customer identity and relational mapping for Cloud SQL PostgreSQL.
-- Implements REQ-IDENT-002: Relational Customer Identity Schema & Migration 0003.

-- ----------------------------------------------------------------------------
-- 1. Table: customers (Master customer entity)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customers (
    customer_id VARCHAR(64) PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Backfill customers from existing orders_mock table if present
INSERT INTO customers (customer_id)
SELECT DISTINCT customer_id FROM orders_mock
ON CONFLICT (customer_id) DO NOTHING;

-- ----------------------------------------------------------------------------
-- 2. Table: customer_identities (Maps IdP issuer+subject to customer)
-- Zero email storage invariant enforced (subject UID only).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customer_identities (
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    customer_id VARCHAR(64) NOT NULL REFERENCES customers(customer_id) ON DELETE CASCADE,
    email_verified BOOLEAN NOT NULL DEFAULT false,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (issuer, subject)
);

CREATE INDEX IF NOT EXISTS idx_customer_identities_customer_id ON customer_identities(customer_id);

-- ----------------------------------------------------------------------------
-- 3. Table: identity_events (Append-only identity audit trail)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS identity_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id VARCHAR(64) NULL,
    issuer TEXT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_identity_events_customer_id ON identity_events(customer_id);
CREATE INDEX IF NOT EXISTS idx_identity_events_created_at ON identity_events(created_at);

-- ----------------------------------------------------------------------------
-- 4. Permissions
-- ----------------------------------------------------------------------------
GRANT SELECT ON customers, customer_identities TO ticket_rw;
-- SELECT is required because append_identity_event uses INSERT ... RETURNING (reads generated columns).
GRANT SELECT, INSERT ON identity_events TO ticket_rw;
