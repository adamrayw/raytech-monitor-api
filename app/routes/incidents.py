from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models.incident import Incident
from app.models.monitor import Monitor
from app.models.user import User


router = APIRouter(
    prefix="/incidents",
    tags=["incidents"],
)


def serialize_incident(
    incident: Incident,
    monitor: Monitor,
) -> dict:
    return {
        "id": incident.id,
        "monitor_id": incident.monitor_id,
        "monitor_name": monitor.name,
        "monitor_url": monitor.url,
        "status": incident.status,
        "cause": incident.cause,
        "start_http_status": incident.start_http_status,
        "last_http_status": incident.last_http_status,
        "started_at": incident.started_at,
        "resolved_at": incident.resolved_at,
        "duration_seconds": incident.duration_seconds,
        "resolution_reason": getattr(incident, "resolution_reason", None),
        "created_at": incident.created_at,
        "updated_at": incident.updated_at,
    }


@router.get("")
def list_incidents(
    monitor_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = (
        select(Incident, Monitor)
        .join(
            Monitor,
            Monitor.id == Incident.monitor_id,
        )
        .where(Monitor.user_id == user.id)
    )

    if monitor_id is not None:
        query = query.where(Incident.monitor_id == monitor_id)

    query = query.order_by(Incident.started_at.desc()).limit(limit)

    rows = db.execute(query).all()

    items = [
        serialize_incident(incident, monitor)
        for incident, monitor in rows
    ]

    return {
        "items": items,
        "count": len(items),
    }


@router.get("/{incident_id}")
def get_incident(
    incident_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = db.execute(
        select(Incident, Monitor)
        .join(
            Monitor,
            Monitor.id == Incident.monitor_id,
        )
        .where(
            Incident.id == incident_id,
            Monitor.user_id == user.id,
        )
    ).first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Incident not found",
        )

    incident, monitor = row

    return serialize_incident(
        incident,
        monitor,
    )
