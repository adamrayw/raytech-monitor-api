import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.monitor import Monitor
from app.models.notification import Notification
from app.models.ssl_check import SSLCheck
from app.models.ssl_notification import SSLNotificationLog
from app.services.ssl_checker import SSLCheckResult, check_ssl

MAX_SSL_CONCURRENCY = 10
CHECK_INTERVAL_HOURS = 12
RETRY_INTERVAL_MINUTES = 30


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_due_ssl_monitor_ids(db: Session) -> list[int]:
    now = utcnow()

    monitor_ids = list(
        db.scalars(
            select(Monitor.id)
            .where(
                Monitor.is_active.is_(True),
                Monitor.ssl_monitoring_enabled.is_(True),
                or_(
                    Monitor.ssl_next_check_at.is_(None),
                    Monitor.ssl_next_check_at <= now,
                ),
            )
            .order_by(Monitor.ssl_next_check_at.asc().nullsfirst())
        ).all()
    )

    return monitor_ids


def evaluate_ssl_result_and_notify(
    db: Session,
    monitor: Monitor,
    result: SSLCheckResult,
    checked_at: datetime,
) -> None:
    previous_status = monitor.ssl_status
    previous_cert_id = monitor.ssl_fingerprint_sha256 or (
        monitor.ssl_expires_at.isoformat() if monitor.ssl_expires_at else None
    )

    current_cert_id = result.fingerprint_sha256 or (
        result.expires_at.isoformat() if result.expires_at else "none"
    )

    # 1. Renewal Detection:
    # If previous status was expiring_soon, expired, or invalid
    # and now certificate is valid and is a renewed/different cert (or longer expiry)
    had_active_warning = previous_status in {"expiring_soon", "expired", "invalid"}
    is_now_valid = result.status == "valid"
    is_different_cert = (
        current_cert_id != previous_cert_id
        or (
            result.expires_at is not None
            and monitor.ssl_expires_at is not None
            and to_utc(result.expires_at) > to_utc(monitor.ssl_expires_at)
        )
    )

    if had_active_warning and is_now_valid and (is_different_cert or previous_status in {"expired", "invalid"}):
        milestone = "renewed"
        existing_log = db.scalar(
            select(SSLNotificationLog).where(
                SSLNotificationLog.monitor_id == monitor.id,
                SSLNotificationLog.cert_identifier == current_cert_id,
                SSLNotificationLog.milestone == milestone,
            )
        )
        if not existing_log:
            notification = Notification(
                user_id=monitor.user_id,
                monitor_id=monitor.id,
                incident_id=None,
                event_type="ssl_renewed",
                title="SSL Certificate Renewed",
                message=f"A valid certificate was detected for {monitor.name}.",
                is_read=False,
            )
            db.add(notification)
            db.flush()

            log_entry = SSLNotificationLog(
                monitor_id=monitor.id,
                cert_identifier=current_cert_id,
                milestone=milestone,
                notified_at=checked_at,
                notification_id=notification.id,
            )
            db.add(log_entry)

    # 2. Milestone Warnings:
    # Milestone options: 30, 14, 7, 3, 1
    # Only emit notifications within user configured warning threshold.
    if result.status in {"valid", "expiring_soon"} and result.days_remaining is not None:
        eligible_milestones = [
            m
            for m in [30, 14, 7, 3, 1]
            if m <= monitor.ssl_warning_days and result.days_remaining <= m
        ]
        if eligible_milestones:
            active_m = min(eligible_milestones)
            milestone_key = f"milestone_{active_m}d"
            existing_log = db.scalar(
                select(SSLNotificationLog).where(
                    SSLNotificationLog.monitor_id == monitor.id,
                    SSLNotificationLog.cert_identifier == current_cert_id,
                    SSLNotificationLog.milestone == milestone_key,
                )
            )
            if not existing_log:
                if result.days_remaining <= 0:
                    hours_remaining = (
                        max(1, int((to_utc(result.expires_at) - to_utc(checked_at)).total_seconds() // 3600))
                        if result.expires_at
                        else 0
                    )
                    time_str = f"{hours_remaining} hours" if hours_remaining > 0 else "less than 1 day"
                    msg = f"The certificate for {monitor.name} expires in {time_str}."
                elif result.days_remaining == 1:
                    msg = f"The certificate for {monitor.name} expires in 1 day."
                else:
                    msg = f"The certificate for {monitor.name} expires in {result.days_remaining} days."

                notification = Notification(
                    user_id=monitor.user_id,
                    monitor_id=monitor.id,
                    incident_id=None,
                    event_type=f"ssl_expiring_{active_m}d",
                    title="SSL Certificate Expiring",
                    message=msg,
                    is_read=False,
                )
                db.add(notification)
                db.flush()

                # Record the active milestone and mark any superseded larger milestones
                for m in eligible_milestones:
                    m_key = f"milestone_{m}d"
                    already_recorded = db.scalar(
                        select(SSLNotificationLog).where(
                            SSLNotificationLog.monitor_id == monitor.id,
                            SSLNotificationLog.cert_identifier == current_cert_id,
                            SSLNotificationLog.milestone == m_key,
                        )
                    )
                    if not already_recorded:
                        log_entry = SSLNotificationLog(
                            monitor_id=monitor.id,
                            cert_identifier=current_cert_id,
                            milestone=m_key,
                            notified_at=checked_at,
                            notification_id=notification.id if m == active_m else None,
                        )
                        db.add(log_entry)

    # 3. Certificate Expired Notification:
    if result.status == "expired":
        milestone = "expired"
        existing_log = db.scalar(
            select(SSLNotificationLog).where(
                SSLNotificationLog.monitor_id == monitor.id,
                SSLNotificationLog.cert_identifier == current_cert_id,
                SSLNotificationLog.milestone == milestone,
            )
        )
        if not existing_log:
            notification = Notification(
                user_id=monitor.user_id,
                monitor_id=monitor.id,
                incident_id=None,
                event_type="ssl_expired",
                title="SSL Certificate Expired",
                message=f"The certificate for {monitor.name} has expired.",
                is_read=False,
            )
            db.add(notification)
            db.flush()

            log_entry = SSLNotificationLog(
                monitor_id=monitor.id,
                cert_identifier=current_cert_id,
                milestone=milestone,
                notified_at=checked_at,
                notification_id=notification.id,
            )
            db.add(log_entry)

    # 4. Certificate Invalid Notification:
    if result.status == "invalid":
        milestone = "invalid"
        existing_log = db.scalar(
            select(SSLNotificationLog).where(
                SSLNotificationLog.monitor_id == monitor.id,
                SSLNotificationLog.cert_identifier == current_cert_id,
                SSLNotificationLog.milestone == milestone,
            )
        )
        if not existing_log:
            err_detail = f": {result.error_message}" if result.error_message else "."
            notification = Notification(
                user_id=monitor.user_id,
                monitor_id=monitor.id,
                incident_id=None,
                event_type="ssl_invalid",
                title="SSL Certificate Invalid",
                message=f"The certificate for {monitor.name} is invalid{err_detail}",
                is_read=False,
            )
            db.add(notification)
            db.flush()

            log_entry = SSLNotificationLog(
                monitor_id=monitor.id,
                cert_identifier=current_cert_id,
                milestone=milestone,
                notified_at=checked_at,
                notification_id=notification.id,
            )
            db.add(log_entry)

    # 5. Update Monitor state
    monitor.ssl_status = result.status
    monitor.ssl_last_checked_at = checked_at
    monitor.ssl_error_type = result.error_type
    monitor.ssl_error_message = result.error_message

    if result.status in {"valid", "expiring_soon", "expired"}:
        monitor.ssl_last_successful_check_at = checked_at
        monitor.ssl_expires_at = result.expires_at
        monitor.ssl_issuer = result.issuer
        monitor.ssl_subject = result.subject
        monitor.ssl_serial_number = result.serial_number
        monitor.ssl_fingerprint_sha256 = result.fingerprint_sha256
        monitor.ssl_valid_from = result.valid_from

    # Schedule next check
    if result.status == "check_failed":
        monitor.ssl_next_check_at = checked_at + timedelta(minutes=RETRY_INTERVAL_MINUTES)
    else:
        monitor.ssl_next_check_at = checked_at + timedelta(hours=CHECK_INTERVAL_HOURS)

    # 6. Record history in SSLCheck
    check_entry = SSLCheck(
        monitor_id=monitor.id,
        checked_at=checked_at,
        status=result.status,
        issuer=result.issuer,
        subject=result.subject,
        valid_from=result.valid_from,
        expires_at=result.expires_at,
        fingerprint_sha256=result.fingerprint_sha256,
        serial_number=result.serial_number,
        error_type=result.error_type,
        error_message=result.error_message,
    )
    db.add(check_entry)


async def check_one_ssl_monitor(
    monitor_id: int,
    semaphore: asyncio.Semaphore,
) -> dict:
    async with semaphore:
        db = SessionLocal()
        try:
            monitor = db.get(Monitor, monitor_id)
            if monitor is None:
                return {"monitor_id": monitor_id, "result": "not_found"}

            if not monitor.is_active or not monitor.ssl_monitoring_enabled:
                return {"monitor_id": monitor_id, "result": "skipped"}

            url = monitor.url
            warning_days = monitor.ssl_warning_days
            timeout_seconds = monitor.timeout_seconds
        finally:
            db.close()

        # Perform network I/O with NO database session held open
        result = await check_ssl(
            url=url,
            warning_days=warning_days,
            timeout_seconds=timeout_seconds,
        )

        checked_at = utcnow()

        db = SessionLocal()
        try:
            monitor = db.get(Monitor, monitor_id)
            if monitor is None:
                return {"monitor_id": monitor_id, "result": "not_found"}

            evaluate_ssl_result_and_notify(
                db=db,
                monitor=monitor,
                result=result,
                checked_at=checked_at,
            )

            db.commit()
            db.refresh(monitor)

            return {
                "monitor_id": monitor.id,
                "name": monitor.name,
                "ssl_status": monitor.ssl_status,
                "ssl_expires_at": monitor.ssl_expires_at,
                "days_remaining": result.days_remaining,
                "error_type": monitor.ssl_error_type,
                "ssl_next_check_at": monitor.ssl_next_check_at,
            }
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


async def run_due_ssl_checks() -> dict:
    db = SessionLocal()
    try:
        monitor_ids = get_due_ssl_monitor_ids(db)
    finally:
        db.close()

    if not monitor_ids:
        return {
            "due": 0,
            "checked": 0,
            "results": [],
        }

    semaphore = asyncio.Semaphore(MAX_SSL_CONCURRENCY)

    results = await asyncio.gather(
        *[
            check_one_ssl_monitor(
                monitor_id=monitor_id,
                semaphore=semaphore,
            )
            for monitor_id in monitor_ids
        ],
        return_exceptions=True,
    )

    normalized_results = []
    for monitor_id, res in zip(monitor_ids, results):
        if isinstance(res, Exception):
            normalized_results.append(
                {
                    "monitor_id": monitor_id,
                    "result": "worker_error",
                    "error": str(res)[:500],
                }
            )
        else:
            normalized_results.append(res)

    return {
        "due": len(monitor_ids),
        "checked": len(normalized_results),
        "results": normalized_results,
    }
