import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.monitor import Monitor
from app.models.status_page import StatusPage
from app.models.subscription import UserSubscription
from app.models.subscription_audit import SubscriptionAuditLog
from app.models.user import User

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


PLAN_CONFIG: dict[str, dict[str, Any]] = {
    "free": {
        "id": "free",
        "name": "Free",
        "price_display": "Rp0",
        "price_raw": 0,
        "billing_period": "free forever",
        "description": "Essential monitoring for personal projects and small services.",
        "max_http_monitors": 3,
        "min_http_interval_seconds": 300,
        "min_http_interval_display": "5 minutes",
        "max_heartbeat_monitors": 1,
        "max_status_pages": 1,
        "ssl_monitoring": False,
        "email_alerts": False,
        "in_app_notifications": True,
        "incident_history_days": 30,
        "features": [
            "3 HTTP monitors",
            "5-minute check intervals",
            "1 Heartbeat monitor",
            "1 Public Status Page",
            "Basic incident history",
            "In-app notifications",
        ],
    },
    "pro": {
        "id": "pro",
        "name": "Pro",
        "price_display": "Rp49.000",
        "price_raw": 49000,
        "billing_period": "/ month",
        "description": "Advanced monitoring and alerting for production applications.",
        "max_http_monitors": 20,
        "min_http_interval_seconds": 60,
        "min_http_interval_display": "1 minute",
        "max_heartbeat_monitors": 20,
        "max_status_pages": 5,
        "ssl_monitoring": True,
        "email_alerts": True,
        "in_app_notifications": True,
        "incident_history_days": 90,
        "features": [
            "20 HTTP monitors",
            "1-minute check intervals",
            "20 Heartbeat monitors",
            "5 Public Status Pages",
            "SSL certificate monitoring",
            "Email alerts",
            "90-day incident retention (planned)",
        ],
    },
}


def ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def record_subscription_audit_event(
    db: Session,
    user_id: int,
    subscription_id: int,
    previous_plan: str,
    new_plan: str,
    event_type: str,
    reason: str | None = None,
    payment_transaction_id: int | None = None,
    commit: bool = True,
) -> SubscriptionAuditLog:
    """Records an auditable subscription lifecycle transition."""
    audit = SubscriptionAuditLog(
        user_id=user_id,
        subscription_id=subscription_id,
        previous_plan=previous_plan,
        new_plan=new_plan,
        event_type=event_type,
        reason=reason,
        payment_transaction_id=payment_transaction_id,
        created_at=utcnow(),
    )
    db.add(audit)
    if commit:
        db.commit()
    else:
        db.flush()
    logger.info(
        f"Subscription audit event recorded: user {user_id}, {event_type} ({previous_plan} -> {new_plan})"
    )
    return audit


def get_or_create_subscription(db: Session, user_id: int, commit: bool = True) -> UserSubscription:
    """Retrieves existing user subscription or initializes default Free tier.

    Always falls back to Free plan if none exists or on initial onboarding.
    When commit=False, flushes changes without committing so outer transaction retains atomicity.
    """
    sub = db.scalar(
        select(UserSubscription).where(UserSubscription.user_id == user_id)
    )
    if sub is not None:
        return sub

    # Create default Free subscription
    new_sub = UserSubscription(
        user_id=user_id,
        plan="free",
        status="active",
        started_at=utcnow(),
        cancel_at_period_end=False,
        legacy_override=False,
    )
    if not commit:
        db.add(new_sub)
        db.flush()
        return new_sub

    try:
        db.add(new_sub)
        db.commit()
        db.refresh(new_sub)
        return new_sub
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(UserSubscription).where(UserSubscription.user_id == user_id)
        )
        if existing is not None:
            return existing
        # Fallback in-memory object (never grants Pro)
        return UserSubscription(
            user_id=user_id,
            plan="free",
            status="active",
            started_at=utcnow(),
        )


def get_effective_plan(db: Session, user_id: int) -> tuple[str, UserSubscription]:
    """Returns effective plan id ('free' or 'pro') and subscription record.

    Never interprets missing or error state as Pro.
    Automatically checks and enforces expiration on paid Pro subscriptions even if worker is delayed.
    """
    try:
        sub = get_or_create_subscription(db, user_id)

        # Evaluate automatic expiration for paid Pro (without grandfathered legacy_override)
        if sub.plan == "pro" and not getattr(sub, "legacy_override", False):
            if sub.current_period_end is not None:
                now = utcnow()
                end_time = sub.current_period_end if sub.current_period_end.tzinfo else sub.current_period_end.replace(tzinfo=timezone.utc)
                if now > end_time:
                    logger.info(f"User {user_id} Pro subscription expired at {sub.current_period_end}. Downgrading to Free.")
                    previous_plan = sub.plan
                    sub.plan = "free"
                    sub.status = "expired"
                    sub.updated_at = now

                    record_subscription_audit_event(
                        db=db,
                        user_id=user_id,
                        subscription_id=sub.id,
                        previous_plan=previous_plan,
                        new_plan="free",
                        event_type="pro_expired",
                        reason="period_ended",
                        commit=False,
                    )

                    user = db.get(User, user_id)
                    if user:
                        from app.services.email_service import enqueue_subscription_expired_email
                        enqueue_subscription_expired_email(db, user, sub)

                    try:
                        db.commit()
                        db.refresh(sub)
                    except Exception as e:
                        db.rollback()
                        logger.warning(f"Failed to persist expired status for user {user_id}: {e}")

        plan_id = sub.plan.lower() if sub.plan else "free"
        if plan_id not in PLAN_CONFIG:
            plan_id = "free"
        return plan_id, sub
    except Exception as exc:
        logger.error(f"Error resolving effective plan for user {user_id}: {exc}")
        return "free", UserSubscription(user_id=user_id, plan="free", status="active")


def get_entitlements(db: Session, user_id: int) -> dict[str, Any]:
    """Returns the full entitlement dictionary for the user's plan.

    If legacy_override is True on the subscription, premium features are unlocked.
    """
    plan_id, sub = get_effective_plan(db, user_id)
    base_entitlements = dict(PLAN_CONFIG.get(plan_id, PLAN_CONFIG["free"]))

    if getattr(sub, "legacy_override", False):
        base_entitlements["max_http_monitors"] = max(base_entitlements["max_http_monitors"], 20)
        base_entitlements["min_http_interval_seconds"] = 60
        base_entitlements["min_http_interval_display"] = "1 minute"
        base_entitlements["max_heartbeat_monitors"] = max(base_entitlements["max_heartbeat_monitors"], 20)
        base_entitlements["max_status_pages"] = max(base_entitlements["max_status_pages"], 5)
        base_entitlements["ssl_monitoring"] = True
        base_entitlements["email_alerts"] = True

    return base_entitlements


def get_subscription_usage(db: Session, user_id: int) -> dict[str, Any]:
    """Calculates active usage of HTTP monitors, Heartbeat monitors, and Status Pages.

    Paused monitors still count toward quota as provisioned resources.
    """
    plan_id, sub = get_effective_plan(db, user_id)
    entitlements = get_entitlements(db, user_id)

    http_used = (
        db.scalar(
            select(func.count(Monitor.id)).where(
                Monitor.user_id == user_id,
                Monitor.type == "http",
            )
        )
        or 0
    )

    heartbeat_used = (
        db.scalar(
            select(func.count(Monitor.id)).where(
                Monitor.user_id == user_id,
                Monitor.type == "heartbeat",
            )
        )
        or 0
    )

    status_pages_used = (
        db.scalar(
            select(func.count(StatusPage.id)).where(
                StatusPage.user_id == user_id,
            )
        )
        or 0
    )

    http_limit = entitlements["max_http_monitors"]
    heartbeat_limit = entitlements["max_heartbeat_monitors"]
    status_pages_limit = entitlements["max_status_pages"]

    return {
        "plan": plan_id,
        "name": entitlements["name"],
        "status": getattr(sub, "status", "active"),
        "legacy_override": getattr(sub, "legacy_override", False),
        "http_monitors": {
            "used": http_used,
            "limit": http_limit,
            "active_count": min(http_used, http_limit),
            "restricted_count": max(0, http_used - http_limit),
            "over_limit": http_used > http_limit,
            "at_limit": http_used >= http_limit,
            "remaining": max(0, http_limit - http_used),
        },
        "heartbeat_monitors": {
            "used": heartbeat_used,
            "limit": heartbeat_limit,
            "active_count": min(heartbeat_used, heartbeat_limit),
            "restricted_count": max(0, heartbeat_used - heartbeat_limit),
            "over_limit": heartbeat_used > heartbeat_limit,
            "at_limit": heartbeat_used >= heartbeat_limit,
            "remaining": max(0, heartbeat_limit - heartbeat_used),
        },
        "status_pages": {
            "used": status_pages_used,
            "limit": status_pages_limit,
            "active_count": min(status_pages_used, status_pages_limit),
            "restricted_count": max(0, status_pages_used - status_pages_limit),
            "over_limit": status_pages_used > status_pages_limit,
            "at_limit": status_pages_used >= status_pages_limit,
            "remaining": max(0, status_pages_limit - status_pages_used),
        },
        "ssl_monitoring_enabled": entitlements["ssl_monitoring"],
        "email_alerts_enabled": entitlements["email_alerts"],
        "min_http_interval_seconds": entitlements["min_http_interval_seconds"],
    }


def _acquire_user_quota_lock(db: Session, user_id: int):
    """Acquires a row-level lock on the user record in PostgreSQL to avoid race conditions.

    In SQLite (unit tests), SELECT FOR UPDATE is safely skipped.
    """
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(
            select(User.id).where(User.id == user_id).with_for_update()
        )


def enforce_monitor_creation_quota(db: Session, user_id: int, monitor_type: str):
    """Enforces monitor quota with concurrency lock.

    Raises HTTP 403 with code PLAN_LIMIT_EXCEEDED if limit reached.
    """
    _acquire_user_quota_lock(db, user_id)
    entitlements = get_entitlements(db, user_id)

    if monitor_type == "heartbeat":
        max_allowed = entitlements["max_heartbeat_monitors"]
        current_count = (
            db.scalar(
                select(func.count(Monitor.id)).where(
                    Monitor.user_id == user_id,
                    Monitor.type == "heartbeat",
                )
            )
            or 0
        )
        if current_count >= max_allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "PLAN_LIMIT_EXCEEDED",
                    "message": f"Your current plan allows a maximum of {max_allowed} Heartbeat monitor(s). Please upgrade to Pro for additional quota.",
                    "resource": "heartbeat_monitors",
                    "limit": max_allowed,
                    "used": current_count,
                    "plan": entitlements["id"],
                },
            )
    else:
        max_allowed = entitlements["max_http_monitors"]
        current_count = (
            db.scalar(
                select(func.count(Monitor.id)).where(
                    Monitor.user_id == user_id,
                    Monitor.type == "http",
                )
            )
            or 0
        )
        if current_count >= max_allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "PLAN_LIMIT_EXCEEDED",
                    "message": f"Your current plan allows a maximum of {max_allowed} HTTP monitor(s). Please upgrade to Pro for additional quota.",
                    "resource": "http_monitors",
                    "limit": max_allowed,
                    "used": current_count,
                    "plan": entitlements["id"],
                },
            )


def enforce_http_interval(db: Session, user_id: int, requested_interval: int):
    """Enforces minimum HTTP check interval.

    Free: minimum 300 seconds (5m)
    Pro: minimum 60 seconds (1m)
    """
    entitlements = get_entitlements(db, user_id)
    min_allowed = entitlements["min_http_interval_seconds"]

    if requested_interval < min_allowed:
        min_mins = min_allowed // 60
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "PLAN_LIMIT_EXCEEDED",
                "message": f"Your current plan requires a minimum check interval of {min_mins} minute(s) ({min_allowed} seconds). Upgrade to Pro for 1-minute intervals.",
                "min_interval": min_allowed,
                "requested_interval": requested_interval,
                "plan": entitlements["id"],
            },
        )


def enforce_status_page_quota(db: Session, user_id: int):
    """Enforces Public Status Page quota on creation.

    Unpublished drafts count toward quota. Deleting a page releases quota.
    """
    _acquire_user_quota_lock(db, user_id)
    entitlements = get_entitlements(db, user_id)
    max_allowed = entitlements["max_status_pages"]

    current_count = (
        db.scalar(
            select(func.count(StatusPage.id)).where(
                StatusPage.user_id == user_id,
            )
        )
        or 0
    )

    if current_count >= max_allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "PLAN_LIMIT_EXCEEDED",
                "message": f"Your current plan allows a maximum of {max_allowed} Public Status Page(s). Upgrade to Pro for up to 5 status pages.",
                "resource": "status_pages",
                "limit": max_allowed,
                "used": current_count,
                "plan": entitlements["id"],
            },
        )


def enforce_ssl_access(db: Session, user_id: int, existing_monitor: Monitor | None = None):
    """Enforces SSL Certificate Monitoring entitlement.

    New configurations require Pro. Grandfathered existing configurations remain active.
    """
    # If updating an existing monitor that already had SSL enabled, preserve it
    if existing_monitor is not None and existing_monitor.ssl_monitoring_enabled:
        return

    entitlements = get_entitlements(db, user_id)
    if not entitlements["ssl_monitoring"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "PLAN_FEATURE_LOCKED",
                "message": "SSL Certificate Monitoring is available on the Pro plan.",
                "feature": "ssl_monitoring",
                "plan": entitlements["id"],
            },
        )


def enforce_email_access(db: Session, user_id: int):
    """Enforces Email Alerts entitlement on enabling email settings or test emails."""
    entitlements = get_entitlements(db, user_id)
    if not entitlements["email_alerts"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "PLAN_FEATURE_LOCKED",
                "message": "Email alerts are available on the Pro plan. In-app notifications remain fully active on your Free plan.",
                "feature": "email_alerts",
                "plan": entitlements["id"],
            },
        )


def is_user_email_entitled(db: Session, user_id: int) -> bool:
    """Helper used during email delivery dispatch to check if user is entitled or grandfathered."""
    try:
        entitlements = get_entitlements(db, user_id)
        return bool(entitlements.get("email_alerts", False))
    except Exception:
        return False


def switch_user_plan_dev(
    db: Session,
    user_id: int,
    new_plan: str,
    reason: str = "dev_admin_switch",
    legacy_override: bool = False,
) -> UserSubscription:
    """Development/testing helper for safely switching user plan between 'free' and 'pro'.

    Strictly gated by environment in the route layer.
    """
    plan_clean = new_plan.strip().lower()
    if plan_clean not in PLAN_CONFIG:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid plan '{new_plan}'. Must be one of: {list(PLAN_CONFIG.keys())}",
        )

    sub = get_or_create_subscription(db, user_id, commit=False)
    previous_plan = sub.plan
    sub.plan = plan_clean
    sub.status = "active"
    sub.legacy_override = legacy_override
    sub.override_reason = reason
    sub.updated_at = utcnow()
    if plan_clean == "pro":
        sub.current_period_start = utcnow()
        sub.current_period_end = utcnow() + timedelta(days=30)
    else:
        sub.current_period_start = None
        sub.current_period_end = None

    event_type = "admin_override_enabled" if legacy_override else (
        "pro_activated" if plan_clean == "pro" else "pro_downgraded"
    )

    record_subscription_audit_event(
        db=db,
        user_id=user_id,
        subscription_id=sub.id,
        previous_plan=previous_plan,
        new_plan=plan_clean,
        event_type=event_type,
        reason=reason,
        commit=False,
    )

    db.commit()
    db.refresh(sub)
    logger.info(f"User {user_id} subscription plan changed to '{plan_clean}' (legacy_override={legacy_override}, reason: {reason})")
    return sub


def activate_pro_from_payment(
    db: Session,
    user_id: int,
    transaction_id: int,
    duration_days: int = 30,
    commit: bool = True,
) -> UserSubscription:
    """Activates or extends 30 days of Pro entitlements after a verified payment.

    Cases handled:
    A. Free -> Pro: Start Pro access; set expiration = payment activation time + 30 days.
    B. Active Pro -> Extended Pro: New expiration = previous current_period_end + 30 days. Never discards remaining time.
    C. Expired Pro -> Pro: New expiration = payment activation time + 30 days.
    D. Grandfathered legacy accounts: Preserves legacy_override while updating payment period end.

    Concurrency:
    Locks subscription row in PostgreSQL to serialize concurrent renewal and background expiration jobs.
    Records SubscriptionAuditLog atomically.
    When commit=False, caller owns the database transaction and flushes changes without committing.
    """
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(
            select(UserSubscription.id)
            .where(UserSubscription.user_id == user_id)
            .with_for_update()
        )

    sub = get_or_create_subscription(db, user_id, commit=commit)
    now = utcnow()
    previous_plan = sub.plan

    cur_end = ensure_utc(sub.current_period_end)
    if getattr(sub, "legacy_override", False):
        sub.plan = "pro"
        sub.status = "active"
        sub.current_period_start = now
        sub.current_period_end = now + timedelta(days=duration_days)
        sub.override_reason = f"xendit_payment:{transaction_id} (legacy grandfathered)"
        event_type = "pro_extended" if previous_plan == "pro" else "pro_activated"
    elif sub.plan == "pro" and cur_end is not None and cur_end > now:
        # Active paid Pro; extend end date from current_period_end
        sub.current_period_end = cur_end + timedelta(days=duration_days)
        sub.override_reason = f"xendit_payment:{transaction_id}"
        event_type = "pro_extended"
    else:
        # Free tier or expired Pro
        sub.plan = "pro"
        sub.status = "active"
        sub.current_period_start = now
        sub.current_period_end = now + timedelta(days=duration_days)
        sub.override_reason = f"xendit_payment:{transaction_id}"
        event_type = "pro_activated"

    sub.cancel_at_period_end = False
    sub.updated_at = now

    record_subscription_audit_event(
        db=db,
        user_id=user_id,
        subscription_id=sub.id,
        previous_plan=previous_plan,
        new_plan="pro",
        event_type=event_type,
        reason=f"xendit_payment:{transaction_id}",
        payment_transaction_id=transaction_id,
        commit=False,
    )

    if commit:
        db.commit()
        db.refresh(sub)
    else:
        db.flush()

    logger.info(f"User {user_id} {event_type} Pro subscription until {sub.current_period_end} via tx {transaction_id} (commit={commit})")
    return sub


def get_eligible_resource_ids(
    db: Session,
    user_id: int,
    resource_type: str = "http",
) -> set[int]:
    """Returns the set of resource IDs eligible under the user's active plan limits.

    Deterministic policy:
    1. Pro users and grandfathered legacy_override users receive full Pro quotas (20 HTTP, 20 Heartbeat, 5 Status Pages).
    2. Free users are restricted to Free quotas (3 HTTP, 1 Heartbeat, 1 Status Page).
    3. Eligible resources are selected deterministically by earliest creation: ORDER BY created_at ASC, id ASC.
    4. Resources beyond the quota remain safely saved but are marked as plan-restricted and skipped from worker checks.
    """
    entitlements = get_entitlements(db, user_id)

    if resource_type == "http":
        limit = entitlements["max_http_monitors"]
        ids = db.scalars(
            select(Monitor.id)
            .where(
                Monitor.user_id == user_id,
                Monitor.type == "http",
            )
            .order_by(Monitor.created_at.asc(), Monitor.id.asc())
            .limit(limit)
        ).all()
        return set(ids)

    elif resource_type == "heartbeat":
        limit = entitlements["max_heartbeat_monitors"]
        ids = db.scalars(
            select(Monitor.id)
            .where(
                Monitor.user_id == user_id,
                Monitor.type == "heartbeat",
            )
            .order_by(Monitor.created_at.asc(), Monitor.id.asc())
            .limit(limit)
        ).all()
        return set(ids)

    elif resource_type == "status_page":
        limit = entitlements["max_status_pages"]
        ids = db.scalars(
            select(StatusPage.id)
            .where(StatusPage.user_id == user_id)
            .order_by(StatusPage.created_at.asc(), StatusPage.id.asc())
            .limit(limit)
        ).all()
        return set(ids)

    return set()


def process_subscription_expirations(db: Session) -> dict[str, Any]:
    """Evaluates and processes all expired Pro subscriptions.

    Requirements:
    1. Finds subscriptions with plan='pro', current_period_end < now, legacy_override=False.
    2. Concurrency-safe: Locks row with SELECT FOR UPDATE to prevent race condition with renewals.
    3. Re-verifies expiration condition after acquiring lock.
    4. Transitions subscription to plan='free', status='expired'.
    5. Records audit event 'pro_expired'.
    6. Enqueues expiration confirmation email.
    7. Retains all user monitors, history, and payment records without deletion.
    """
    now = utcnow()

    query = (
        select(UserSubscription)
        .where(
            UserSubscription.plan == "pro",
            UserSubscription.current_period_end.is_not(None),
            UserSubscription.current_period_end <= now,
            UserSubscription.legacy_override.is_(False),
        )
    )

    try:
        bind = db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        expired_subs = db.scalars(query).all()
    except Exception:
        db.rollback()
        expired_subs = db.scalars(
            select(UserSubscription)
            .where(
                UserSubscription.plan == "pro",
                UserSubscription.current_period_end.is_not(None),
                UserSubscription.current_period_end <= now,
                UserSubscription.legacy_override.is_(False),
            )
        ).all()

    expired_count = 0

    for sub in expired_subs:
        end_tz = sub.current_period_end if sub.current_period_end.tzinfo else sub.current_period_end.replace(tzinfo=timezone.utc)
        if sub.plan != "pro" or sub.legacy_override or end_tz > now:
            continue

        previous_plan = sub.plan
        sub.plan = "free"
        sub.status = "expired"
        sub.updated_at = now

        record_subscription_audit_event(
            db=db,
            user_id=sub.user_id,
            subscription_id=sub.id,
            previous_plan=previous_plan,
            new_plan="free",
            event_type="pro_expired",
            reason="period_ended",
            commit=False,
        )

        user = db.get(User, sub.user_id)
        if user:
            from app.services.email_service import enqueue_subscription_expired_email
            enqueue_subscription_expired_email(db, user, sub)

        db.commit()
        expired_count += 1
        logger.info(f"Processed expiration for user {sub.user_id}, downgraded to Free.")

    return {"processed": len(expired_subs), "expired": expired_count}


def process_subscription_reminders(db: Session) -> dict[str, Any]:
    """Finds active Pro subscriptions nearing expiration and queues transactional reminders.

    Thresholds:
    - 7 days before expiration: sent when remaining time is between 6.0 and 7.5 days
    - 3 days before expiration: sent when remaining time is between 2.0 and 3.5 days
    - 1 day before expiration: sent when remaining time is between 0.0 and 1.5 days

    Deduplicated via database outbox key:
    'sub_reminder:{sub.id}:{sub.current_period_end_timestamp}:{threshold}d'
    Stale thresholds are not sent if workers were delayed.
    """
    now = utcnow()
    active_pro_subs = db.scalars(
        select(UserSubscription)
        .where(
            UserSubscription.plan == "pro",
            UserSubscription.current_period_end.is_not(None),
            UserSubscription.current_period_end > now,
            UserSubscription.legacy_override.is_(False),
        )
    ).all()

    queued_count = 0
    from app.services.email_service import enqueue_subscription_reminder_email

    for sub in active_pro_subs:
        end_tz = sub.current_period_end if sub.current_period_end.tzinfo else sub.current_period_end.replace(tzinfo=timezone.utc)
        delta = end_tz - now
        days_left = delta.total_seconds() / 86400.0

        user = db.get(User, sub.user_id)
        if not user or not user.email:
            continue

        # Evaluate 1-day threshold (0.0 <= days_left <= 1.5)
        if 0.0 <= days_left <= 1.5:
            d = enqueue_subscription_reminder_email(db, user, sub, threshold_days=1)
            if d:
                db.commit()
                queued_count += 1

        # Evaluate 3-day threshold (2.0 <= days_left <= 3.5)
        elif 2.0 <= days_left <= 3.5:
            d = enqueue_subscription_reminder_email(db, user, sub, threshold_days=3)
            if d:
                db.commit()
                queued_count += 1

        # Evaluate 7-day threshold (6.0 <= days_left <= 7.5)
        elif 6.0 <= days_left <= 7.5:
            d = enqueue_subscription_reminder_email(db, user, sub, threshold_days=7)
            if d:
                db.commit()
                queued_count += 1

    return {"active_pro_count": len(active_pro_subs), "reminders_queued": queued_count}


def run_subscription_lifecycle_jobs(db: Session | None = None) -> dict[str, Any]:
    """Runs all subscription lifecycle background maintenance tasks."""
    from app.database import SessionLocal
    close_session = False
    if db is None:
        db = SessionLocal()
        close_session = True

    try:
        exp_res = process_subscription_expirations(db)
        rem_res = process_subscription_reminders(db)
        return {
            "expirations": exp_res,
            "reminders": rem_res,
        }
    finally:
        if close_session:
            db.close()


