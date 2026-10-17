import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.heartbeat_event import HeartbeatEvent
from app.models.incident import Incident
from app.models.monitor import Monitor
from app.models.notification import Notification


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def generate_heartbeat_token() -> tuple[str, str]:
    """
    Generate a cryptographically secure 256-bit token and its SHA-256 verifier hash.
    Returns (raw_token, token_hash).
    """
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_heartbeat_token(raw_token)
    return raw_token, token_hash


def hash_heartbeat_token(raw_token: str) -> str:
    """Hash raw heartbeat token using SHA-256."""
    return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()


def process_heartbeat(
    db: Session,
    raw_token: str,
    client_ip: str | None = None,
) -> tuple[bool, str, Monitor | None]:
    """
    Process incoming heartbeat ping.
    Validates token, updates deadlines, resolves open incidents on recovery,
    and returns (success, message, monitor).
    """
    token_hash = hash_heartbeat_token(raw_token)

    # Use row locking if available to serialize concurrent heartbeat/worker checks
    stmt = (
        select(Monitor)
        .where(
            Monitor.type == "heartbeat",
            Monitor.heartbeat_token_hash == token_hash,
        )
    )
    try:
        stmt = stmt.with_for_update()
    except Exception:
        pass

    monitor = db.scalar(stmt)
    if monitor is None:
        return False, "Invalid heartbeat token", None

    now = utcnow()

    # If paused, record event but do not activate or change deadlines
    if not monitor.is_active:
        event = HeartbeatEvent(
            monitor_id=monitor.id,
            received_at=now,
            event_type="ping_paused",
            ip_address=client_ip,
        )
        db.add(event)
        db.commit()
        return True, "Heartbeat recorded (monitor paused)", monitor

    # Calculate schedule deadlines
    interval_sec = monitor.expected_interval_seconds or 3600
    grace_sec = monitor.grace_period_seconds or 600

    next_expected = now + timedelta(seconds=interval_sec)
    deadline = next_expected + timedelta(seconds=grace_sec)

    previous_status = monitor.status

    monitor.last_heartbeat_at = now
    monitor.next_expected_at = next_expected
    monitor.deadline_at = deadline
    monitor.status = "healthy"
    monitor.last_success_at = now
    monitor.consecutive_successes = (monitor.consecutive_successes or 0) + 1
    monitor.consecutive_failures = 0

    event_type = "ping"

    # If previously missed, resolve incident and emit recovery notification
    if previous_status == "missed":
        event_type = "recovery"
        open_incident = db.scalar(
            select(Incident)
            .where(
                Incident.monitor_id == monitor.id,
                Incident.status == "open",
            )
            .order_by(Incident.started_at.desc())
        )
        if open_incident:
            open_incident.status = "resolved"
            open_incident.resolved_at = now
            if open_incident.started_at:
                start_dt = open_incident.started_at
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                duration = int((now - start_dt).total_seconds())
                open_incident.duration_seconds = max(0, duration)

            # Check if recovery notification already exists for this incident
            existing_notif = db.scalar(
                select(Notification).where(
                    Notification.incident_id == open_incident.id,
                    Notification.event_type == "heartbeat_recovered",
                )
            )
            if not existing_notif:
                recovery_notif = Notification(
                    user_id=monitor.user_id,
                    monitor_id=monitor.id,
                    incident_id=open_incident.id,
                    event_type="heartbeat_recovered",
                    title=f"{monitor.name} is running again",
                    message="A new successful heartbeat was received.",
                    is_read=False,
                )
                db.add(recovery_notif)

    event = HeartbeatEvent(
        monitor_id=monitor.id,
        received_at=now,
        event_type=event_type,
        ip_address=client_ip,
    )
    db.add(event)
    db.commit()
    db.refresh(monitor)

    return True, "Heartbeat received", monitor


def get_overdue_heartbeat_monitor_ids(db: Session) -> list[int]:
    """Retrieve IDs of active healthy heartbeat monitors past their deadline."""
    now = utcnow()
    return list(
        db.scalars(
            select(Monitor.id)
            .where(
                Monitor.is_active.is_(True),
                Monitor.type == "heartbeat",
                Monitor.status == "healthy",
                Monitor.deadline_at.is_not(None),
                Monitor.deadline_at <= now,
            )
            .order_by(Monitor.deadline_at.asc())
        ).all()
    )


def evaluate_overdue_heartbeat(db: Session, monitor_id: int) -> bool:
    """
    Evaluate one overdue heartbeat monitor.
    Transition to 'missed', open incident, and emit 'heartbeat_missed' notification.
    """
    now = utcnow()

    stmt = select(Monitor).where(Monitor.id == monitor_id)
    try:
        stmt = stmt.with_for_update()
    except Exception:
        pass

    monitor = db.scalar(stmt)
    if not monitor:
        return False

    # Double check condition inside locked transaction
    if not monitor.is_active or monitor.type != "heartbeat" or monitor.status != "healthy":
        return False

    if not monitor.deadline_at:
        return False

    deadline = monitor.deadline_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)

    if deadline > now:
        return False

    # Transition to missed
    monitor.status = "missed"
    monitor.last_failure_at = now
    monitor.consecutive_failures = (monitor.consecutive_failures or 0) + 1
    monitor.consecutive_successes = 0

    deadline_str = deadline.strftime("%H:%M")
    cause_msg = f"Expected heartbeat was not received by {deadline_str} UTC."

    # Open incident if none is open
    existing_incident = db.scalar(
        select(Incident).where(
            Incident.monitor_id == monitor.id,
            Incident.status == "open",
        )
    )

    incident = existing_incident
    if not incident:
        incident = Incident(
            monitor_id=monitor.id,
            status="open",
            cause=cause_msg,
            started_at=deadline,
        )
        db.add(incident)
        db.flush()

    # Emit notification if not already notified for this incident
    existing_notif = db.scalar(
        select(Notification).where(
            Notification.incident_id == incident.id,
            Notification.event_type == "heartbeat_missed",
        )
    )
    if not existing_notif:
        notif = Notification(
            user_id=monitor.user_id,
            monitor_id=monitor.id,
            incident_id=incident.id,
            event_type="heartbeat_missed",
            title=f"{monitor.name} heartbeat missed",
            message="Expected heartbeat was not received within the configured interval and grace period.",
            is_read=False,
        )
        db.add(notif)

    event = HeartbeatEvent(
        monitor_id=monitor.id,
        received_at=now,
        event_type="missed",
        ip_address=None,
    )
    db.add(event)
    db.commit()

    return True


async def run_due_heartbeat_checks() -> dict:
    """Worker task: checks for overdue heartbeats and triggers missed alerts."""
    db = SessionLocal()
    try:
        overdue_ids = get_overdue_heartbeat_monitor_ids(db)
    finally:
        db.close()

    if not overdue_ids:
        return {"due": 0, "processed": 0}

    processed_count = 0
    for m_id in overdue_ids:
        session = SessionLocal()
        try:
            if evaluate_overdue_heartbeat(session, m_id):
                processed_count += 1
        except Exception:
            session.rollback()
        finally:
            session.close()

    return {"due": len(overdue_ids), "processed": processed_count}
