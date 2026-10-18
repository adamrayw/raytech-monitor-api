-- RAYTECH MONITOR v0.8 — Payment Confirmation Email Outbox Forward Migration
-- Safe, non-destructive migration script for PostgreSQL

-- 1. Add idempotency_key to email_deliveries table
ALTER TABLE email_deliveries
ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128) UNIQUE;

-- 2. Add index for fast deduplication lookup
CREATE INDEX IF NOT EXISTS idx_email_deliveries_idempotency_key
ON email_deliveries (idempotency_key);
