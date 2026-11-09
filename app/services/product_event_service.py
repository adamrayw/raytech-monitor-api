import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.product_event import ProductEvent


EVENT_CATEGORIES: dict[str, str] = {
    "user_signed_up": "account",
    "user_logged_in": "account",
    "user_logged_out": "account",
    "onboarding_started": "onboarding",
    "onboarding_completed": "onboarding",
    "first_monitor_created": "onboarding",
    "first_heartbeat_created": "onboarding",
    "first_status_page_created": "onboarding",
    "monitor_created": "monitoring",
    "monitor_deleted": "monitoring",
    "monitor_paused": "monitoring",
    "monitor_resumed": "monitoring",
    "heartbeat_created": "monitoring",
    "heartbeat_deleted": "monitoring",
    "status_page_created": "monitoring",
    "status_page_deleted": "monitoring",
    "ssl_monitor_enabled": "monitoring",
    "email_alerts_enabled": "monitoring",
    "pricing_viewed": "billing",
    "upgrade_clicked": "billing",
    "checkout_started": "billing",
    "payment_pending": "billing",
    "payment_succeeded": "billing",
    "payment_failed": "billing",
    "payment_expired": "billing",
    "pro_activated": "subscription",
    "pro_renewed": "subscription",
    "pro_expired": "subscription",
    "dashboard_viewed": "engagement",
    "incident_viewed": "engagement",
    "billing_viewed": "engagement",
    "status_page_viewed": "engagement",
    "feedback_opened": "feedback",
    "feedback_submitted": "feedback",
}

CLIENT_EVENT_NAMES = {
    "onboarding_started",
    "pricing_viewed",
    "upgrade_clicked",
    "dashboard_viewed",
    "incident_viewed",
    "billing_viewed",
    "status_page_viewed",
    "feedback_opened",
}

MEANINGFUL_ACTIVITY_EVENTS = {
    "user_logged_in",
    "dashboard_viewed",
    "incident_viewed",
    "billing_viewed",
    "status_page_viewed",
    "monitor_created",
    "monitor_deleted",
    "monitor_paused",
    "monitor_resumed",
    "heartbeat_created",
    "heartbeat_deleted",
    "status_page_created",
    "status_page_deleted",
    "ssl_monitor_enabled",
}

PROPERTY_ALLOWLIST: dict[str, set[str]] = {
    "user_signed_up": {"launch_cohort"},
    "onboarding_completed": {"time_to_activation_seconds"},
    "first_monitor_created": {"monitor_id", "time_to_activation_seconds"},
    "first_heartbeat_created": {"monitor_id"},
    "first_status_page_created": {"status_page_id"},
    "monitor_created": {"monitor_id", "monitor_type", "interval_seconds", "ssl_enabled"},
    "monitor_deleted": {"monitor_id", "monitor_type"},
    "monitor_paused": {"monitor_id", "monitor_type"},
    "monitor_resumed": {"monitor_id", "monitor_type"},
    "heartbeat_created": {"monitor_id", "expected_interval_seconds"},
    "heartbeat_deleted": {"monitor_id"},
    "status_page_created": {"status_page_id", "published"},
    "status_page_deleted": {"status_page_id"},
    "ssl_monitor_enabled": {"monitor_id"},
    "email_alerts_enabled": set(),
    "pricing_viewed": {"page_path", "plan"},
    "upgrade_clicked": {"page_path"},
    "checkout_started": {"transaction_id", "provider", "amount", "currency"},
    "payment_pending": {"transaction_id", "provider"},
    "payment_succeeded": {"transaction_id", "provider", "amount", "currency"},
    "payment_failed": {"transaction_id", "provider", "reason_code"},
    "payment_expired": {"transaction_id", "provider"},
    "pro_activated": {"transaction_id", "provider", "period_days"},
    "pro_renewed": {"transaction_id", "provider", "period_days"},
    "pro_expired": {"subscription_id"},
    "dashboard_viewed": {"page_path"},
    "incident_viewed": {"page_path", "incident_id"},
    "billing_viewed": {"page_path"},
    "status_page_viewed": {"page_path", "status_page_id"},
    "feedback_opened": {"page_path"},
    "feedback_submitted": {"feedback_id", "category", "rating"},
    "onboarding_started": {"page_path"},
    "user_logged_in": set(),
    "user_logged_out": set(),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sanitize_page_path(value: Any) -> str:
    path = str(value or "").strip().split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        return "/"
    return path[:255]


def validate_event_properties(event_name: str, properties: dict[str, Any] | None) -> dict[str, Any]:
    supplied = properties or {}
    allowed = PROPERTY_ALLOWLIST.get(event_name, set())
    unknown = set(supplied) - allowed
    if unknown:
        raise ValueError(f"Unsupported analytics properties: {', '.join(sorted(unknown))}")

    cleaned: dict[str, Any] = {}
    for key, value in supplied.items():
        if key == "page_path":
            cleaned[key] = sanitize_page_path(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            cleaned[key] = value
        else:
            raise ValueError(f"Unsupported analytics property value for {key}")
    return cleaned


def record_product_event(
    db: Session,
    *,
    event_name: str,
    user_id: int | None,
    source: str,
    properties: dict[str, Any] | None = None,
    anonymous_session_id: str | None = None,
    idempotency_key: str | None = None,
    created_at: datetime | None = None,
) -> ProductEvent:
    if event_name not in EVENT_CATEGORIES:
        raise ValueError("Unsupported product event")
    if user_id is None and not anonymous_session_id:
        raise ValueError("Product event requires a user or anonymous session")
    if idempotency_key and len(idempotency_key) > 160:
        raise ValueError("Analytics idempotency key is too long")

    cleaned = validate_event_properties(event_name, properties)
    event = ProductEvent(
        user_id=user_id,
        anonymous_session_id=anonymous_session_id,
        event_name=event_name,
        event_category=EVENT_CATEGORIES[event_name],
        properties=cleaned,
        source=source[:32],
        environment=os.environ.get("ENVIRONMENT", "production").strip().lower()[:32],
        idempotency_key=idempotency_key,
        created_at=created_at or utcnow(),
    )

    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
        return event
    except IntegrityError:
        if not idempotency_key:
            raise
        existing = db.scalar(
            select(ProductEvent).where(ProductEvent.idempotency_key == idempotency_key)
        )
        if existing is None:
            raise
        return existing


def record_daily_client_event(
    db: Session,
    *,
    event_name: str,
    user_id: int,
    properties: dict[str, Any] | None = None,
) -> ProductEvent:
    if event_name not in CLIENT_EVENT_NAMES:
        raise ValueError("Event is not accepted from the client")
    now = utcnow()
    page_path = sanitize_page_path((properties or {}).get("page_path"))
    return record_product_event(
        db,
        event_name=event_name,
        user_id=user_id,
        source="web",
        properties={**(properties or {}), "page_path": page_path},
        idempotency_key=f"client:{event_name}:{user_id}:{now.date().isoformat()}:{page_path}",
        created_at=now,
    )
