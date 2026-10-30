-- db/migrations/0004_redaction_envelope.sql
-- KMS envelope-encrypted redaction maps and 30-day TTL purge permissions.
-- Implements REQ-STORE-009: Reversible PII Tokens and KMS Envelope-Encrypted Redaction Maps.
-- Implements REQ-STORE-010: Redaction Map 30-Day TTL Purge.

-- ----------------------------------------------------------------------------
-- 1. Alter default key_id to 'kms-envelope-v1'
-- ----------------------------------------------------------------------------
ALTER TABLE redaction_maps ALTER COLUMN key_id SET DEFAULT 'kms-envelope-v1';

-- ----------------------------------------------------------------------------
-- 2. Permissions: ticket_rw gets INSERT only on redaction_maps
-- Invariant: No SELECT, UPDATE, or DELETE granted to ticket_rw (C3 PII isolation).
-- ----------------------------------------------------------------------------
GRANT INSERT ON redaction_maps TO ticket_rw;

-- ----------------------------------------------------------------------------
-- 3. Permissions: queue_rw gets DELETE on redaction_maps for 30-day TTL purge
-- (queue_rw already has SELECT from 0001_init.sql).
-- ----------------------------------------------------------------------------
GRANT DELETE ON redaction_maps TO queue_rw;
