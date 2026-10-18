-- Migration: v0.8_xendit_payment_integration.sql
-- Description: Creates payment_transactions table for Xendit Sandbox payments and subscription billing history.
-- Forward-only, non-destructive migration. Preserves all users, subscriptions, monitors, checks, and incidents.

CREATE TABLE IF NOT EXISTS payment_transactions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subscription_id INTEGER REFERENCES user_subscriptions(id) ON DELETE SET NULL,
    provider VARCHAR(32) NOT NULL DEFAULT 'xendit',
    provider_payment_id VARCHAR(128),
    provider_session_id VARCHAR(128),
    external_id VARCHAR(128) UNIQUE NOT NULL,
    plan VARCHAR(32) NOT NULL DEFAULT 'pro',
    amount INTEGER NOT NULL DEFAULT 49000,
    currency VARCHAR(8) NOT NULL DEFAULT 'IDR',
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    payment_method VARCHAR(64),
    payment_channel VARCHAR(64),
    checkout_url VARCHAR(512),
    expires_at TIMESTAMP WITH TIME ZONE,
    paid_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_payment_transactions_user_id ON payment_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_payment_transactions_external_id ON payment_transactions(external_id);
CREATE INDEX IF NOT EXISTS idx_payment_transactions_provider_payment_id ON payment_transactions(provider_payment_id);
CREATE INDEX IF NOT EXISTS idx_payment_transactions_status ON payment_transactions(status);
CREATE INDEX IF NOT EXISTS idx_payment_transactions_created_at ON payment_transactions(created_at);
