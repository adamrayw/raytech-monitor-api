-- Migration: v0.2 SSL Certificate Monitoring
-- Safe forward migration adding SSL configuration and status fields to monitors table,
-- creating ssl_checks history table, and ssl_notification_logs deduplication table.

-- 1. Add SSL columns to monitors table
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_monitoring_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_warning_days INTEGER NOT NULL DEFAULT 14;
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_status VARCHAR(32) NOT NULL DEFAULT 'unknown';
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_next_check_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_last_checked_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_last_successful_check_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_expires_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_issuer VARCHAR(255);
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_subject VARCHAR(255);
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_serial_number VARCHAR(128);
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_fingerprint_sha256 VARCHAR(64);
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_valid_from TIMESTAMP WITH TIME ZONE;
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_error_type VARCHAR(64);
ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_error_message TEXT;

-- 2. Indexes for monitors table
CREATE INDEX IF NOT EXISTS ix_monitors_ssl_monitoring_enabled ON monitors (ssl_monitoring_enabled);
CREATE INDEX IF NOT EXISTS ix_monitors_ssl_status ON monitors (ssl_status);
CREATE INDEX IF NOT EXISTS ix_monitors_ssl_next_check_at ON monitors (ssl_next_check_at);

-- 3. Create ssl_checks table for certificate check history
CREATE TABLE IF NOT EXISTS ssl_checks (
    id SERIAL PRIMARY KEY,
    monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    checked_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    status VARCHAR(32) NOT NULL,
    issuer VARCHAR(255),
    subject VARCHAR(255),
    valid_from TIMESTAMP WITH TIME ZONE,
    expires_at TIMESTAMP WITH TIME ZONE,
    fingerprint_sha256 VARCHAR(64),
    serial_number VARCHAR(128),
    error_type VARCHAR(64),
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS ix_ssl_checks_monitor_id ON ssl_checks (monitor_id);
CREATE INDEX IF NOT EXISTS ix_ssl_checks_checked_at ON ssl_checks (checked_at);
CREATE INDEX IF NOT EXISTS ix_ssl_checks_monitor_id_checked_at ON ssl_checks (monitor_id, checked_at);

-- 4. Create ssl_notification_logs table for milestone alert deduplication
CREATE TABLE IF NOT EXISTS ssl_notification_logs (
    id SERIAL PRIMARY KEY,
    monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    cert_identifier VARCHAR(128) NOT NULL,
    milestone VARCHAR(64) NOT NULL,
    notified_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    notification_id INTEGER REFERENCES notifications(id) ON DELETE SET NULL,
    CONSTRAINT uq_ssl_notification_dedup UNIQUE (monitor_id, cert_identifier, milestone)
);

CREATE INDEX IF NOT EXISTS ix_ssl_notification_logs_monitor_id ON ssl_notification_logs (monitor_id);
CREATE INDEX IF NOT EXISTS ix_ssl_notification_logs_cert_identifier ON ssl_notification_logs (cert_identifier);
