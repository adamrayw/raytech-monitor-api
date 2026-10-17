-- RAYTECH MONITOR v0.4 — Transactional Email Alerts Forward Migration
-- Safe, non-destructive migration script for PostgreSQL

-- 1. Create notification_preferences table
CREATE TABLE IF NOT EXISTS notification_preferences (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    http_down_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    http_recovered_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    heartbeat_missed_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    heartbeat_recovered_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ssl_expiring_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ssl_expired_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ssl_invalid_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ssl_recovered_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_notification_preferences_user UNIQUE (user_id)
);

CREATE INDEX IF NOT EXISTS idx_notification_preferences_user ON notification_preferences(user_id);

-- 2. Create email_deliveries outbox table
CREATE TABLE IF NOT EXISTS email_deliveries (
    id BIGSERIAL PRIMARY KEY,
    notification_id INTEGER REFERENCES notifications(id) ON DELETE SET NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipient_email VARCHAR(255) NOT NULL,
    provider VARCHAR(32) NOT NULL DEFAULT 'resend',
    event_type VARCHAR(64) NOT NULL,
    subject VARCHAR(255) NOT NULL,
    body_text TEXT,
    body_html TEXT,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    provider_message_id VARCHAR(255),
    last_error_code VARCHAR(64),
    last_error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_email_deliveries_notification_recipient UNIQUE (notification_id, recipient_email)
);

-- 3. Create indexes for fast worker polling, user history, and lookups
CREATE INDEX IF NOT EXISTS idx_email_deliveries_outbox ON email_deliveries (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_email_deliveries_user_history ON email_deliveries (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_deliveries_notification ON email_deliveries (notification_id);
