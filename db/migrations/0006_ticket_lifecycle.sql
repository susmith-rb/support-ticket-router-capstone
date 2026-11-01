-- db/migrations/0006_ticket_lifecycle.sql
-- Additive ticket lifecycle tracking: issue_reference, category, and current_owner.
-- Implements REQ-STORE-011 and REQ-AGENT-037.

-- ----------------------------------------------------------------------------
-- 1. Additive columns to tickets table
-- ----------------------------------------------------------------------------
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS issue_reference VARCHAR(64);
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS category VARCHAR(32) NOT NULL DEFAULT 'support';
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS current_owner VARCHAR(128) NOT NULL DEFAULT 'bot';

-- ----------------------------------------------------------------------------
-- 2. Indexes for ticket lifecycle lookups
-- ----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_tickets_issue_reference ON tickets(issue_reference);
CREATE INDEX IF NOT EXISTS idx_tickets_category ON tickets(category);
CREATE INDEX IF NOT EXISTS idx_tickets_current_owner ON tickets(current_owner);

-- ----------------------------------------------------------------------------
-- 3. Backfill existing records
-- ----------------------------------------------------------------------------
-- Backfill category="security" for existing rows whose decision tier/reason
-- is model_armor/model_armor_security_trigger, category="support" otherwise.
UPDATE tickets
SET category = 'security'
WHERE decision->>'tier' = 'model_armor'
   OR decision->>'reason' = 'model_armor_security_trigger'
   OR decision->>'reason' = 'prompt_injection_or_jailbreak'
   OR decision->'details'->>'category' = 'security';

-- Backfill owner from assigned_to/assigned_queue
UPDATE tickets
SET current_owner = CASE
    WHEN assigned_to IS NOT NULL AND assigned_to != '' THEN 'rep:' || assigned_to
    WHEN assigned_queue IS NOT NULL AND assigned_queue LIKE 'human%' THEN 'queue:' || assigned_queue
    ELSE 'bot'
END
WHERE current_owner = 'bot' OR current_owner IS NULL;

-- Backfill issue_reference from decision->>'ticket_id' or generated
UPDATE tickets
SET issue_reference = COALESCE(
    NULLIF(decision->>'ticket_id', ''),
    'TICK-' || SUBSTRING(ticket_id::text, 1, 8)
)
WHERE issue_reference IS NULL;
