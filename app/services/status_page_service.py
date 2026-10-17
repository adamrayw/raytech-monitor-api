import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.incident import Incident
from app.models.monitor import Monitor
from app.models.monitor_check import MonitorCheck
from app.models.status_page import StatusPage, StatusPageMonitor
from app.models.user import User

RESERVED_SLUGS = {
    "admin",
    "api",
    "app",
    "auth",
    "billing",
    "dashboard",
    "docs",
    "health",
    "help",
    "incidents",
    "login",
    "logout",
    "monitors",
    "null",
    "overview",
    "public",
    "root",
    "settings",
    "static",
    "status",
    "status-pages",
    "support",
    "undefined",
}

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validate_slug(slug: str) -> str:
    cleaned = slug.strip().lower()

    if len(cleaned) < 3 or len(cleaned) > 63:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Slug must be between 3 and 63 characters long.",
        )

    if not SLUG_PATTERN.match(cleaned):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Slug must contain only lowercase ASCII letters, numbers, and hyphens (cannot start or end with a hyphen).",
        )

    if cleaned in RESERVED_SLUGS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The slug '{cleaned}' is reserved and cannot be used.",
        )

    return cleaned


def map_monitor_public_status(monitor: Monitor) -> str:
    """Maps internal monitor status to public status representation.

    Public statuses: 'operational', 'degraded', 'down', 'maintenance', 'unknown'
    """
    mon_type = monitor.type or "http"
    mon_status = (monitor.status or "unknown").lower()

    if not monitor.is_active or mon_status == "paused":
        return "maintenance"

    if mon_type == "heartbeat":
        if mon_status == "healthy":
            return "operational"
        elif mon_status == "missed":
            return "down"
        elif mon_status in ("pending", "unknown"):
            return "unknown"
        elif mon_status == "operational":
            return "operational"
        return "unknown"
    else:
        # HTTP Monitor
        if mon_status == "operational":
            return "operational"
        elif mon_status == "degraded":
            return "degraded"
        elif mon_status == "down":
            return "down"
        return "unknown"


def calculate_overall_status(public_statuses: list[str]) -> tuple[str, str]:
    """Calculates overall status for the page and returns (overall_status, headline_message)."""
    if not public_statuses:
        return "unknown", "No Monitored Services"

    if any(s == "down" for s in public_statuses):
        return "down", "Service Disruption Detected"

    if any(s == "degraded" for s in public_statuses):
        return "degraded", "Degraded Performance"

    if all(s == "maintenance" for s in public_statuses):
        return "maintenance", "Scheduled Maintenance in Progress"

    if all(s == "unknown" for s in public_statuses):
        return "unknown", "Service Status Pending"

    # If some are maintenance and the rest operational
    if any(s == "maintenance" for s in public_statuses):
        return "operational", "Systems Operational (Some in Maintenance)"

    return "operational", "All Systems Operational"


def sanitize_incident_cause(cause: str | None) -> str:
    """Converts internal technical error strings to safe public descriptions without exposing IPs/URLs/passwords."""
    if not cause:
        return "Service disruption detected"

    lower = cause.lower()
    if "heartbeat" in lower:
        return "Scheduled service unavailable"
    elif "refused" in lower or "connection error" in lower:
        return "Service temporarily unavailable"
    elif "timeout" in lower or "timed out" in lower:
        return "High latency / request timeout"
    elif "503" in lower or "service unavailable" in lower:
        return "Service temporarily unavailable"
    elif "502" in lower or "bad gateway" in lower:
        return "Service connectivity issue"
    elif "500" in lower or "internal server error" in lower:
        return "Internal service error"
    elif "404" in lower:
        return "Service endpoint unreachable"
    elif "ssl" in lower or "certificate" in lower:
        return "Security certificate validation issue"

    return "Service disruption detected"


def get_30_day_history_for_monitor(
    db: Session,
    monitor: Monitor,
) -> list[dict[str, Any]]:
    """Calculates 30-day daily uptime history for a monitor using real monitor_checks data."""
    now = utcnow()
    days: list[dict[str, Any]] = []

    # If heartbeat monitor: uptime percentage is not based on periodic checks
    if (monitor.type or "http") == "heartbeat":
        for i in range(29, -1, -1):
            day_date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            days.append(
                {
                    "date": day_date,
                    "has_data": False,
                    "uptime_percent": None,
                    "status": "not_applicable",
                    "total_checks": 0,
                    "successful_checks": 0,
                }
            )
        return days

    # HTTP Monitor: query checks in the last 30 days
    start_time = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    checks = db.scalars(
        select(MonitorCheck)
        .where(
            MonitorCheck.monitor_id == monitor.id,
            MonitorCheck.checked_at >= start_time,
        )
        .order_by(MonitorCheck.checked_at.asc())
    ).all()

    # Bucket checks by calendar date (YYYY-MM-DD UTC)
    daily_buckets: dict[str, list[MonitorCheck]] = {}
    for c in checks:
        dt_str = c.checked_at.strftime("%Y-%m-%d")
        daily_buckets.setdefault(dt_str, []).append(c)

    for i in range(29, -1, -1):
        target_day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        day_checks = daily_buckets.get(target_day, [])

        if not day_checks:
            days.append(
                {
                    "date": target_day,
                    "has_data": False,
                    "uptime_percent": None,
                    "status": "no_data",
                    "total_checks": 0,
                    "successful_checks": 0,
                }
            )
        else:
            total = len(day_checks)
            success = sum(1 for c in day_checks if c.success)
            rate = round((success / total) * 100, 2)

            if rate >= 99.0:
                day_status = "operational"
            elif rate >= 95.0:
                day_status = "degraded"
            else:
                day_status = "down"

            days.append(
                {
                    "date": target_day,
                    "has_data": True,
                    "uptime_percent": rate,
                    "status": day_status,
                    "total_checks": total,
                    "successful_checks": success,
                }
            )

    return days


def verify_user_monitors_ownership(
    db: Session,
    user: User,
    monitor_ids: list[int],
) -> list[Monitor]:
    """Ensures that all specified monitor IDs exist and belong strictly to the user."""
    if not monitor_ids:
        return []

    monitors = db.scalars(
        select(Monitor).where(
            Monitor.id.in_(monitor_ids),
            Monitor.user_id == user.id,
        )
    ).all()

    found_ids = {m.id for m in monitors}
    missing_or_unauthorized = set(monitor_ids) - found_ids
    if missing_or_unauthorized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid or unauthorized monitor ID(s): {list(missing_or_unauthorized)}",
        )

    # Return in requested order
    monitor_map = {m.id: m for m in monitors}
    return [monitor_map[mid] for mid in monitor_ids]
