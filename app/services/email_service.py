import html
import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.email_delivery import EmailDelivery
from app.models.monitor import Monitor
from app.models.notification import Notification
from app.models.notification_preference import NotificationPreference
from app.models.user import User

logger = logging.getLogger("raytech.email")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_frontend_url() -> str:
    return os.environ.get("FRONTEND_URL", "https://monitor.raytech.cloud").rstrip("/")


def is_email_enabled() -> bool:
    return os.environ.get("EMAIL_ENABLED", "false").lower() in ("true", "1", "yes")


def is_test_mode() -> bool:
    return os.environ.get("EMAIL_TEST_MODE", "true").lower() in ("true", "1", "yes")


def get_test_recipient() -> str:
    return os.environ.get("EMAIL_TEST_RECIPIENT", "").strip()


def get_resend_api_key() -> str:
    return os.environ.get("RESEND_API_KEY", "").strip()


def get_email_from() -> str:
    return os.environ.get(
        "EMAIL_FROM",
        "Raytech Monitor <onboarding@resend.dev>",
    ).strip()


def get_email_reply_to() -> str:
    return os.environ.get("EMAIL_REPLY_TO", "").strip()


def get_resend_api_base_url() -> str:
    return os.environ.get("RESEND_API_BASE_URL", "https://api.resend.com").rstrip("/")


def get_max_attempts() -> int:
    try:
        return int(os.environ.get("EMAIL_MAX_ATTEMPTS", "5"))
    except ValueError:
        return 5


def get_batch_size() -> int:
    try:
        return int(os.environ.get("EMAIL_BATCH_SIZE", "20"))
    except ValueError:
        return 20


def get_daily_send_limit() -> int:
    try:
        return int(os.environ.get("EMAIL_DAILY_SEND_LIMIT", "80"))
    except ValueError:
        return 80


def get_per_user_daily_limit() -> int:
    try:
        return int(os.environ.get("EMAIL_PER_USER_DAILY_LIMIT", "20"))
    except ValueError:
        return 20


def is_event_enabled_for_user(
    pref: NotificationPreference,
    event_type: str,
) -> bool:
    if not pref.email_enabled:
        return False

    if event_type == "incident_opened":
        return pref.http_down_enabled
    elif event_type == "incident_resolved":
        return pref.http_recovered_enabled
    elif event_type == "heartbeat_missed":
        return pref.heartbeat_missed_enabled
    elif event_type == "heartbeat_recovered":
        return pref.heartbeat_recovered_enabled
    elif event_type.startswith("ssl_expiring"):
        return pref.ssl_expiring_enabled
    elif event_type == "ssl_expired":
        return pref.ssl_expired_enabled
    elif event_type == "ssl_invalid":
        return pref.ssl_invalid_enabled
    elif event_type in ("ssl_renewed", "ssl_recovered"):
        return pref.ssl_recovered_enabled
    elif event_type == "test_email":
        return True

    return False


def build_email_subject(
    event_type: str,
    monitor_name: str,
) -> str:
    if event_type == "incident_opened":
        return f"[Raytech Monitor] DOWN — {monitor_name}"
    elif event_type == "incident_resolved":
        return f"[Raytech Monitor] RECOVERED — {monitor_name}"
    elif event_type == "heartbeat_missed":
        return f"[Raytech Monitor] HEARTBEAT MISSED — {monitor_name}"
    elif event_type == "heartbeat_recovered":
        return f"[Raytech Monitor] HEARTBEAT RECOVERED — {monitor_name}"
    elif event_type.startswith("ssl_expiring"):
        return f"[Raytech Monitor] SSL EXPIRING — {monitor_name}"
    elif event_type == "ssl_expired":
        return f"[Raytech Monitor] SSL EXPIRED — {monitor_name}"
    elif event_type == "ssl_invalid":
        return f"[Raytech Monitor] SSL INVALID — {monitor_name}"
    elif event_type in ("ssl_renewed", "ssl_recovered"):
        return f"[Raytech Monitor] SSL RENEWED — {monitor_name}"
    elif event_type == "test_email":
        return f"[Raytech Monitor] TEST NOTIFICATION — {monitor_name}"
    return f"[Raytech Monitor] ALERT — {monitor_name}"


def render_email_content(
    event_type: str,
    monitor_name: str,
    status_label: str,
    cause_or_message: str,
    occurred_at: datetime,
    monitor_id: int | None = None,
) -> tuple[str, str]:
    """Generates both plain-text and HTML email content with dark/neutral Raytech aesthetic."""
    frontend_url = get_frontend_url()
    monitor_link = (
        f"{frontend_url}/monitors/{monitor_id}"
        if monitor_id is not None
        else frontend_url
    )
    settings_link = f"{frontend_url}/settings"

    occurred_str = occurred_at.strftime("%d %b %Y, %H:%M UTC")

    # Plain-text version
    text_content = f"""Raytech Monitor
========================================
EVENT: {status_label.upper()}
Monitor: {monitor_name}
Status: {status_label}
Occurred: {occurred_str}
Details: {cause_or_message}
----------------------------------------
View Monitor: {monitor_link}

Automated alert from Raytech Monitor.
Manage notification preferences: {settings_link}
"""

    # Escape HTML to prevent injection
    safe_monitor_name = html.escape(monitor_name)
    safe_status_label = html.escape(status_label)
    safe_cause = html.escape(cause_or_message)
    safe_occurred = html.escape(occurred_str)

    # Color accents based on severity
    is_down = "down" in event_type.lower() or "missed" in event_type.lower() or "invalid" in event_type.lower() or "expired" in event_type.lower()
    is_recovered = "resolved" in event_type.lower() or "recovered" in event_type.lower() or "renewed" in event_type.lower()
    badge_color = "#ef4444" if is_down else ("#22c55e" if is_recovered else "#eab308")
    badge_bg = "rgba(239, 68, 68, 0.12)" if is_down else ("rgba(34, 197, 94, 0.12)" if is_recovered else "rgba(234, 179, 8, 0.12)")

    # Minimal, clean dark HTML compatible with Gmail & Outlook
    html_content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>[Raytech Monitor] {safe_status_label} — {safe_monitor_name}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #0c0d0e; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #e6edf3; -webkit-font-smoothing: antialiased;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color: #0c0d0e; padding: 32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width: 540px; background-color: #15171a; border: 1px solid #262930; border-radius: 4px; overflow: hidden; text-align: left;">
          <!-- Header -->
          <tr>
            <td style="padding: 24px 28px; border-bottom: 1px solid #22262b;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <td>
                    <span style="font-size: 11px; font-weight: 700; letter-spacing: 0.12em; color: #8b949e; text-transform: uppercase;">RAYTECH MONITOR</span>
                    <div style="font-size: 19px; font-weight: 600; color: #f0f6fc; margin-top: 4px;">{safe_status_label}</div>
                  </td>
                  <td align="right" valign="middle">
                    <span style="display: inline-block; padding: 4px 10px; font-size: 11px; font-weight: 600; letter-spacing: 0.05em; color: {badge_color}; background-color: {badge_bg}; border: 1px solid {badge_color}; border-radius: 3px; text-transform: uppercase;">
                      {safe_status_label}
                    </span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Incident Details -->
          <tr>
            <td style="padding: 24px 28px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="font-size: 13px; line-height: 1.6;">
                <tr>
                  <td style="padding-bottom: 12px; color: #8b949e; width: 90px;" valign="top">Monitor</td>
                  <td style="padding-bottom: 12px; color: #f0f6fc; font-weight: 600;" valign="top">{safe_monitor_name}</td>
                </tr>
                <tr>
                  <td style="padding-bottom: 12px; color: #8b949e;" valign="top">Occurred</td>
                  <td style="padding-bottom: 12px; color: #c9d1d9;" valign="top">{safe_occurred}</td>
                </tr>
                <tr>
                  <td style="padding-bottom: 16px; color: #8b949e;" valign="top">Cause</td>
                  <td style="padding-bottom: 16px; color: #e6edf3; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; background-color: #0c0d0e; padding: 8px 12px; border-radius: 3px; border: 1px solid #22262b;" valign="top">
                    {safe_cause}
                  </td>
                </tr>
              </table>

              <!-- CTA Button -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top: 16px;">
                <tr>
                  <td>
                    <a href="{monitor_link}" target="_blank" style="display: inline-block; background-color: #21262d; color: #f0f6fc; font-size: 13px; font-weight: 500; text-decoration: none; padding: 10px 18px; border-radius: 3px; border: 1px solid #363b42;">
                      View Monitor &rarr;
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding: 16px 28px; background-color: #0f1113; border-top: 1px solid #22262b; font-size: 11px; color: #8b949e; line-height: 1.5;">
              Automated alert from Raytech Monitor.<br>
              <a href="{settings_link}" target="_blank" style="color: #58a6ff; text-decoration: none;">Manage notification preferences</a>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    return text_content, html_content


def enqueue_email_for_notification(
    db: Session,
    notification: Notification,
) -> EmailDelivery | None:
    """Enqueues an email delivery record for a notification, obeying user preferences and idempotency.

    Does NOT block or call Resend directly.
    """
    if not is_email_enabled():
        return None

    # Load user
    user = db.get(User, notification.user_id)
    if user is None or not user.email:
        return None

    # Load preferences
    pref = db.scalar(
        select(NotificationPreference).where(
            NotificationPreference.user_id == user.id
        )
    )
    if pref is None or not pref.email_enabled:
        return None

    if not is_event_enabled_for_user(pref, notification.event_type):
        return None

    # Check for existing delivery to prevent duplicates
    existing = db.scalar(
        select(EmailDelivery).where(
            EmailDelivery.notification_id == notification.id,
            EmailDelivery.recipient_email == user.email,
        )
    )
    if existing is not None:
        return existing

    # Find monitor name if monitor_id is attached
    monitor_name = "Monitor"
    if notification.monitor_id:
        mon = db.get(Monitor, notification.monitor_id)
        if mon:
            monitor_name = mon.name

    subject = build_email_subject(notification.event_type, monitor_name)

    # Status badge label
    if notification.event_type == "incident_opened":
        status_label = "DOWN"
    elif notification.event_type == "incident_resolved":
        status_label = "RECOVERED"
    elif notification.event_type == "heartbeat_missed":
        status_label = "HEARTBEAT MISSED"
    elif notification.event_type == "heartbeat_recovered":
        status_label = "HEARTBEAT RECOVERED"
    elif notification.event_type.startswith("ssl_expiring"):
        status_label = "SSL EXPIRING"
    elif notification.event_type == "ssl_expired":
        status_label = "SSL EXPIRED"
    elif notification.event_type == "ssl_invalid":
        status_label = "SSL INVALID"
    elif notification.event_type in ("ssl_renewed", "ssl_recovered"):
        status_label = "SSL RENEWED"
    else:
        status_label = "ALERT"

    text_body, html_body = render_email_content(
        event_type=notification.event_type,
        monitor_name=monitor_name,
        status_label=status_label,
        cause_or_message=notification.message,
        occurred_at=notification.created_at or utcnow(),
        monitor_id=notification.monitor_id,
    )

    delivery = EmailDelivery(
        notification_id=notification.id,
        user_id=user.id,
        recipient_email=user.email,
        provider="resend",
        event_type=notification.event_type,
        subject=subject,
        body_text=text_body,
        body_html=html_body,
        status="pending",
        attempt_count=0,
        next_attempt_at=utcnow(),
    )
    db.add(delivery)
    db.flush()

    return delivery


def enqueue_test_email(
    db: Session,
    user: User,
) -> EmailDelivery:
    """Enqueues a test email for the authenticated user."""
    subject = "[Raytech Monitor] TEST NOTIFICATION — System Test"
    text_body, html_body = render_email_content(
        event_type="test_email",
        monitor_name="Raytech Monitor Test",
        status_label="SYSTEM TEST",
        cause_or_message="This is a test notification confirming your email alerting configuration is functional.",
        occurred_at=utcnow(),
        monitor_id=None,
    )

    delivery = EmailDelivery(
        notification_id=None,
        user_id=user.id,
        recipient_email=user.email,
        provider="resend",
        event_type="test_email",
        subject=subject,
        body_text=text_body,
        body_html=html_body,
        status="pending",
        attempt_count=0,
        next_attempt_at=utcnow(),
    )
    db.add(delivery)
    db.flush()
    return delivery


def count_recent_sent_emails(
    db: Session,
    user_id: int | None = None,
    since: datetime | None = None,
) -> int:
    """Count sent emails in the last 24h for rate limit checks."""
    if since is None:
        since = utcnow() - timedelta(days=1)

    stmt = select(func.count(EmailDelivery.id)).where(
        EmailDelivery.status == "sent",
        EmailDelivery.created_at >= since,
    )
    if user_id is not None:
        stmt = stmt.where(EmailDelivery.user_id == user_id)

    return db.scalar(stmt) or 0


def send_resend_email_sync(
    delivery: EmailDelivery,
    api_key: str,
    base_url: str,
    from_address: str,
    reply_to: str | None = None,
    target_recipient: str | None = None,
) -> tuple[bool, int, str | None, str | None]:
    """Sends an email via Resend REST API synchronously.

    Returns: (is_success, status_code, provider_message_id, error_message)
    """
    to_email = target_recipient or delivery.recipient_email
    url = f"{base_url}/emails"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Idempotency-Key": f"raytech-delivery-{delivery.id}-{delivery.attempt_count}",
    }

    payload = {
        "from": from_address,
        "to": [to_email],
        "subject": delivery.subject,
        "text": delivery.body_text or "",
        "html": delivery.body_html or "",
    }
    if reply_to:
        payload["reply_to"] = reply_to

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, headers=headers, json=payload)

        status_code = resp.status_code
        if 200 <= status_code < 300:
            data = resp.json()
            message_id = data.get("id")
            return True, status_code, message_id, None
        else:
            try:
                err_data = resp.json()
                err_msg = err_data.get("message") or resp.text
            except Exception:
                err_msg = resp.text[:500]
            return False, status_code, None, err_msg
    except httpx.TimeoutException:
        return False, 408, None, "Request timed out connecting to Resend API"
    except Exception as e:
        return False, 503, None, f"Network/HTTP error: {str(e)[:500]}"


def process_pending_email_deliveries(db: Session | None = None) -> dict:
    """Outbox worker processor for pending and retry email deliveries.

    Safe, bounded, rate-limited, and isolated from monitoring state machines.
    """
    if not is_email_enabled():
        return {"processed": 0, "sent": 0, "failed": 0, "skipped": 0, "reason": "email_disabled"}

    api_key = get_resend_api_key()
    if not api_key:
        logger.warning("Email is enabled but RESEND_API_KEY is not configured.")
        return {"processed": 0, "sent": 0, "failed": 0, "skipped": 0, "reason": "missing_api_key"}

    close_session = False
    if db is None:
        db = SessionLocal()
        close_session = True

    try:
        now = utcnow()

        # 1. Recover stale processing rows (stuck > 5 minutes)
        stale_threshold = now - timedelta(minutes=5)
        stale_rows = db.scalars(
            select(EmailDelivery).where(
                EmailDelivery.status == "processing",
                EmailDelivery.updated_at < stale_threshold,
            )
        ).all()
        for stale in stale_rows:
            stale.status = "retry"
            stale.updated_at = now
        if stale_rows:
            db.commit()

        # 2. Select pending deliveries up to batch size
        batch_size = get_batch_size()
        query = (
            select(EmailDelivery)
            .where(
                EmailDelivery.status.in_(["pending", "retry"]),
                EmailDelivery.next_attempt_at <= now,
            )
            .order_by(EmailDelivery.id.asc())
            .limit(batch_size)
        )

        # Use row-level locking with skip_locked if supported (PostgreSQL)
        try:
            query = query.with_for_update(skip_locked=True)
            deliveries = db.scalars(query).all()
        except Exception:
            # Fallback for SQLite in tests
            db.rollback()
            query = (
                select(EmailDelivery)
                .where(
                    EmailDelivery.status.in_(["pending", "retry"]),
                    EmailDelivery.next_attempt_at <= now,
                )
                .order_by(EmailDelivery.id.asc())
                .limit(batch_size)
            )
            deliveries = db.scalars(query).all()

        if not deliveries:
            return {"processed": 0, "sent": 0, "failed": 0, "skipped": 0}

        # Mark as processing
        for d in deliveries:
            d.status = "processing"
            d.updated_at = now
        db.commit()

        # Rate limits
        daily_limit = get_daily_send_limit()
        per_user_limit = get_per_user_daily_limit()
        max_attempts = get_max_attempts()
        test_mode = is_test_mode()
        test_recipient = get_test_recipient()
        from_address = get_email_from()
        reply_to = get_email_reply_to()
        base_url = get_resend_api_base_url()

        sent_count = 0
        failed_count = 0
        skipped_count = 0

        for delivery in deliveries:
            # Refresh row state
            db.refresh(delivery)

            # Check global limit
            recent_global_sent = count_recent_sent_emails(db)
            if recent_global_sent >= daily_limit:
                delivery.status = "skipped"
                delivery.last_error_code = "QUOTA_EXCEEDED"
                delivery.last_error_message = f"Global daily sending limit reached ({daily_limit})"
                delivery.updated_at = utcnow()
                db.commit()
                skipped_count += 1
                continue

            # Check per-user limit
            recent_user_sent = count_recent_sent_emails(db, user_id=delivery.user_id)
            if recent_user_sent >= per_user_limit:
                delivery.status = "skipped"
                delivery.last_error_code = "USER_QUOTA_EXCEEDED"
                delivery.last_error_message = f"User daily sending limit reached ({per_user_limit})"
                delivery.updated_at = utcnow()
                db.commit()
                skipped_count += 1
                continue

            # Determine actual recipient
            actual_recipient = delivery.recipient_email
            if test_mode:
                if not test_recipient:
                    delivery.status = "failed"
                    delivery.last_error_code = "CONFIG_ERROR"
                    delivery.last_error_message = "EMAIL_TEST_MODE is enabled but EMAIL_TEST_RECIPIENT is not configured"
                    delivery.updated_at = utcnow()
                    db.commit()
                    failed_count += 1
                    continue
                actual_recipient = test_recipient
                logger.info(
                    "Test mode active: routing email for %s to test recipient %s",
                    delivery.recipient_email,
                    test_recipient,
                )

            # Perform send
            is_success, status_code, message_id, err_msg = send_resend_email_sync(
                delivery=delivery,
                api_key=api_key,
                base_url=base_url,
                from_address=from_address,
                reply_to=reply_to,
                target_recipient=actual_recipient,
            )

            if is_success:
                delivery.status = "sent"
                delivery.sent_at = utcnow()
                delivery.provider_message_id = message_id
                delivery.last_error_code = None
                delivery.last_error_message = None
                delivery.updated_at = utcnow()
                sent_count += 1
            else:
                delivery.attempt_count += 1
                delivery.last_error_code = str(status_code)
                delivery.last_error_message = (err_msg or "")[:1000]

                # Check if retryable (429 or 5xx or network errors)
                is_retryable = status_code in (429, 408, 500, 502, 503, 504)
                if is_retryable and delivery.attempt_count < max_attempts:
                    delivery.status = "retry"
                    # Exponential backoff with jitter: 2^attempt * 10s + jitter
                    jitter = random.uniform(1.0, 5.0)
                    backoff_seconds = (2 ** delivery.attempt_count) * 10 + jitter
                    delivery.next_attempt_at = utcnow() + timedelta(seconds=backoff_seconds)
                else:
                    delivery.status = "failed"
                    failed_count += 1

                delivery.updated_at = utcnow()

            db.commit()

        return {
            "processed": len(deliveries),
            "sent": sent_count,
            "failed": failed_count,
            "skipped": skipped_count,
        }

    finally:
        if close_session:
            db.close()
