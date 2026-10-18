from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models.monitor import Monitor
from app.models.monitor_check import MonitorCheck
from app.models.ssl_check import SSLCheck
from app.models.heartbeat_event import HeartbeatEvent
from app.models.incident import Incident
from app.models.user import User
from app.services.heartbeat_service import generate_heartbeat_token
from app.services.subscription_service import (
    enforce_http_interval,
    enforce_monitor_creation_quota,
    enforce_ssl_access,
)
from app.schemas.monitor import (
    HeartbeatEventResponse,
    HeartbeatEventsListResponse,
    HeartbeatRegenerateResponse,
    MonitorCheckListResponse,
    MonitorCreate,
    MonitorListResponse,
    MonitorMetricPoint,
    MonitorMetricsResponse,
    MonitorResponse,
    MonitorUpdate,
    SSLCheckHistoryResponse,
    SSLDetailResponse,
)


router = APIRouter(
    prefix="/monitors",
    tags=["monitors"],
)


def utcnow():
    return datetime.now(timezone.utc)


def calculate_p95(values: list[float]) -> float | None:
    if not values:
        return None
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * 0.95
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    d = k - f
    return round(sorted_vals[f] + d * (sorted_vals[c] - sorted_vals[f]), 2)


def get_owned_monitor(
    monitor_id: int,
    user: User,
    db: Session,
) -> Monitor:
    monitor = db.scalar(
        select(Monitor).where(
            Monitor.id == monitor_id,
            Monitor.user_id == user.id,
        )
    )

    if monitor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Monitor not found",
        )

    return monitor


@router.get("", response_model=MonitorListResponse)
def list_monitors(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    monitors = list(
        db.scalars(
            select(Monitor)
            .where(Monitor.user_id == user.id)
            .order_by(Monitor.created_at.desc())
        ).all()
    )

    from app.services.subscription_service import get_eligible_resource_ids
    eligible_http = get_eligible_resource_ids(db, user.id, "http")
    eligible_hb = get_eligible_resource_ids(db, user.id, "heartbeat")

    items = []
    for m in monitors:
        res = MonitorResponse.model_validate(m)
        if m.type == "http":
            res.is_restricted_by_plan = m.id not in eligible_http
        elif m.type == "heartbeat":
            res.is_restricted_by_plan = m.id not in eligible_hb
        items.append(res)

    return {
        "items": items,
        "count": len(items),
    }


from app.services.rate_limiter import api_rate_limiter
from app.services.http_checker import UnsafeTargetError, validate_target


@router.post(
    "",
    response_model=MonitorResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_monitor(
    payload: MonitorCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    api_rate_limiter.check_or_raise(
        f"monitor_write:{user.id}",
        max_requests=30,
        window_seconds=60.0,
        error_detail="Too many monitor modifications. Please wait a moment before trying again.",
    )

    if payload.type == "heartbeat":
        enforce_monitor_creation_quota(db, user.id, "heartbeat")
        raw_token, token_hash = generate_heartbeat_token()
        monitor = Monitor(
            user_id=user.id,
            type="heartbeat",
            name=payload.name.strip(),
            url=None,
            interval_seconds=payload.interval_seconds,
            timeout_seconds=payload.timeout_seconds,
            status="pending",
            is_active=True,
            next_check_at=None,
            expected_interval_seconds=payload.expected_interval_seconds or 3600,
            grace_period_seconds=payload.grace_period_seconds or 600,
            heartbeat_token_hash=token_hash,
            ssl_monitoring_enabled=False,
            ssl_status="paused",
            ssl_next_check_at=None,
        )
        db.add(monitor)
        db.commit()
        db.refresh(monitor)

        resp = MonitorResponse.model_validate(monitor)
        resp.heartbeat_token = raw_token
        return resp

    # HTTP monitor creation
    enforce_monitor_creation_quota(db, user.id, "http")
    enforce_http_interval(db, user.id, payload.interval_seconds)

    url_str = str(payload.url)
    try:
        await validate_target(url_str)
    except UnsafeTargetError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid target URL: {exc}",
        )

    ssl_enabled = payload.ssl_monitoring_enabled
    if ssl_enabled:
        enforce_ssl_access(db, user.id)
        if not url_str.lower().startswith("https://"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="SSL monitoring can only be enabled for HTTPS URLs",
            )

    monitor = Monitor(
        user_id=user.id,
        type="http",
        name=payload.name.strip(),
        url=url_str,
        interval_seconds=payload.interval_seconds,
        timeout_seconds=payload.timeout_seconds,
        expected_status_min=payload.expected_status_min,
        expected_status_max=payload.expected_status_max,
        degraded_threshold_ms=payload.degraded_threshold_ms,
        status="unknown",
        is_active=True,
        next_check_at=utcnow(),
        ssl_monitoring_enabled=ssl_enabled,
        ssl_warning_days=payload.ssl_warning_days,
        ssl_status="unknown" if ssl_enabled else "paused",
        ssl_next_check_at=utcnow() if ssl_enabled else None,
    )

    db.add(monitor)
    db.commit()
    db.refresh(monitor)

    return monitor


@router.get("/{monitor_id}", response_model=MonitorResponse)
def get_monitor(
    monitor_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return get_owned_monitor(monitor_id, user, db)


@router.patch("/{monitor_id}", response_model=MonitorResponse)
async def update_monitor(
    monitor_id: int,
    payload: MonitorUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    api_rate_limiter.check_or_raise(
        f"monitor_write:{user.id}",
        max_requests=30,
        window_seconds=60.0,
        error_detail="Too many monitor modifications. Please wait a moment before trying again.",
    )

    monitor = get_owned_monitor(monitor_id, user, db)

    updates = payload.model_dump(exclude_unset=True)

    current_min = updates.get(
        "expected_status_min",
        monitor.expected_status_min,
    )
    current_max = updates.get(
        "expected_status_max",
        monitor.expected_status_max,
    )

    if current_min > current_max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="expected_status_min must be less than or equal to expected_status_max",
        )

    # Entitlement checks on update
    if "interval_seconds" in updates and updates["interval_seconds"] is not None:
        if monitor.type == "http":
            enforce_http_interval(db, user.id, updates["interval_seconds"])

    if "ssl_monitoring_enabled" in updates and updates["ssl_monitoring_enabled"] is True:
        enforce_ssl_access(db, user.id, existing_monitor=monitor)

    new_url = updates.get("url")
    if new_url is not None:
        try:
            await validate_target(str(new_url))
        except UnsafeTargetError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid target URL: {exc}",
            )

    effective_url = str(new_url) if new_url is not None else monitor.url
    effective_ssl = updates.get("ssl_monitoring_enabled", monitor.ssl_monitoring_enabled)

    if effective_ssl and not effective_url.lower().startswith("https://"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="SSL monitoring can only be enabled for HTTPS URLs",
        )

    state_affecting_fields = {
        "url",
        "expected_status_min",
        "expected_status_max",
        "timeout_seconds",
        "degraded_threshold_ms",
    }

    reset_state = bool(state_affecting_fields.intersection(updates))

    if "name" in updates:
        updates["name"] = updates["name"].strip()

    if "url" in updates:
        updates["url"] = str(updates["url"])

    previous_active = monitor.is_active
    previous_url = monitor.url
    previous_ssl_enabled = monitor.ssl_monitoring_enabled

    for key, value in updates.items():
        setattr(monitor, key, value)

    # Invalidate stale SSL metadata if hostname changes
    if "url" in updates and str(updates["url"]) != previous_url:
        old_host = urlparse(previous_url).hostname
        new_host = urlparse(str(updates["url"])).hostname
        if old_host != new_host and monitor.ssl_monitoring_enabled:
            monitor.ssl_status = "unknown"
            monitor.ssl_expires_at = None
            monitor.ssl_issuer = None
            monitor.ssl_subject = None
            monitor.ssl_serial_number = None
            monitor.ssl_fingerprint_sha256 = None
            monitor.ssl_valid_from = None
            monitor.ssl_error_type = None
            monitor.ssl_error_message = None
            monitor.ssl_next_check_at = utcnow()

    # Handle SSL toggle
    if "ssl_monitoring_enabled" in updates:
        if monitor.ssl_monitoring_enabled and not previous_ssl_enabled:
            monitor.ssl_status = "unknown"
            monitor.ssl_next_check_at = utcnow()
        elif not monitor.ssl_monitoring_enabled and previous_ssl_enabled:
            monitor.ssl_status = "paused"
            monitor.ssl_next_check_at = None

    if "is_active" in updates:
        if monitor.type == "heartbeat":
            if monitor.is_active and not previous_active:
                # Resumed: reset to pending until fresh heartbeat arrives
                # Do NOT assume healthy before fresh heartbeat proves recovery
                monitor.status = "pending"
                monitor.next_expected_at = None
                monitor.deadline_at = None
                monitor.consecutive_failures = 0
                monitor.consecutive_successes = 0
                db.add(HeartbeatEvent(monitor_id=monitor.id, received_at=utcnow(), event_type="resumed"))
            elif not monitor.is_active:
                monitor.status = "paused"
                db.add(HeartbeatEvent(monitor_id=monitor.id, received_at=utcnow(), event_type="paused"))
        else:
            if monitor.is_active and not previous_active:
                monitor.status = "unknown"
                monitor.consecutive_failures = 0
                monitor.consecutive_successes = 0
                monitor.next_check_at = utcnow()
                if monitor.ssl_monitoring_enabled:
                    monitor.ssl_status = "unknown"
                    monitor.ssl_next_check_at = utcnow()
            elif not monitor.is_active:
                monitor.status = "paused"
                monitor.next_check_at = None
                if monitor.ssl_monitoring_enabled:
                    monitor.ssl_status = "paused"
                    monitor.ssl_next_check_at = None

    if monitor.type == "heartbeat":
        if ("expected_interval_seconds" in updates or "grace_period_seconds" in updates) and monitor.last_heartbeat_at:
            last_hb = monitor.last_heartbeat_at
            if last_hb.tzinfo is None:
                last_hb = last_hb.replace(tzinfo=timezone.utc)
            interval = monitor.expected_interval_seconds or 3600
            grace = monitor.grace_period_seconds or 600
            monitor.next_expected_at = last_hb + timedelta(seconds=interval)
            monitor.deadline_at = monitor.next_expected_at + timedelta(seconds=grace)

    if monitor.is_active and reset_state:
        # Administrative closure for active incident when target configuration changes
        # Does NOT emit false recovery notification/email
        open_inc = db.scalar(
            select(Incident).where(
                Incident.monitor_id == monitor.id,
                Incident.status == "open",
            )
        )
        if open_inc:
            now = utcnow()
            open_inc.status = "resolved"
            open_inc.resolved_at = now
            open_inc.resolution_reason = "configuration_changed"
            started_at = open_inc.started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            open_inc.duration_seconds = max(0, int((now - started_at).total_seconds()))

        monitor.status = "unknown"
        monitor.consecutive_failures = 0
        monitor.consecutive_successes = 0
        monitor.next_check_at = utcnow()

    db.commit()
    db.refresh(monitor)

    return monitor


@router.delete(
    "/{monitor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_monitor(
    monitor_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    monitor = get_owned_monitor(monitor_id, user, db)

    db.delete(monitor)
    db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{monitor_id}/ssl", response_model=SSLDetailResponse)
def get_monitor_ssl(
    monitor_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    monitor = get_owned_monitor(monitor_id, user, db)
    parsed = urlparse(monitor.url)
    hostname = parsed.hostname or ""
    port = parsed.port or 443

    now = utcnow()
    days_remaining = None
    if monitor.ssl_expires_at:
        exp = monitor.ssl_expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        delta = (exp - now).total_seconds()
        days_remaining = int(delta // 86400)

    return SSLDetailResponse(
        monitor_id=monitor.id,
        enabled=monitor.ssl_monitoring_enabled,
        status=monitor.ssl_status,
        hostname=hostname,
        port=port,
        issuer=monitor.ssl_issuer,
        subject=monitor.ssl_subject,
        valid_from=monitor.ssl_valid_from,
        expires_at=monitor.ssl_expires_at,
        days_remaining=days_remaining,
        last_checked_at=monitor.ssl_last_checked_at,
        last_successful_check_at=monitor.ssl_last_successful_check_at,
        error_type=monitor.ssl_error_type,
        error_message=monitor.ssl_error_message,
        fingerprint_sha256=monitor.ssl_fingerprint_sha256,
        serial_number=monitor.ssl_serial_number,
    )


@router.get("/{monitor_id}/ssl/history", response_model=SSLCheckHistoryResponse)
def get_monitor_ssl_history(
    monitor_id: int,
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    get_owned_monitor(monitor_id, user, db)

    checks = list(
        db.scalars(
            select(SSLCheck)
            .where(SSLCheck.monitor_id == monitor_id)
            .order_by(SSLCheck.checked_at.desc())
            .limit(limit)
        ).all()
    )

    return {
        "items": checks,
        "count": len(checks),
    }


@router.get("/{monitor_id}/checks", response_model=MonitorCheckListResponse)
def get_monitor_checks(
    monitor_id: int,
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    get_owned_monitor(monitor_id, user, db)

    checks = list(
        db.scalars(
            select(MonitorCheck)
            .where(MonitorCheck.monitor_id == monitor_id)
            .order_by(MonitorCheck.checked_at.desc())
            .limit(limit)
        ).all()
    )

    return {
        "items": checks,
        "count": len(checks),
    }


@router.get("/{monitor_id}/metrics", response_model=MonitorMetricsResponse)
def get_monitor_metrics(
    monitor_id: int,
    metric_range: str = Query(default="24h", alias="range"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if metric_range not in ("24h", "7d"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Supported range values are '24h' and '7d'",
        )

    get_owned_monitor(monitor_id, user, db)

    to_time = utcnow()
    if metric_range == "24h":
        from_time = to_time - timedelta(hours=24)
        bucket_seconds = 15 * 60  # 15 minutes
    else:
        from_time = to_time - timedelta(days=7)
        bucket_seconds = 2 * 3600  # 2 hours

    checks = list(
        db.scalars(
            select(MonitorCheck)
            .where(
                MonitorCheck.monitor_id == monitor_id,
                MonitorCheck.checked_at >= from_time,
                MonitorCheck.checked_at <= to_time,
            )
            .order_by(MonitorCheck.checked_at.asc())
        ).all()
    )

    total_checks = len(checks)
    successful_checks = sum(1 for c in checks if c.success)
    failed_checks = sum(1 for c in checks if not c.success)

    uptime_percent: float | None = (
        round((successful_checks / total_checks) * 100, 2)
        if total_checks > 0
        else None
    )

    valid_response_times = [
        c.response_ms for c in checks if c.response_ms is not None
    ]

    if valid_response_times:
        avg_response_ms = round(
            sum(valid_response_times) / len(valid_response_times), 2
        )
        min_response_ms = round(min(valid_response_times), 2)
        max_response_ms = round(max(valid_response_times), 2)
        p95_response_ms = calculate_p95(valid_response_times)
    else:
        avg_response_ms = None
        min_response_ms = None
        max_response_ms = None
        p95_response_ms = None

    buckets: dict[int, list[MonitorCheck]] = {}
    for c in checks:
        b_idx = int((c.checked_at - from_time).total_seconds() // bucket_seconds)
        buckets.setdefault(b_idx, []).append(c)

    num_buckets = int((to_time - from_time).total_seconds() // bucket_seconds) + 1
    points: list[MonitorMetricPoint] = []

    for i in range(num_buckets):
        bucket_time = from_time + timedelta(seconds=i * bucket_seconds)
        b_checks = buckets.get(i, [])
        if b_checks:
            b_ms = [c.response_ms for c in b_checks if c.response_ms is not None]
            b_avg = round(sum(b_ms) / len(b_ms), 2) if b_ms else None
            b_success = sum(1 for c in b_checks if c.success)
            b_rate = round((b_success / len(b_checks)) * 100, 2)
            points.append(
                MonitorMetricPoint(
                    timestamp=bucket_time,
                    avg_response_ms=b_avg,
                    success_rate_percent=b_rate,
                    checks=len(b_checks),
                )
            )

    return {
        "monitor_id": monitor_id,
        "range": metric_range,
        "from": from_time,
        "to": to_time,
        "total_checks": total_checks,
        "successful_checks": successful_checks,
        "failed_checks": failed_checks,
        "uptime_percent": uptime_percent,
        "avg_response_ms": avg_response_ms,
        "p95_response_ms": p95_response_ms,
        "min_response_ms": min_response_ms,
        "max_response_ms": max_response_ms,
        "points": points,
    }


@router.post(
    "/{monitor_id}/heartbeat/regenerate-token",
    response_model=HeartbeatRegenerateResponse,
)
def regenerate_heartbeat_token(
    monitor_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    monitor = get_owned_monitor(monitor_id, user, db)
    if monitor.type != "heartbeat":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Monitor is not a heartbeat monitor",
        )

    raw_token, token_hash = generate_heartbeat_token()
    monitor.heartbeat_token_hash = token_hash
    db.add(
        HeartbeatEvent(
            monitor_id=monitor.id,
            received_at=utcnow(),
            event_type="token_regenerated",
        )
    )
    db.commit()

    return HeartbeatRegenerateResponse(
        heartbeat_token=raw_token,
        message="Heartbeat token regenerated successfully. Existing scripts must be updated.",
    )


@router.get(
    "/{monitor_id}/heartbeat/events",
    response_model=HeartbeatEventsListResponse,
)
def get_heartbeat_events(
    monitor_id: int,
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    monitor = get_owned_monitor(monitor_id, user, db)
    if monitor.type != "heartbeat":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Monitor is not a heartbeat monitor",
        )

    events = list(
        db.scalars(
            select(HeartbeatEvent)
            .where(HeartbeatEvent.monitor_id == monitor.id)
            .order_by(HeartbeatEvent.received_at.desc())
            .limit(limit)
        ).all()
    )

    return HeartbeatEventsListResponse(
        items=events,
        count=len(events),
    )
