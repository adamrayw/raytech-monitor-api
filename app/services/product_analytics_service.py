from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.monitor import Monitor
from app.models.notification_preference import NotificationPreference
from app.models.payment_transaction import PaymentTransaction
from app.models.product_event import ProductEvent
from app.models.status_page import StatusPage
from app.models.subscription import UserSubscription
from app.models.user import User
from app.models.user_feedback import UserFeedback
from app.services.product_event_service import MEANINGFUL_ACTIVITY_EVENTS


REAL_PAYMENT_FILTERS = (
    PaymentTransaction.status == "PAID",
    PaymentTransaction.provider == "qrisgate",
    PaymentTransaction.provider_mode == "live",
    PaymentTransaction.revenue_classification == "REAL",
)


def ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 2)


def distinct_event_users(db: Session, event_name: str) -> set[int]:
    return set(event_user_times(db, event_name))


def event_user_times(db: Session, event_name: str) -> dict[int, datetime]:
    rows = db.execute(
        select(ProductEvent.user_id, func.min(ProductEvent.created_at))
        .where(
            ProductEvent.event_name == event_name,
            ProductEvent.user_id.is_not(None),
        )
        .group_by(ProductEvent.user_id)
    ).all()
    return {
        int(user_id): ensure_utc(created_at)
        for user_id, created_at in rows
        if user_id is not None
    }


def distinct_event_users_with_property(
    db: Session,
    event_name: str,
    property_name: str,
    property_value: str,
) -> set[int]:
    return {
        int(user_id)
        for user_id in db.scalars(
            select(ProductEvent.user_id)
            .where(
                ProductEvent.event_name == event_name,
                ProductEvent.user_id.is_not(None),
                ProductEvent.properties[property_name].as_string() == property_value,
            )
            .distinct()
        ).all()
        if user_id is not None
    }


def active_pro_user_ids(db: Session, now: datetime) -> set[int]:
    return set(
        db.scalars(
            select(UserSubscription.user_id).where(
                UserSubscription.plan == "pro",
                UserSubscription.status == "active",
                UserSubscription.current_period_end.is_not(None),
                UserSubscription.current_period_end > now,
            )
        ).all()
    )


def retention_result(
    signup_dates: dict[int, date],
    activity_dates: dict[int, set[date]],
    target_day: int,
    today: date,
) -> dict[str, Any]:
    eligible = {
        user_id: signup_date
        for user_id, signup_date in signup_dates.items()
        if signup_date <= today - timedelta(days=target_day)
    }
    if not eligible:
        return {
            "cohort_size": 0,
            "retained_users": 0,
            "retention_percent": None,
            "status": "insufficient_data",
        }

    retained = sum(
        1
        for user_id, signup_date in eligible.items()
        if any(
            activity_date >= signup_date + timedelta(days=target_day)
            for activity_date in activity_dates.get(user_id, set())
        )
    )
    return {
        "cohort_size": len(eligible),
        "retained_users": retained,
        "retention_percent": ratio(retained, len(eligible)),
        "status": "available",
    }


def build_retention(db: Session, now: datetime) -> dict[str, Any]:
    signup_rows = db.execute(
        select(ProductEvent.user_id, ProductEvent.created_at).where(
            ProductEvent.event_name == "user_signed_up",
            ProductEvent.user_id.is_not(None),
        )
    ).all()
    signup_dates = {
        int(user_id): ensure_utc(created_at).date()
        for user_id, created_at in signup_rows
        if user_id is not None
    }

    activity_dates: dict[int, set[date]] = defaultdict(set)
    activity_rows = db.execute(
        select(ProductEvent.user_id, ProductEvent.created_at).where(
            ProductEvent.event_name.in_(MEANINGFUL_ACTIVITY_EVENTS),
            ProductEvent.user_id.is_not(None),
        )
    ).all()
    for user_id, created_at in activity_rows:
        if user_id is not None:
            activity_dates[int(user_id)].add(ensure_utc(created_at).date())

    cohorts: list[dict[str, Any]] = []
    grouped: dict[date, list[int]] = defaultdict(list)
    for user_id, signup_date in signup_dates.items():
        grouped[signup_date].append(user_id)

    for cohort_date in sorted(grouped, reverse=True)[:30]:
        user_ids = grouped[cohort_date]
        row: dict[str, Any] = {
            "cohort_date": cohort_date.isoformat(),
            "users": len(user_ids),
        }
        for day in (1, 7, 30):
            if cohort_date > now.date() - timedelta(days=day):
                row[f"d{day}"] = None
                continue
            retained = sum(
                1
                for user_id in user_ids
                if any(
                    activity_date >= cohort_date + timedelta(days=day)
                    for activity_date in activity_dates.get(user_id, set())
                )
            )
            row[f"d{day}"] = {
                "retained_users": retained,
                "retention_percent": ratio(retained, len(user_ids)),
            }
        cohorts.append(row)

    return {
        "d1": retention_result(signup_dates, activity_dates, 1, now.date()),
        "d7": retention_result(signup_dates, activity_dates, 7, now.date()),
        "d30": retention_result(signup_dates, activity_dates, 30, now.date()),
        "cohorts": cohorts,
    }


def build_admin_product_analytics(db: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    total_users = int(db.scalar(select(func.count(User.id))) or 0)
    signup_users = distinct_event_users(db, "user_signed_up")
    activated_users = set(
        db.scalars(select(Monitor.user_id).where(Monitor.type == "http").distinct()).all()
    )
    pricing_users = distinct_event_users_with_property(
        db, "pricing_viewed", "plan", "free"
    )

    checkout_users = distinct_event_users(db, "checkout_started")
    payment_users = set(
        db.scalars(
            select(PaymentTransaction.user_id)
            .where(*REAL_PAYMENT_FILTERS)
            .distinct()
        ).all()
    )
    verified_payment_users = distinct_event_users(db, "payment_succeeded") & payment_users
    pricing_checkout_users = checkout_users & pricing_users
    verified_checkout_payment_users = verified_payment_users & checkout_users

    active_pro = active_pro_user_ids(db, now)

    activation_seconds = [
        int(value)
        for value in db.scalars(
            select(ProductEvent.properties["time_to_activation_seconds"].as_integer()).where(
                ProductEvent.event_name == "first_monitor_created"
            )
        ).all()
        if isinstance(value, (int, float))
    ]

    funnel_event_names = (
        ("Signup", "user_signed_up"),
        ("First monitor", "first_monitor_created"),
        ("Pricing viewed", "pricing_viewed"),
        ("Upgrade clicked", "upgrade_clicked"),
        ("Checkout started", "checkout_started"),
        ("Payment succeeded", "payment_succeeded"),
        ("Pro activated", "pro_activated"),
    )
    funnel_times = {
        event_name: event_user_times(db, event_name)
        for _, event_name in funnel_event_names
    }
    funnel_times["pricing_viewed"] = {
        user_id: event_time
        for user_id, event_time in funnel_times["pricing_viewed"].items()
        if user_id in pricing_users
    }
    eligible_funnel_users: dict[int, datetime] = funnel_times["user_signed_up"]
    funnel_counts: list[tuple[str, int]] = []
    for stage, event_name in funnel_event_names:
        current_times = funnel_times[event_name]
        if event_name == "user_signed_up":
            eligible_funnel_users = current_times
        else:
            eligible_funnel_users = {
                user_id: current_times[user_id]
                for user_id, previous_time in eligible_funnel_users.items()
                if user_id in current_times and current_times[user_id] >= previous_time
            }
        if event_name in {"payment_succeeded", "pro_activated"}:
            eligible_funnel_users = {
                user_id: event_time
                for user_id, event_time in eligible_funnel_users.items()
                if user_id in payment_users
            }
        funnel_counts.append((stage, len(eligible_funnel_users)))
    funnel: list[dict[str, Any]] = []
    previous_count: int | None = None
    for stage, count in funnel_counts:
        conversion = None if previous_count is None else ratio(count, previous_count)
        dropoff = None if conversion is None else round(max(0.0, 100.0 - conversion), 2)
        funnel.append(
            {
                "stage": stage,
                "count": count,
                "previous_stage_count": previous_count,
                "conversion_percent": conversion,
                "dropoff_percent": dropoff,
            }
        )
        previous_count = count

    total_http = int(db.scalar(select(func.count(Monitor.id)).where(Monitor.type == "http")) or 0)
    users_with_http = set(
        db.scalars(select(Monitor.user_id).where(Monitor.type == "http").distinct()).all()
    )
    users_with_heartbeat = set(
        db.scalars(select(Monitor.user_id).where(Monitor.type == "heartbeat").distinct()).all()
    )
    users_with_status = set(db.scalars(select(StatusPage.user_id).distinct()).all())
    pro_with_ssl = set(
        db.scalars(
            select(Monitor.user_id).where(
                Monitor.type == "http",
                Monitor.ssl_monitoring_enabled.is_(True),
                Monitor.user_id.in_(active_pro or {-1}),
            ).distinct()
        ).all()
    )
    pro_with_alerts = set(
        db.scalars(
            select(NotificationPreference.user_id).where(
                NotificationPreference.email_enabled.is_(True),
                NotificationPreference.user_id.in_(active_pro or {-1}),
            ).distinct()
        ).all()
    )

    last_30_days = now - timedelta(days=30)
    recently_active = set(
        db.scalars(
            select(ProductEvent.user_id).where(
                ProductEvent.event_name.in_(MEANINGFUL_ACTIVITY_EVENTS),
                ProductEvent.created_at >= last_30_days,
                ProductEvent.user_id.is_not(None),
            ).distinct()
        ).all()
    )

    retention = build_retention(db, now)
    first_event_at = db.scalar(select(func.min(ProductEvent.created_at)))

    return {
        "tracking_started_at": first_event_at.isoformat() if first_event_at else None,
        "overview": {
            "total_users": total_users,
            "signups_tracked": len(signup_users),
            "activated_users": len(activated_users),
            "activation_rate": {
                "numerator": len(activated_users),
                "denominator": total_users,
                "percent": ratio(len(activated_users), total_users),
            },
            "average_time_to_activation_seconds": (
                round(sum(activation_seconds) / len(activation_seconds), 2)
                if activation_seconds else None
            ),
            "median_time_to_activation_seconds": (
                round(float(median(activation_seconds)), 2) if activation_seconds else None
            ),
            "active_pro_users": len(active_pro),
            "free_to_pro_conversion": {
                "numerator": len(payment_users),
                "denominator": total_users,
                "percent": ratio(len(payment_users), total_users),
            },
            "pricing_to_checkout": {
                "numerator": len(pricing_checkout_users),
                "denominator": len(pricing_users),
                "percent": ratio(len(pricing_checkout_users), len(pricing_users)),
            },
            "checkout_to_payment": {
                "numerator": len(verified_checkout_payment_users),
                "denominator": len(checkout_users),
                "percent": ratio(len(verified_checkout_payment_users), len(checkout_users)),
            },
            "renewing_users": len(distinct_event_users(db, "pro_renewed")),
            "d1_retention": retention["d1"],
            "d7_retention": retention["d7"],
        },
        "funnel": funnel,
        "retention": retention,
        "feature_adoption": {
            "active_users_30d": len(recently_active),
            "average_http_monitors_per_active_user": (
                round(total_http / len(recently_active), 2) if recently_active else None
            ),
            "users_with_http_monitor": {
                "numerator": len(users_with_http),
                "denominator": total_users,
                "percent": ratio(len(users_with_http), total_users),
            },
            "users_with_heartbeat": {
                "numerator": len(users_with_heartbeat),
                "denominator": total_users,
                "percent": ratio(len(users_with_heartbeat), total_users),
            },
            "users_with_status_page": {
                "numerator": len(users_with_status),
                "denominator": total_users,
                "percent": ratio(len(users_with_status), total_users),
            },
            "pro_users_with_ssl": {
                "numerator": len(pro_with_ssl),
                "denominator": len(active_pro),
                "percent": ratio(len(pro_with_ssl), len(active_pro)),
            },
            "pro_users_with_email_alerts": {
                "numerator": len(pro_with_alerts),
                "denominator": len(active_pro),
                "percent": ratio(len(pro_with_alerts), len(active_pro)),
            },
            "billing_users": len(distinct_event_users(db, "billing_viewed")),
            "incident_viewers": len(distinct_event_users(db, "incident_viewed")),
        },
    }


def build_soft_launch_summary(db: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    beta_users = set(
        db.scalars(select(User.id).where(User.launch_cohort == "beta")).all()
    )
    beta_activated = set(
        db.scalars(
            select(Monitor.user_id).where(
                Monitor.type == "http",
                Monitor.user_id.in_(beta_users or {-1}),
            ).distinct()
        ).all()
    )
    paying_beta = set(
        db.scalars(
            select(PaymentTransaction.user_id).where(
                *REAL_PAYMENT_FILTERS,
                PaymentTransaction.user_id.in_(beta_users or {-1}),
            ).distinct()
        ).all()
    )
    feedback_count = int(db.scalar(select(func.count(UserFeedback.id))) or 0)
    open_feedback = int(
        db.scalar(select(func.count(UserFeedback.id)).where(UserFeedback.status == "open")) or 0
    )
    payment_failures = int(
        db.scalar(
            select(func.count(PaymentTransaction.id)).where(
                PaymentTransaction.provider == "qrisgate",
                PaymentTransaction.provider_mode == "live",
                PaymentTransaction.status == "FAILED",
                PaymentTransaction.created_at >= now - timedelta(hours=24),
            )
        ) or 0
    )
    return {
        "beta_users": len(beta_users),
        "activated_beta_users": len(beta_activated),
        "paying_beta_users": len(paying_beta),
        "beta_conversion": {
            "numerator": len(paying_beta),
            "denominator": len(beta_users),
            "percent": ratio(len(paying_beta), len(beta_users)),
        },
        "feedback_count": feedback_count,
        "open_feedback_items": open_feedback,
        "critical_errors_last_24h": None,
        "critical_errors_status": "unavailable",
        "payment_failures_last_24h": payment_failures,
    }
