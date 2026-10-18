-- RAYTECH MONITOR v0.9 — Subscription Lifecycle Management Forward Migration
-- Safe, non-destructive migration script for PostgreSQL

-- 1. Create subscription audit log table
CREATE TABLE IF NOT EXISTS subscription_audit_logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subscription_id INTEGER NOT NULL REFERENCES user_subscriptions(id) ON DELETE CASCADE,
    previous_plan VARCHAR(32) NOT NULL,
    new_plan VARCHAR(32) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    reason VARCHAR(255),
    payment_transaction_id INTEGER REFERENCES payment_transactions(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 2. Indexes for audit query performance
CREATE INDEX IF NOT EXISTS idx_sub_audit_user_id ON subscription_audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_sub_audit_sub_id ON subscription_audit_logs(subscription_id);
CREATE INDEX IF NOT EXISTS idx_sub_audit_event_type ON subscription_audit_logs(event_type);
CREATE INDEX IF NOT EXISTS idx_sub_audit_created_at ON subscription_audit_logs(created_at);
