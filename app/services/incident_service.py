from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.incident import Incident
from app.models.monitor import Monitor
from app.services.http_checker import CheckResult


def get_open_incident(
    db: Session,
    monitor_id: int,
) -> Incident | None:
    return db.scalar(
        select(Incident)
        .where(
            Incident.monitor_id == monitor_id,
            Incident.status == "open",
        )
        .order_by(Incident.started_at.desc())
    )


def open_incident(
    db: Session,
    monitor: Monitor,
    result: CheckResult,
    checked_at: datetime,
) -> Incident:
    existing = get_open_incident(db, monitor.id)

    if existing is not None:
        existing.last_http_status = result.http_status
        return existing

    cause = result.error_message

    if not cause:
        if result.http_status is not None:
            cause = f"HTTP {result.http_status}"
        else:
            cause = result.error_type or "Monitor check failed"

    incident = Incident(
        monitor_id=monitor.id,
        status="open",
        cause=cause,
        start_http_status=result.http_status,
        last_http_status=result.http_status,
        started_at=checked_at,
    )

    db.add(incident)

    return incident


def update_open_incident(
    db: Session,
    monitor: Monitor,
    result: CheckResult,
) -> Incident | None:
    incident = get_open_incident(db, monitor.id)

    if incident is None:
        return None

    incident.last_http_status = result.http_status

    return incident


def resolve_incident(
    db: Session,
    monitor: Monitor,
    checked_at: datetime,
) -> Incident | None:
    incident = get_open_incident(db, monitor.id)

    if incident is None:
        return None

    incident.status = "resolved"
    incident.resolved_at = checked_at

    duration = checked_at - incident.started_at
    incident.duration_seconds = max(
        0,
        int(duration.total_seconds()),
    )

    return incident


def handle_incident_transition(
    db: Session,
    monitor: Monitor,
    result: CheckResult,
    previous_status: str,
    checked_at: datetime,
) -> dict:
    current_status = monitor.status

    event = None
    incident = None

    if previous_status != "down" and current_status == "down":
        incident = open_incident(
            db=db,
            monitor=monitor,
            result=result,
            checked_at=checked_at,
        )

        event = "opened"

    elif previous_status == "down" and current_status == "down":
        incident = update_open_incident(
            db=db,
            monitor=monitor,
            result=result,
        )

    elif (
        previous_status == "down"
        and current_status in {"operational", "degraded"}
    ):
        incident = resolve_incident(
            db=db,
            monitor=monitor,
            checked_at=checked_at,
        )

        if incident is not None:
            event = "resolved"

    # Important:
    # make sure a newly created incident already has an ID
    # before we return its transition metadata.
    if incident is not None:
        db.flush()

    return {
        "event": event,
        "incident_id": incident.id if incident is not None else None,
    }
