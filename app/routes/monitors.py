from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models.monitor import Monitor
from app.models.user import User
from app.schemas.monitor import (
    MonitorCreate,
    MonitorListResponse,
    MonitorResponse,
    MonitorUpdate,
)


router = APIRouter(
    prefix="/monitors",
    tags=["monitors"],
)


def utcnow():
    return datetime.now(timezone.utc)


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

    return {
        "items": monitors,
        "count": len(monitors),
    }


@router.post(
    "",
    response_model=MonitorResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_monitor(
    payload: MonitorCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    monitor = Monitor(
        user_id=user.id,
        type="http",
        name=payload.name.strip(),
        url=str(payload.url),
        interval_seconds=payload.interval_seconds,
        timeout_seconds=payload.timeout_seconds,
        expected_status_min=payload.expected_status_min,
        expected_status_max=payload.expected_status_max,
        degraded_threshold_ms=payload.degraded_threshold_ms,
        status="unknown",
        is_active=True,
        next_check_at=utcnow(),
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
def update_monitor(
    monitor_id: int,
    payload: MonitorUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
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

    for key, value in updates.items():
        setattr(monitor, key, value)

    if "is_active" in updates:
        if monitor.is_active and not previous_active:
            monitor.status = "unknown"
            monitor.consecutive_failures = 0
            monitor.consecutive_successes = 0
            monitor.next_check_at = utcnow()

        elif not monitor.is_active:
            monitor.status = "paused"
            monitor.next_check_at = None

    elif monitor.is_active and reset_state:
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
