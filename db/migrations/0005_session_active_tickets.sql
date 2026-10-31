-- db/migrations/0005_session_active_tickets.sql
-- Session tracking and multi-turn ticket holding support.
-- Implements REQ-AGENT-033: Multi-Turn Conversation Holding and Customer Follow-up Attachment.

-- ----------------------------------------------------------------------------
-- 1. Add session_id column to tickets table if not exists
-- ----------------------------------------------------------------------------
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS session_id VARCHAR(128);

-- ----------------------------------------------------------------------------
-- 2. Indexes for session lookup and holding status queries
-- ----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_tickets_session_id ON tickets(session_id);
CREATE INDEX IF NOT EXISTS idx_tickets_session_customer ON tickets(session_id, customer_id);
