from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.incident import Incident
from app.models.monitor import Monitor
from app.models.status_page import StatusPage, StatusPageMonitor
from app.schemas.status_page import (
    PublicHistoryResponse,
    PublicIncidentItem,
    PublicIncidentsResponse,
    PublicServiceHistory,
    PublicServiceItem,
    PublicStatusPageResponse,
)
from app.services.status_page_service import (
    calculate_overall_status,
    get_30_day_history_for_monitor,
    map_monitor_public_status,
    sanitize_incident_cause,
    utcnow,
)

router = APIRouter(prefix="/public/status", tags=["public-status"])


def get_published_status_page_or_404(db: Session, slug: str) -> StatusPage:
    cleaned_slug = slug.strip().lower()
    page = db.scalar(
        select(StatusPage).where(
            StatusPage.slug == cleaned_slug,
            StatusPage.is_published.is_(True),
        )
    )
    if page is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Status page not found.",
        )
    return page


@router.get("/{slug}", response_model=PublicStatusPageResponse)
def get_public_status_page(
    slug: str,
    db: Session = Depends(get_db),
):
    page = get_published_status_page_or_404(db, slug)

    page_monitors = (
        db.scalars(
            select(StatusPageMonitor)
            .where(StatusPageMonitor.status_page_id == page.id)
            .order_by(StatusPageMonitor.display_order.asc())
        ).all()
    )

    services: list[PublicServiceItem] = []
    public_statuses: list[str] = []

    for spm in page_monitors:
        mon = db.get(Monitor, spm.monitor_id)
        if mon:
            pub_status = map_monitor_public_status(mon)
            public_statuses.append(pub_status)
            services.append(
                PublicServiceItem(
                    id=spm.id,
                    name=spm.display_name or mon.name,
                    type=mon.type or "http",
                    status=pub_status,
                    display_order=spm.display_order,
                    last_checked_at=mon.last_checked_at,
                )
            )

    overall_status_code, headline = calculate_overall_status(public_statuses)

    return PublicStatusPageResponse(
        name=page.name,
        slug=page.slug,
        description=page.description,
        accent_color=page.accent_color,
        logo_url=page.logo_url,
        overall_status=overall_status_code,
        overall_headline=headline,
        show_incident_history=page.show_incident_history,
        updated_at=page.updated_at,
        services=services,
    )


@router.get("/{slug}/history", response_model=PublicHistoryResponse)
def get_public_status_history(
    slug: str,
    range: str = Query(default="30d"),
    db: Session = Depends(get_db),
):
    page = get_published_status_page_or_404(db, slug)

    page_monitors = (
        db.scalars(
            select(StatusPageMonitor)
            .where(StatusPageMonitor.status_page_id == page.id)
            .order_by(StatusPageMonitor.display_order.asc())
        ).all()
    )

    services_history: list[PublicServiceHistory] = []

    for spm in page_monitors:
        mon = db.get(Monitor, spm.monitor_id)
        if mon:
            days = get_30_day_history_for_monitor(db, mon)

            # Compute 30d aggregate uptime if HTTP
            days_with_data = [d for d in days if d["has_data"]]
            if days_with_data:
                total_checks = sum(d["total_checks"] for d in days_with_data)
                success_checks = sum(d["successful_checks"] for d in days_with_data)
                uptime_30d = (
                    round((success_checks / total_checks) * 100, 2)
                    if total_checks > 0
                    else None
                )
            else:
                uptime_30d = None

            note = (
                "Uptime history not available for heartbeat monitors"
                if (mon.type or "http") == "heartbeat"
                else None
            )

            services_history.append(
                PublicServiceHistory(
                    service_id=spm.id,
                    service_name=spm.display_name or mon.name,
                    service_type=mon.type or "http",
                    uptime_30d_percent=uptime_30d,
                    days=days,
                    note=note,
                )
            )

    return PublicHistoryResponse(
        services=services_history,
        range="30d",
    )


@router.get("/{slug}/incidents", response_model=PublicIncidentsResponse)
def get_public_status_incidents(
    slug: str,
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    page = get_published_status_page_or_404(db, slug)

    if not page.show_incident_history:
        return PublicIncidentsResponse(items=[], count=0)

    page_monitors = (
        db.scalars(
            select(StatusPageMonitor).where(
                StatusPageMonitor.status_page_id == page.id
            )
        ).all()
    )
    monitor_ids = [spm.monitor_id for spm in page_monitors]
    if not monitor_ids:
        return PublicIncidentsResponse(items=[], count=0)

    # Monitor ID to public display name map
    monitor_names = {}
    for spm in page_monitors:
        mon = db.get(Monitor, spm.monitor_id)
        if mon:
            monitor_names[mon.id] = spm.display_name or mon.name

    # Query incidents in the past 30 days or any currently open incident for these monitors
    thirty_days_ago = utcnow() - timedelta(days=30)
    incidents = db.scalars(
        select(Incident)
        .where(
            Incident.monitor_id.in_(monitor_ids),
            or_(
                Incident.status == "open",
                Incident.started_at >= thirty_days_ago,
            ),
        )
        .order_by(Incident.started_at.desc())
        .limit(limit)
    ).all()

    items: list[PublicIncidentItem] = []
    for inc in incidents:
        items.append(
            PublicIncidentItem(
                id=inc.id,
                service_name=monitor_names.get(inc.monitor_id, "Monitored Service"),
                status=inc.status,
                started_at=inc.started_at,
                resolved_at=inc.resolved_at,
                duration_seconds=inc.duration_seconds,
                public_message=sanitize_incident_cause(inc.cause),
            )
        )

    return PublicIncidentsResponse(items=items, count=len(items))
