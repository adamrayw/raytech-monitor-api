-- Production launch hardening for admin authorization, revenue classification,
-- QRISGate idempotency support, and worker observability.
-- Existing users and historical payment rows remain non-admin and UNKNOWN.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS ix_users_is_admin
    ON users (is_admin);

ALTER TABLE payment_transactions
    ADD COLUMN IF NOT EXISTS unique_code INTEGER;

ALTER TABLE payment_transactions
    ADD COLUMN IF NOT EXISTS total_amount INTEGER;

ALTER TABLE payment_transactions
    ADD COLUMN IF NOT EXISTS revenue_classification VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN';

CREATE INDEX IF NOT EXISTS ix_payment_transactions_revenue_classification
    ON payment_transactions (revenue_classification);


CREATE TABLE IF NOT EXISTS processed_webhook_events (
    event_id VARCHAR(128) PRIMARY KEY,
    provider VARCHAR(32) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS service_health (
    service VARCHAR(64) PRIMARY KEY,
    status VARCHAR(32) NOT NULL DEFAULT 'unknown',
    details TEXT,
    last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_payment_transactions_status'
    ) THEN
        ALTER TABLE payment_transactions
            ADD CONSTRAINT ck_payment_transactions_status
            CHECK (status IN ('PENDING', 'PAID', 'FAILED', 'EXPIRED', 'CANCELED'))
            NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_payment_transactions_revenue_classification'
    ) THEN
        ALTER TABLE payment_transactions
            ADD CONSTRAINT ck_payment_transactions_revenue_classification
            CHECK (revenue_classification IN ('REAL', 'SANDBOX', 'TEST', 'MANUAL', 'UNKNOWN'))
            NOT VALID;
    END IF;
END $$;
