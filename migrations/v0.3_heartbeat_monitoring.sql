-- RAYTECH MONITOR v0.3 — Heartbeat Monitoring Forward Migration
-- Safe, non-destructive migration script for PostgreSQL

-- 1. Ensure type column exists on monitors
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS type VARCHAR(32) NOT NULL DEFAULT 'http';

-- 2. Allow url to be nullable for heartbeat monitors
ALTER TABLE monitors ALTER COLUMN url DROP NOT NULL;

-- 3. Add heartbeat configuration and scheduling columns to monitors
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS expected_interval_seconds INTEGER;
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS grace_period_seconds INTEGER;
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS next_expected_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS deadline_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS heartbeat_token_hash VARCHAR(64);

-- 4. Create unique index for token hash lookups
CREATE UNIQUE INDEX IF NOT EXISTS uq_monitors_heartbeat_token_hash
ON monitors (heartbeat_token_hash)
WHERE heartbeat_token_hash IS NOT NULL;

-- 5. Create index for background worker overdue heartbeat queries
CREATE INDEX IF NOT EXISTS idx_monitors_heartbeat_deadline
ON monitors (is_active, type, status, deadline_at);

-- 6. Create dedicated heartbeat_events table
CREATE TABLE IF NOT EXISTS heartbeat_events (
    id BIGSERIAL PRIMARY KEY,
    monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    received_at TIMESTAMP WITH TIME ZONE NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    ip_address VARCHAR(45)
);

-- 7. Create index on heartbeat_events for fast monitor event history retrieval
CREATE INDEX IF NOT EXISTS idx_heartbeat_events_monitor
ON heartbeat_events (monitor_id, received_at DESC);
