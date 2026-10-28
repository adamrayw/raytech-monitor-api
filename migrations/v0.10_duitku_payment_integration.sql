-- Migration: v0.10_duitku_payment_integration.sql
-- Description: Adds provider_mode and qr_string columns to payment_transactions for Duitku Sandbox integration.
-- Forward-only, non-destructive migration. Preserves all users, subscriptions, monitors, checks, and existing transactions.

ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS provider_mode VARCHAR(32) NOT NULL DEFAULT 'sandbox';
ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS qr_string TEXT;

-- Backfill any ambiguous historical provider modes to 'UNKNOWN'
UPDATE payment_transactions SET provider_mode = 'UNKNOWN' WHERE provider_mode IS NULL;

-- Index for querying transactions by provider and provider_mode
CREATE INDEX IF NOT EXISTS idx_payment_transactions_provider ON payment_transactions(provider);
CREATE INDEX IF NOT EXISTS idx_payment_transactions_provider_mode ON payment_transactions(provider_mode);
