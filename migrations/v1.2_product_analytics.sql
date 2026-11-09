-- First-party product analytics, early-user cohort tagging, and feedback.
-- Financial records and operational histories are not modified.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS launch_cohort VARCHAR(16);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_users_launch_cohort'
          AND conrelid = 'users'::regclass
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT ck_users_launch_cohort
            CHECK (launch_cohort IS NULL OR launch_cohort IN ('internal', 'beta', 'public'));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_users_launch_cohort
    ON users (launch_cohort);

CREATE TABLE IF NOT EXISTS product_events (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    anonymous_session_id VARCHAR(64),
    event_name VARCHAR(64) NOT NULL,
    event_category VARCHAR(32) NOT NULL,
    properties JSONB NOT NULL DEFAULT '{}'::jsonb,
    source VARCHAR(32) NOT NULL,
    environment VARCHAR(32) NOT NULL,
    idempotency_key VARCHAR(160),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_events_idempotency_key UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_product_events_event_created
    ON product_events (event_name, created_at);

CREATE INDEX IF NOT EXISTS ix_product_events_user_created
    ON product_events (user_id, created_at);

CREATE INDEX IF NOT EXISTS ix_product_events_created_at
    ON product_events (created_at);

CREATE TABLE IF NOT EXISTS user_feedback (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category VARCHAR(32) NOT NULL,
    message TEXT NOT NULL,
    rating INTEGER,
    page_path VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'open',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_user_feedback_category
        CHECK (category IN ('bug', 'feature_request', 'user_experience', 'payment', 'other')),
    CONSTRAINT ck_user_feedback_rating
        CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
    CONSTRAINT ck_user_feedback_status
        CHECK (status IN ('open', 'reviewed', 'closed'))
);

CREATE INDEX IF NOT EXISTS ix_user_feedback_user_id
    ON user_feedback (user_id);

CREATE INDEX IF NOT EXISTS ix_user_feedback_status_created
    ON user_feedback (status, created_at DESC);
