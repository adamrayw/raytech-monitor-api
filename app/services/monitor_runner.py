import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.monitor import Monitor
from app.models.monitor_check import MonitorCheck
from app.services.http_checker import CheckResult, check_http
from app.services.incident_service import handle_incident_transition
from app.services.notification_service import create_incident_notification

MAX_CONCURRENCY = 20


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_due_monitor_ids(db: Session) -> list[int]:
    now = utcnow()

    monitor_ids = list(
        db.scalars(
            select(Monitor.id)
            .where(
                Monitor.is_active.is_(True),
                Monitor.type == "http",
                or_(
                    Monitor.next_check_at.is_(None),
                    Monitor.next_check_at <= now,
                ),
            )
            .order_by(Monitor.next_check_at.asc().nullsfirst())
        ).all()
    )

    return monitor_ids


def apply_check_result(
    db: Session,
    monitor: Monitor,
    result: CheckResult,
    checked_at: datetime,
) -> str:
    """
    Apply one HTTP result to monitor state.

    State policy v1:

    unknown + first success
        -> operational / degraded immediately

    failure
        -> down only after 2 consecutive failures

    down + recovery
        -> requires 2 consecutive successful HTTP checks

    degraded
        -> HTTP succeeded but response exceeded latency threshold
    """

    previous_status = monitor.status

    monitor.last_checked_at = checked_at
    monitor.last_http_status = result.http_status
    monitor.last_response_ms = result.response_ms

    monitor.next_check_at = checked_at + timedelta(
        seconds=monitor.interval_seconds
    )

    if result.success:
        monitor.last_success_at = checked_at

        monitor.consecutive_successes += 1
        monitor.consecutive_failures = 0

        observed_status = "operational"

        if (
            result.response_ms is not None
            and result.response_ms > monitor.degraded_threshold_ms
        ):
            observed_status = "degraded"

        if previous_status == "down":
            # Require two successful checks before recovery.
            if monitor.consecutive_successes >= 2:
                monitor.status = observed_status
            else:
                monitor.status = "down"

        else:
            monitor.status = observed_status

    else:
        monitor.last_failure_at = checked_at

        monitor.consecutive_failures += 1
        monitor.consecutive_successes = 0

        if monitor.consecutive_failures >= 2:
            monitor.status = "down"

        elif previous_status == "unknown":
            monitor.status = "unknown"

        # If currently operational/degraded and this is only
        # the first failure, retain current state until confirmed.

    check = MonitorCheck(
        monitor_id=monitor.id,
        checked_at=checked_at,
        success=result.success,
        http_status=result.http_status,
        response_ms=result.response_ms,
        error_type=result.error_type,
        error_message=result.error_message,
    )

    db.add(check)

    return previous_status


async def check_one_monitor(
    monitor_id: int,
    semaphore: asyncio.Semaphore,
) -> dict:
    async with semaphore:
        # Load configuration using a short-lived DB session.
        db = SessionLocal()

        try:
            monitor = db.get(Monitor, monitor_id)

            if monitor is None:
                return {
                    "monitor_id": monitor_id,
                    "result": "not_found",
                }

            if not monitor.is_active or monitor.type != "http":
                return {
                    "monitor_id": monitor_id,
                    "result": "skipped",
                }

            config = {
                "url": monitor.url,
                "timeout_seconds": monitor.timeout_seconds,
                "expected_status_min": monitor.expected_status_min,
                "expected_status_max": monitor.expected_status_max,
            }

        finally:
            db.close()

        # Do not keep a database transaction open
        # while waiting for the external HTTP request.
        result = await check_http(**config)

        checked_at = utcnow()

        db = SessionLocal()

        try:
            monitor = db.get(Monitor, monitor_id)

            if monitor is None:
                return {
                    "monitor_id": monitor_id,
                    "result": "not_found",
                }

            previous_status = apply_check_result(
            db=db,
                monitor=monitor,
                result=result,
                checked_at=checked_at,
            )

            incident_transition = handle_incident_transition(
                db=db,
            	monitor=monitor,
            	result=result,
            	previous_status=previous_status,
            	checked_at=checked_at,
            )

            notification = create_incident_notification(
            	db=db,
                monitor=monitor,
            	incident_id=incident_transition["incident_id"],
            	event=incident_transition["event"],
            )

            db.commit()
            db.refresh(monitor)

            return {
                "monitor_id": monitor.id,
                "name": monitor.name,
                "url": monitor.url,
                "success": result.success,
                "http_status": result.http_status,
                "response_ms": result.response_ms,
                "error_type": result.error_type,
                "previous_status": previous_status,
                "status": monitor.status,
                "consecutive_failures": monitor.consecutive_failures,
                "consecutive_successes": monitor.consecutive_successes,
                "incident_event": incident_transition["event"],
            "incident_id": incident_transition["incident_id"],
                "next_check_at": monitor.next_check_at,
        "notification_id": (
                   notification.id
                   if notification is not None
                   else None
        ),
        "next_check_at": monitor.next_check_at,
            }

        except Exception:
            db.rollback()
            raise

        finally:
            db.close()


async def run_due_monitors() -> dict:
    db = SessionLocal()

    try:
        monitor_ids = get_due_monitor_ids(db)
    finally:
        db.close()

    if not monitor_ids:
        return {
            "due": 0,
            "checked": 0,
            "success": 0,
            "failed": 0,
            "results": [],
        }

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    results = await asyncio.gather(
        *[
            check_one_monitor(
                monitor_id=monitor_id,
                semaphore=semaphore,
            )
            for monitor_id in monitor_ids
        ],
        return_exceptions=True,
    )

    normalized_results = []

    successful_checks = 0
    failed_checks = 0

    for monitor_id, result in zip(monitor_ids, results):
        if isinstance(result, Exception):
            failed_checks += 1

            normalized_results.append(
                {
                    "monitor_id": monitor_id,
                    "result": "worker_error",
                    "error": str(result)[:500],
                }
            )

            continue

        normalized_results.append(result)

        if result.get("success") is True:
            successful_checks += 1

        elif result.get("success") is False:
            failed_checks += 1

    return {
        "due": len(monitor_ids),
        "checked": len(normalized_results),
        "success": successful_checks,
        "failed": failed_checks,
        "results": normalized_results,
    }
