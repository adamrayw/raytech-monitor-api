from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models.email_delivery import EmailDelivery
from app.models.notification_preference import NotificationPreference
from app.models.user import User
from app.schemas.notification_preference import (
    EmailDeliveryItem,
    EmailHistoryResponse,
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate,
    TestEmailResponse,
)
from app.services.email_service import (
    enqueue_test_email,
    get_test_recipient,
    is_email_enabled,
    is_test_mode,
)
from app.services.subscription_service import enforce_email_access

router = APIRouter(prefix="/settings", tags=["settings"])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_or_create_preferences(
    db: Session,
    user_id: int,
) -> NotificationPreference:
    pref = db.scalar(
        select(NotificationPreference).where(
            NotificationPreference.user_id == user_id
        )
    )
    if pref is None:
        pref = NotificationPreference(
            user_id=user_id,
            email_enabled=False,
            http_down_enabled=True,
            http_recovered_enabled=True,
            heartbeat_missed_enabled=True,
            heartbeat_recovered_enabled=True,
            ssl_expiring_enabled=True,
            ssl_expired_enabled=True,
            ssl_invalid_enabled=True,
            ssl_recovered_enabled=True,
        )
        db.add(pref)
        db.commit()
        db.refresh(pref)
    return pref


@router.get(
    "/notifications",
    response_model=NotificationPreferenceResponse,
)
def get_notification_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pref = get_or_create_preferences(db, current_user.id)
    test_mode = is_test_mode()
    test_recip = get_test_recipient() if test_mode else None

    return NotificationPreferenceResponse(
        email_enabled=pref.email_enabled,
        http_down_enabled=pref.http_down_enabled,
        http_recovered_enabled=pref.http_recovered_enabled,
        heartbeat_missed_enabled=pref.heartbeat_missed_enabled,
        heartbeat_recovered_enabled=pref.heartbeat_recovered_enabled,
        ssl_expiring_enabled=pref.ssl_expiring_enabled,
        ssl_expired_enabled=pref.ssl_expired_enabled,
        ssl_invalid_enabled=pref.ssl_invalid_enabled,
        ssl_recovered_enabled=pref.ssl_recovered_enabled,
        test_mode=test_mode,
        test_recipient=test_recip,
        account_email=current_user.email,
    )


@router.patch(
    "/notifications",
    response_model=NotificationPreferenceResponse,
)
def update_notification_settings(
    payload: NotificationPreferenceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pref = get_or_create_preferences(db, current_user.id)

    update_data = payload.model_dump(exclude_unset=True)
    if update_data.get("email_enabled") is True and not pref.email_enabled:
        enforce_email_access(db, current_user.id)

    for field, value in update_data.items():
        if value is not None and hasattr(pref, field):
            setattr(pref, field, value)

    pref.updated_at = utcnow()
    db.commit()
    db.refresh(pref)

    test_mode = is_test_mode()
    test_recip = get_test_recipient() if test_mode else None

    return NotificationPreferenceResponse(
        email_enabled=pref.email_enabled,
        http_down_enabled=pref.http_down_enabled,
        http_recovered_enabled=pref.http_recovered_enabled,
        heartbeat_missed_enabled=pref.heartbeat_missed_enabled,
        heartbeat_recovered_enabled=pref.heartbeat_recovered_enabled,
        ssl_expiring_enabled=pref.ssl_expiring_enabled,
        ssl_expired_enabled=pref.ssl_expired_enabled,
        ssl_invalid_enabled=pref.ssl_invalid_enabled,
        ssl_recovered_enabled=pref.ssl_recovered_enabled,
        test_mode=test_mode,
        test_recipient=test_recip,
        account_email=current_user.email,
    )


@router.post(
    "/notifications/test-email",
    response_model=TestEmailResponse,
)
def send_test_email(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    enforce_email_access(db, current_user.id)

    if not is_email_enabled():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email notifications are globally disabled on this server.",
        )

    # Rate limiting: limit test email requests to at most once every 30 seconds per user
    thirty_seconds_ago = utcnow() - timedelta(seconds=30)
    recent_test = db.scalar(
        select(EmailDelivery).where(
            EmailDelivery.user_id == current_user.id,
            EmailDelivery.event_type == "test_email",
            EmailDelivery.created_at >= thirty_seconds_ago,
        )
    )
    if recent_test is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="A test email was already requested recently. Please wait 30 seconds before sending another test email.",
        )

    test_mode = is_test_mode()
    test_recipient = get_test_recipient()

    if test_mode and not test_recipient:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Test mode is enabled on the server, but no EMAIL_TEST_RECIPIENT is configured.",
        )

    delivery = enqueue_test_email(db, current_user)
    db.commit()

    recipient_note = (
        test_recipient
        if test_mode
        else current_user.email
    )

    msg = (
        f"Test email queued for delivery to authorized test recipient ({recipient_note}) because EMAIL_TEST_MODE is enabled."
        if test_mode
        else f"Test email queued for delivery to {current_user.email}."
    )

    return TestEmailResponse(
        status="queued",
        delivery_id=delivery.id,
        recipient=recipient_note,
        test_mode=test_mode,
        message=msg,
    )


@router.get(
    "/notifications/email-history",
    response_model=EmailHistoryResponse,
)
def get_email_history(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    deliveries = db.scalars(
        select(EmailDelivery)
        .where(EmailDelivery.user_id == current_user.id)
        .order_by(EmailDelivery.created_at.desc())
        .limit(limit)
    ).all()

    items = [
        EmailDeliveryItem(
            id=d.id,
            event_type=d.event_type,
            subject=d.subject,
            recipient_email=d.recipient_email,
            status=d.status,
            attempt_count=d.attempt_count,
            provider_message_id=d.provider_message_id,
            last_error_message=d.last_error_message,
            created_at=d.created_at,
            sent_at=d.sent_at,
        )
        for d in deliveries
    ]

    return EmailHistoryResponse(
        items=items,
        count=len(items),
    )
