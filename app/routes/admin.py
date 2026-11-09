from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import require_admin
from app.models.incident import Incident
from app.models.monitor import Monitor
from app.models.payment_transaction import PaymentTransaction
from app.models.service_health import ServiceHealth
from app.models.status_page import StatusPage
from app.models.subscription import UserSubscription
from app.models.user import User
from app.models.user_feedback import UserFeedback
from app.services.product_analytics_service import (
    build_admin_product_analytics,
    build_soft_launch_summary,
)

router = APIRouter(prefix="/admin", tags=["admin"])
JAKARTA = ZoneInfo("Asia/Jakarta")
REAL_REVENUE_FILTERS = (
    PaymentTransaction.status == "PAID",
    PaymentTransaction.provider == "qrisgate",
    PaymentTransaction.provider_mode == "live",
    PaymentTransaction.revenue_classification == "REAL",
)


def _count(db: Session, *filters: Any) -> int:
    return int(db.scalar(select(func.count()).where(*filters)) or 0)


def _sum(db: Session, *filters: Any) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(PaymentTransaction.amount), 0)).where(*filters)) or 0)


def _jakarta_boundaries(now: datetime) -> tuple[datetime, datetime]:
    local_now = now.astimezone(JAKARTA)
    day_start = datetime.combine(local_now.date(), time.min, JAKARTA).astimezone(timezone.utc)
    month_start = datetime(local_now.year, local_now.month, 1, tzinfo=JAKARTA).astimezone(timezone.utc)
    return day_start, month_start


def _restricted_monitor_count(db: Session, now: datetime) -> int:
    subscriptions = {
        sub.user_id: sub
        for sub in db.scalars(select(UserSubscription)).all()
    }
    grouped = db.execute(
        select(Monitor.user_id, Monitor.type, func.count(Monitor.id))
        .where(Monitor.type.in_(("http", "heartbeat")))
        .group_by(Monitor.user_id, Monitor.type)
    ).all()
    restricted = 0
    for user_id, monitor_type, count in grouped:
        sub = subscriptions.get(user_id)
        end = None
        if sub and sub.current_period_end:
            end = sub.current_period_end
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
        is_pro = bool(sub and (sub.legacy_override or (sub.plan == "pro" and sub.status == "active" and end and end > now)))
        limit = 20 if is_pro else (3 if monitor_type == "http" else 1)
        restricted += max(0, int(count) - limit)
    return restricted


@router.get("/overview")
def get_admin_overview(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    day_start, month_start = _jakarta_boundaries(now)

    active_pro_filters = (
        UserSubscription.plan == "pro",
        UserSubscription.status == "active",
        UserSubscription.current_period_end.is_not(None),
        UserSubscription.current_period_end > now,
    )
    active_pro = _count(db, *active_pro_filters)
    total_users = int(db.scalar(select(func.count(User.id))) or 0)

    worker = db.get(ServiceHealth, "monitor-worker")
    worker_seen = worker.last_seen_at if worker else None
    if worker_seen and worker_seen.tzinfo is None:
        worker_seen = worker_seen.replace(tzinfo=timezone.utc)
    worker_stale_seconds = int((now - worker_seen).total_seconds()) if worker_seen else None
    worker_status = "unknown"
    if worker:
        worker_status = "stale" if worker_stale_seconds is not None and worker_stale_seconds > 60 else worker.status

    stale_pending_cutoff = now - timedelta(minutes=30)

    return {
        "users": {
            "total_users": total_users,
            "new_users_today": _count(db, User.created_at >= day_start),
            "new_users_this_month": _count(db, User.created_at >= month_start),
            "free_users": max(0, total_users - active_pro),
            "active_pro_users": active_pro,
            "expired_pro_users": _count(db, UserSubscription.status == "expired"),
        },
        "payments": {
            "real_paid_transactions": _count(db, *REAL_REVENUE_FILTERS),
            "revenue_today": _sum(db, *REAL_REVENUE_FILTERS, PaymentTransaction.paid_at >= day_start),
            "revenue_this_month": _sum(db, *REAL_REVENUE_FILTERS, PaymentTransaction.paid_at >= month_start),
            "revenue_all_time": _sum(db, *REAL_REVENUE_FILTERS),
            "pending_transactions": _count(db, PaymentTransaction.status == "PENDING"),
            "stale_pending_transactions": _count(
                db,
                PaymentTransaction.status == "PENDING",
                PaymentTransaction.created_at < stale_pending_cutoff,
            ),
            "failed_transactions": _count(db, PaymentTransaction.status == "FAILED"),
            "expired_transactions": _count(db, PaymentTransaction.status == "EXPIRED"),
            "unknown_revenue_classification": _count(
                db,
                PaymentTransaction.status == "PAID",
                PaymentTransaction.revenue_classification == "UNKNOWN",
            ),
        },
        "subscriptions": {
            "active_pro": active_pro,
            "expiring_within_7_days": _count(
                db, *active_pro_filters, UserSubscription.current_period_end <= now + timedelta(days=7)
            ),
            "expiring_within_3_days": _count(
                db, *active_pro_filters, UserSubscription.current_period_end <= now + timedelta(days=3)
            ),
            "expiring_within_1_day": _count(
                db, *active_pro_filters, UserSubscription.current_period_end <= now + timedelta(days=1)
            ),
        },
        "monitoring": {
            "total_http_monitors": _count(db, Monitor.type == "http"),
            "active_monitors": _count(db, Monitor.is_active.is_(True)),
            "restricted_monitors": _restricted_monitor_count(db, now),
            "total_heartbeats": _count(db, Monitor.type == "heartbeat"),
            "total_status_pages": _count(db, StatusPage.id.is_not(None)),
            "current_open_incidents": _count(db, Incident.status == "open"),
        },
        "infrastructure": {
            "api_health": "healthy",
            "database_connectivity": "connected",
            "worker_status": worker_status,
            "worker_last_seen_at": worker_seen.isoformat() if worker_seen else None,
            "worker_stale_seconds": worker_stale_seconds,
            "latest_successful_backup": None,
        },
    }


@router.get("/payments")
def list_admin_payments(
    provider: str | None = Query(default=None, max_length=32),
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    mode: str | None = Query(default=None, max_length=32),
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    filters: list[Any] = []
    if provider:
        filters.append(PaymentTransaction.provider == provider.strip().lower())
    if status_filter:
        normalized_status = status_filter.strip().upper()
        if normalized_status not in {"PENDING", "PAID", "FAILED", "EXPIRED", "CANCELED"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid payment status")
        filters.append(PaymentTransaction.status == normalized_status)
    if mode:
        filters.append(PaymentTransaction.provider_mode == mode.strip().lower())
    if date_from:
        filters.append(PaymentTransaction.created_at >= datetime.combine(date_from, time.min, timezone.utc))
    if date_to:
        filters.append(PaymentTransaction.created_at < datetime.combine(date_to + timedelta(days=1), time.min, timezone.utc))

    total = int(db.scalar(select(func.count(PaymentTransaction.id)).where(*filters)) or 0)
    rows = db.execute(
        select(PaymentTransaction, User.email)
        .join(User, User.id == PaymentTransaction.user_id)
        .where(*filters)
        .order_by(PaymentTransaction.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()

    return {
        "count": total,
        "items": [
            {
                "id": tx.id,
                "external_id": tx.external_id,
                "user_id": tx.user_id,
                "user_email": email,
                "provider": tx.provider,
                "provider_mode": tx.provider_mode,
                "revenue_classification": tx.revenue_classification,
                "amount": tx.amount,
                "currency": tx.currency,
                "status": tx.status,
                "payment_reference": tx.provider_payment_id,
                "payment_method": tx.payment_method,
                "created_at": tx.created_at.isoformat(),
                "paid_at": tx.paid_at.isoformat() if tx.paid_at else None,
            }
            for tx, email in rows
        ],
    }


@router.get("/users")
def list_admin_users(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    total = int(db.scalar(select(func.count(User.id))) or 0)
    users = db.scalars(select(User).order_by(User.created_at.desc()).offset(offset).limit(limit)).all()
    items = []
    now = datetime.now(timezone.utc)
    for user in users:
        sub = db.scalar(select(UserSubscription).where(UserSubscription.user_id == user.id))
        is_active_pro = bool(
            sub
            and sub.plan == "pro"
            and sub.status == "active"
            and sub.current_period_end
            and (sub.current_period_end.replace(tzinfo=timezone.utc) if sub.current_period_end.tzinfo is None else sub.current_period_end) > now
        )
        items.append(
            {
                "id": user.id,
                "email": user.email,
                "name": user.name,
                "is_admin": user.is_admin,
                "launch_cohort": user.launch_cohort,
                "created_at": user.created_at.isoformat(),
                "plan": "pro" if is_active_pro else "free",
                "subscription_status": sub.status if sub else "active",
                "pro_expires_at": sub.current_period_end.isoformat() if sub and sub.current_period_end else None,
                "monitor_count": _count(db, Monitor.user_id == user.id, Monitor.type == "http"),
                "heartbeat_count": _count(db, Monitor.user_id == user.id, Monitor.type == "heartbeat"),
                "status_page_count": _count(db, StatusPage.user_id == user.id),
            }
        )
    return {"count": total, "items": items}


@router.get("/analytics")
def get_admin_product_analytics(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    return build_admin_product_analytics(db)


@router.get("/soft-launch")
def get_admin_soft_launch(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    return build_soft_launch_summary(db)


@router.get("/feedback")
def list_admin_feedback(
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    filters: list[Any] = []
    if status_filter:
        normalized_status = status_filter.strip().lower()
        if normalized_status not in {"open", "reviewed", "closed"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid feedback status",
            )
        filters.append(UserFeedback.status == normalized_status)

    total = int(db.scalar(select(func.count(UserFeedback.id)).where(*filters)) or 0)
    rows = db.execute(
        select(UserFeedback, User.email)
        .join(User, User.id == UserFeedback.user_id)
        .where(*filters)
        .order_by(UserFeedback.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "count": total,
        "items": [
            {
                "id": feedback.id,
                "user_id": feedback.user_id,
                "user_email": email,
                "category": feedback.category,
                "message": feedback.message,
                "rating": feedback.rating,
                "page_path": feedback.page_path,
                "status": feedback.status,
                "created_at": feedback.created_at.isoformat(),
            }
            for feedback, email in rows
        ],
    }
