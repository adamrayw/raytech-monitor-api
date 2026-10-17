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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[frontend_url],
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
            try:
                conn.execute(text("ALTER TABLE monitors ALTER COLUMN url DROP NOT NULL;"))
            except Exception:
                pass
            conn.commit()
    except Exception:
        pass


app.include_router(monitors_router)
app.include_router(incidents_router)
app.include_router(notifications_router)
app.include_router(auth_router)
app.include_router(heartbeat_router)

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
