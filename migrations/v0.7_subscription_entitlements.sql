-- ==============================================================================
-- RAYTECH MONITOR v0.7 — SUBSCRIPTION ENTITLEMENTS MIGRATION
-- Non-destructive, idempotent forward-only migration.
-- Preserves all existing users, monitors, checks, incidents, status pages, and preferences.
-- ==============================================================================

-- 1. Create user_subscriptions table if it does not already exist
CREATE TABLE IF NOT EXISTS user_subscriptions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan VARCHAR(32) NOT NULL DEFAULT 'free',
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    current_period_start TIMESTAMP WITH TIME ZONE,
    current_period_end TIMESTAMP WITH TIME ZONE,
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
    legacy_override BOOLEAN NOT NULL DEFAULT FALSE,
    override_reason VARCHAR(128),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_user_subscriptions_user_id ON user_subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_plan ON user_subscriptions(plan);
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_status ON user_subscriptions(status);

-- 2. Safely backfill existing users who do not have a subscription record yet.
-- Grandfather existing legacy users who already utilized advanced features prior to v0.7.
INSERT INTO user_subscriptions (user_id, plan, status, legacy_override, override_reason, started_at)
SELECT 
    u.id AS user_id,
    'free' AS plan,
    'active' AS status,
    CASE 
        WHEN (
            (SELECT COUNT(*) FROM monitors m WHERE m.user_id = u.id AND m.type = 'http') > 3
            OR (SELECT COUNT(*) FROM monitors m WHERE m.user_id = u.id AND m.type = 'heartbeat') > 1
            OR (SELECT COUNT(*) FROM status_pages sp WHERE sp.user_id = u.id) > 1
            OR EXISTS (SELECT 1 FROM monitors m WHERE m.user_id = u.id AND m.ssl_monitoring_enabled = TRUE)
            OR EXISTS (SELECT 1 FROM notification_preferences np WHERE np.user_id = u.id AND np.email_enabled = TRUE)
        ) THEN TRUE
        ELSE FALSE
    END AS legacy_override,
    CASE 
        WHEN (
            (SELECT COUNT(*) FROM monitors m WHERE m.user_id = u.id AND m.type = 'http') > 3
            OR (SELECT COUNT(*) FROM monitors m WHERE m.user_id = u.id AND m.type = 'heartbeat') > 1
            OR (SELECT COUNT(*) FROM status_pages sp WHERE sp.user_id = u.id) > 1
            OR EXISTS (SELECT 1 FROM monitors m WHERE m.user_id = u.id AND m.ssl_monitoring_enabled = TRUE)
            OR EXISTS (SELECT 1 FROM notification_preferences np WHERE np.user_id = u.id AND np.email_enabled = TRUE)
        ) THEN 'pre_v0.7_legacy_grandfathered'
        ELSE NULL
    END AS override_reason,
    CURRENT_TIMESTAMP AS started_at
FROM users u
ON CONFLICT (user_id) DO NOTHING;
