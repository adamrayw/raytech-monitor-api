import os

from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from fastapi import FastAPI
from sqlalchemy import text

from app.database import Base, engine
from app import models
from app.routes.monitors import router as monitors_router
from app.routes.incidents import router as incidents_router
from app.routes.notifications import router as notifications_router
from app.routes.auth import router as auth_router
from app.routes.heartbeat import router as heartbeat_router
from app.routes.settings import router as settings_router
from app.routes.status_pages import router as status_pages_router
from app.routes.public_status import router as public_status_router
from app.routes.subscription import router as subscription_router
from app.routes.billing import router as billing_router
from app.routes.webhooks import router as webhooks_router

app = FastAPI(
    title="Raytech Monitor API",
    version="0.1.0",
)

frontend_url = os.environ.get(
    "FRONTEND_URL",
    "https://monitor.raytech.cloud",
)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["SESSION_SECRET"],
    session_cookie="raytech_monitor_session",
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
    https_only=True,
)

allowed_origins = list({frontend_url, "http://localhost:3000", "http://127.0.0.1:3000"})

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_monitoring_enabled BOOLEAN NOT NULL DEFAULT FALSE;"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_warning_days INTEGER NOT NULL DEFAULT 14;"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_status VARCHAR(32) NOT NULL DEFAULT 'unknown';"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_next_check_at TIMESTAMP WITH TIME ZONE;"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_last_checked_at TIMESTAMP WITH TIME ZONE;"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_last_successful_check_at TIMESTAMP WITH TIME ZONE;"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_expires_at TIMESTAMP WITH TIME ZONE;"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_issuer VARCHAR(255);"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_subject VARCHAR(255);"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_serial_number VARCHAR(128);"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_fingerprint_sha256 VARCHAR(64);"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_valid_from TIMESTAMP WITH TIME ZONE;"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_error_type VARCHAR(64);"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS ssl_error_message TEXT;"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS expected_interval_seconds INTEGER;"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS grace_period_seconds INTEGER;"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMP WITH TIME ZONE;"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS next_expected_at TIMESTAMP WITH TIME ZONE;"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS deadline_at TIMESTAMP WITH TIME ZONE;"))
            conn.execute(text("ALTER TABLE monitors ADD COLUMN IF NOT EXISTS heartbeat_token_hash VARCHAR(64);"))
            conn.execute(text("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS resolution_reason VARCHAR(64);"))
            conn.execute(text("""
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
            """))
            conn.execute(text("ALTER TABLE user_subscriptions ADD COLUMN IF NOT EXISTS legacy_override BOOLEAN NOT NULL DEFAULT FALSE;"))
            conn.execute(text("ALTER TABLE user_subscriptions ADD COLUMN IF NOT EXISTS override_reason VARCHAR(128);"))
            conn.execute(text("""
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
            """))
            try:
                conn.execute(text("ALTER TABLE monitors ALTER COLUMN url DROP NOT NULL;"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE email_deliveries ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128) UNIQUE;"))
            except Exception:
                pass
            conn.commit()
    except Exception:
        pass

    try:
        from app.services.xendit_service import validate_xendit_environment
        validate_xendit_environment()
    except Exception as exc:
        logger.warning(f"Xendit environment check: {exc}")


app.include_router(monitors_router)
app.include_router(incidents_router)
app.include_router(notifications_router)
app.include_router(auth_router)
app.include_router(heartbeat_router)
app.include_router(settings_router)
app.include_router(status_pages_router)
app.include_router(public_status_router)
app.include_router(subscription_router)
app.include_router(billing_router)
app.include_router(webhooks_router)

@app.get("/")
def root():
    return {
        "service": "raytech-monitor-api",
        "message": "Raytech Monitor API",
    }


@app.get("/health")
def health():
    database_status = "disconnected"

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        database_status = "connected"
    except Exception:
        database_status = "disconnected"

    return {
        "status": "ok" if database_status == "connected" else "degraded",
        "service": "raytech-monitor-api",
        "version": "0.1.0",
        "database": database_status,
    }
