import os
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models.monitor import Monitor
from app.models.status_page import StatusPage, StatusPageMonitor
from app.models.user import User
from app.schemas.status_page import (
    StatusPageCreate,
    StatusPageDetailResponse,
    StatusPageListResponse,
    StatusPageMonitorResponse,
    StatusPageSummaryResponse,
    StatusPageUpdate,
)
from app.services.status_page_service import (
    map_monitor_public_status,
    utcnow,
    validate_slug,
    verify_user_monitors_ownership,
)

router = APIRouter(prefix="/status-pages", tags=["status-pages"])


def get_frontend_url() -> str:
    return os.environ.get("FRONTEND_URL", "https://monitor.raytech.cloud").rstrip("/")


def build_summary_response(page: StatusPage, monitor_count: int) -> StatusPageSummaryResponse:
    frontend_url = get_frontend_url()
    return StatusPageSummaryResponse(
        id=page.id,
        name=page.name,
        slug=page.slug,
        description=page.description,
        accent_color=page.accent_color,
        logo_url=page.logo_url,
        is_published=page.is_published,
        show_incident_history=page.show_incident_history,
        monitor_count=monitor_count,
        public_url=f"{frontend_url}/status/{page.slug}",
        created_at=page.created_at,
        updated_at=page.updated_at,
    )


def build_detail_response(db: Session, page: StatusPage) -> StatusPageDetailResponse:
    frontend_url = get_frontend_url()
    page_monitors = (
        db.scalars(
            select(StatusPageMonitor)
            .where(StatusPageMonitor.status_page_id == page.id)
            .order_by(StatusPageMonitor.display_order.asc())
        ).all()
    )

    monitors_res: list[StatusPageMonitorResponse] = []
    for spm in page_monitors:
        mon = db.get(Monitor, spm.monitor_id)
        if mon:
            monitors_res.append(
                StatusPageMonitorResponse(
                    id=spm.id,
                    monitor_id=mon.id,
                    monitor_name=mon.name,
                    monitor_type=mon.type or "http",
                    display_name=spm.display_name,
                    display_order=spm.display_order,
                    current_status=map_monitor_public_status(mon),
                )
            )

    return StatusPageDetailResponse(
        id=page.id,
        name=page.name,
        slug=page.slug,
        description=page.description,
        accent_color=page.accent_color,
        logo_url=page.logo_url,
        is_published=page.is_published,
        show_incident_history=page.show_incident_history,
        monitor_count=len(monitors_res),
        public_url=f"{frontend_url}/status/{page.slug}",
        created_at=page.created_at,
        updated_at=page.updated_at,
        monitors=monitors_res,
    )


@router.get("", response_model=StatusPageListResponse)
def list_status_pages(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pages = (
        db.scalars(
            select(StatusPage)
            .where(StatusPage.user_id == current_user.id)
            .order_by(StatusPage.created_at.desc())
        ).all()
    )

    items: list[StatusPageSummaryResponse] = []
    for p in pages:
        count = db.scalar(
            select(func.count(StatusPageMonitor.id)).where(
                StatusPageMonitor.status_page_id == p.id
            )
        ) or 0
        items.append(build_summary_response(p, count))

    return StatusPageListResponse(items=items, count=len(items))


@router.post("", response_model=StatusPageDetailResponse, status_code=status.HTTP_201_CREATED)
def create_status_page(
    payload: StatusPageCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cleaned_slug = validate_slug(payload.slug)

    # Check globally unique slug
    existing = db.scalar(
        select(StatusPage).where(StatusPage.slug == cleaned_slug)
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The slug '{cleaned_slug}' is already taken. Please choose another slug.",
        )

    # Verify monitor ownership
    monitor_ids = [m.monitor_id for m in payload.monitors]
    verify_user_monitors_ownership(db, current_user, monitor_ids)

    # If publishing, must have at least one monitor
    if payload.is_published and not payload.monitors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one monitor must be selected before publishing a status page.",
        )

    page = StatusPage(
        user_id=current_user.id,
        name=payload.name.strip(),
        slug=cleaned_slug,
        description=payload.description.strip() if payload.description else None,
        accent_color=payload.accent_color.strip() or "#00f0ff",
        logo_url=payload.logo_url.strip() if payload.logo_url else None,
        is_published=payload.is_published,
        show_incident_history=payload.show_incident_history,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(page)
    db.flush()

    for idx, item in enumerate(payload.monitors):
        spm = StatusPageMonitor(
            status_page_id=page.id,
            monitor_id=item.monitor_id,
            display_name=item.display_name.strip() if item.display_name else None,
            display_order=item.display_order if item.display_order is not None else idx,
            created_at=utcnow(),
        )
        db.add(spm)

    db.commit()
    db.refresh(page)

    return build_detail_response(db, page)


@router.get("/{status_page_id}", response_model=StatusPageDetailResponse)
def get_status_page(
    status_page_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    page = db.get(StatusPage, status_page_id)
    if page is None or page.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Status page not found.",
        )

    return build_detail_response(db, page)


@router.patch("/{status_page_id}", response_model=StatusPageDetailResponse)
def update_status_page(
    status_page_id: int,
    payload: StatusPageUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    page = db.get(StatusPage, status_page_id)
    if page is None or page.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Status page not found.",
        )

    if payload.slug is not None:
        cleaned_slug = validate_slug(payload.slug)
        if cleaned_slug != page.slug:
            existing = db.scalar(
                select(StatusPage).where(
                    StatusPage.slug == cleaned_slug,
                    StatusPage.id != page.id,
                )
            )
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"The slug '{cleaned_slug}' is already taken. Please choose another slug.",
                )
            page.slug = cleaned_slug

    if payload.name is not None:
        page.name = payload.name.strip()
    if payload.description is not None:
        page.description = payload.description.strip() if payload.description else None
    if payload.accent_color is not None:
        page.accent_color = payload.accent_color.strip() or "#00f0ff"
    if payload.logo_url is not None:
        page.logo_url = payload.logo_url.strip() if payload.logo_url else None
    if payload.show_incident_history is not None:
        page.show_incident_history = payload.show_incident_history

    # Handle monitor associations
    if payload.monitors is not None:
        monitor_ids = [m.monitor_id for m in payload.monitors]
        verify_user_monitors_ownership(db, current_user, monitor_ids)

        # Remove existing associations
        existing_spms = db.scalars(
            select(StatusPageMonitor).where(
                StatusPageMonitor.status_page_id == page.id
            )
        ).all()
        for spm in existing_spms:
            db.delete(spm)
        db.flush()

        for idx, item in enumerate(payload.monitors):
            spm = StatusPageMonitor(
                status_page_id=page.id,
                monitor_id=item.monitor_id,
                display_name=item.display_name.strip() if item.display_name else None,
                display_order=item.display_order if item.display_order is not None else idx,
                created_at=utcnow(),
            )
            db.add(spm)
        db.flush()

    # Check published state
    target_published = payload.is_published if payload.is_published is not None else page.is_published
    current_monitor_count = db.scalar(
        select(func.count(StatusPageMonitor.id)).where(
            StatusPageMonitor.status_page_id == page.id
        )
    ) or 0

    if target_published and current_monitor_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one monitor must be selected before publishing a status page.",
        )

    if payload.is_published is not None:
        page.is_published = payload.is_published

    page.updated_at = utcnow()
    db.commit()
    db.refresh(page)

    return build_detail_response(db, page)


@router.delete("/{status_page_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_status_page(
    status_page_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    page = db.get(StatusPage, status_page_id)
    if page is None or page.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Status page not found.",
        )

    db.delete(page)
    db.commit()


@router.post("/{status_page_id}/publish", response_model=StatusPageDetailResponse)
def publish_status_page(
    status_page_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    page = db.get(StatusPage, status_page_id)
    if page is None or page.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Status page not found.",
        )

    count = db.scalar(
        select(func.count(StatusPageMonitor.id)).where(
            StatusPageMonitor.status_page_id == page.id
        )
    ) or 0
    if count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one monitor must be selected before publishing a status page.",
        )

    page.is_published = True
    page.updated_at = utcnow()
    db.commit()
    db.refresh(page)

    return build_detail_response(db, page)


@router.post("/{status_page_id}/unpublish", response_model=StatusPageDetailResponse)
def unpublish_status_page(
    status_page_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    page = db.get(StatusPage, status_page_id)
    if page is None or page.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Status page not found.",
        )

    page.is_published = False
    page.updated_at = utcnow()
    db.commit()
    db.refresh(page)

    return build_detail_response(db, page)
