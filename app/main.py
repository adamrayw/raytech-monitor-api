import logging
import os

from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.database import engine
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
from app.routes.admin import router as admin_router

logger = logging.getLogger("raytech.api")

app = FastAPI(
    title="Raytech Monitor API",
    version="1.0.0",
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

# Production environment isolates CORS exclusively to FRONTEND_URL
env = os.environ.get("ENVIRONMENT", "production").strip().lower()
if env in ("development", "dev", "test"):
    allowed_origins = list({frontend_url, "http://localhost:3000", "http://127.0.0.1:3000"})
else:
    allowed_origins = [frontend_url]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class PayloadTooLargeError(BaseException):
    """Raised when streaming/chunked request payload exceeds max allowed size.

    Inherits from BaseException to avoid being intercepted by standard
    'except Exception' blocks in intermediate route handlers.
    """
    pass


class SecurityHeadersAndBodyLimitMiddleware:
    """Adds standard OWASP security headers and enforces request body size limits at the ASGI layer.

    Protects against denial-of-service from unbounded streaming/chunked requests,
    missing Content-Length headers, and oversized payloads without buffering
    excessive bytes into server memory.
    """

    MAX_BODY_SIZE = 2 * 1024 * 1024  # 2 Megabytes

    def __init__(self, app: ASGIApp, max_body_size: int | None = None):
        self.app = app
        self.max_body_size = (
            max_body_size if max_body_size is not None else self.MAX_BODY_SIZE
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(raw=scope.get("headers", []))
        content_length_header = headers.get("content-length")

        if content_length_header is not None:
            try:
                cl = int(content_length_header)
                if cl < 0:
                    await self._send_json_error(
                        scope,
                        send,
                        status.HTTP_400_BAD_REQUEST,
                        "Invalid content-length header",
                    )
                    return
                if cl > self.max_body_size:
                    await self._send_json_error(
                        scope,
                        send,
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        "Payload exceeds maximum allowed size of 2MB.",
                    )
                    return
            except ValueError:
                await self._send_json_error(
                    scope,
                    send,
                    status.HTTP_400_BAD_REQUEST,
                    "Invalid content-length header",
                )
                return

        received_bytes = 0
        response_started = False

        async def limited_receive():
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                received_bytes += len(chunk)
                if received_bytes > self.max_body_size:
                    raise PayloadTooLargeError()
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                mutable = MutableHeaders(raw=list(message.get("headers", [])))
                mutable.setdefault("x-content-type-options", "nosniff")
                mutable.setdefault("x-frame-options", "DENY")
                mutable.setdefault("referrer-policy", "strict-origin-when-cross-origin")
                mutable.setdefault(
                    "permissions-policy", "geolocation=(), camera=(), microphone=()"
                )
                message["headers"] = mutable.raw
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except PayloadTooLargeError:
            if not response_started:
                await self._send_json_error(
                    scope,
                    send,
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    "Payload exceeds maximum allowed size of 2MB.",
                )
            else:
                raise

    async def _send_json_error(
        self, scope: Scope, send: Send, status_code: int, error: str
    ) -> None:
        res = JSONResponse(
            {"error": error},
            status_code=status_code,
            headers={
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "strict-origin-when-cross-origin",
                "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
            },
        )
        await res(scope, lambda: None, send)


app.add_middleware(SecurityHeadersAndBodyLimitMiddleware)

@app.on_event("startup")
def startup():
    from app.services.qrisgate_service import validate_qrisgate_environment

    validate_qrisgate_environment()


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
app.include_router(webhooks_router, prefix="/api")
app.include_router(admin_router)

# Explicit aliases required by QRISGate integration specs:
from app.routes.billing import CheckoutRequest, create_checkout_session, get_order_by_id
from app.dependencies.auth import get_current_user
from app.database import get_db
from app.models.user import User
from sqlalchemy.orm import Session
from typing import Any

@app.post("/api/checkout", tags=["billing"], summary="Direct QRISGate checkout endpoint")
def api_checkout_alias(
    payload: CheckoutRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Alias for POST /billing/checkout fulfilling Task 1 specification."""
    return create_checkout_session(payload=payload, db=db, current_user=current_user)

@app.get("/api/v1/transactions/{transaction_id_or_external_id}", tags=["billing"], summary="Transaction polling endpoint")
def api_v1_transaction_status(
    transaction_id_or_external_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Authenticated alias for transaction order status and polling."""
    return get_order_by_id(
        order_id=transaction_id_or_external_id,
        db=db,
        current_user=current_user,
    )

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

    status_code = status.HTTP_200_OK if database_status == "connected" else status.HTTP_503_SERVICE_UNAVAILABLE

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if database_status == "connected" else "degraded",
            "service": "raytech-monitor-api",
            "version": "1.0.0",
            "database": database_status,
        },
    )
