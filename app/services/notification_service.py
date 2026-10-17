from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.incident import Incident
from app.models.monitor import Monitor
from app.models.notification import Notification


def create_incident_notification(
    db: Session,
    monitor: Monitor,
    incident_id: int | None,
    event: str | None,
) -> Notification | None:
    if event not in {"opened", "resolved"}:
        return None

    if incident_id is None:
        return None

    incident = db.get(Incident, incident_id)

    if incident is None:
        return None

    event_type = (
        "incident_opened"
        if event == "opened"
        else "incident_resolved"
    )

    existing = db.scalar(
        select(Notification).where(
            Notification.incident_id == incident.id,
            Notification.event_type == event_type,
        )
    )

    if existing is not None:
        return existing

    if event == "opened":
        title = f"{monitor.name} is DOWN"

        if incident.cause:
            message = incident.cause
        else:
            message = "The monitor has been confirmed unavailable."

    else:
        title = f"{monitor.name} is back online"

        if incident.duration_seconds is not None:
            message = (
                f"Service recovered after "
                f"{format_duration(incident.duration_seconds)} of downtime."
            )
        else:
            message = "Service has recovered."

    notification = Notification(
        user_id=monitor.user_id,
        monitor_id=monitor.id,
        incident_id=incident.id,
        event_type=event_type,
        title=title,
        message=message,
        is_read=False,
    )

    db.add(notification)
    db.flush()

    try:
        from app.services.email_service import enqueue_email_for_notification
        enqueue_email_for_notification(db, notification)
    except Exception:
        pass

    return notification


def format_duration(seconds: int) -> str:
    seconds = max(0, seconds)

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []

    if hours:
        parts.append(f"{hours}h")

    if minutes:
        parts.append(f"{minutes}m")

    if seconds or not parts:
        parts.append(f"{seconds}s")

    return " ".join(parts)
