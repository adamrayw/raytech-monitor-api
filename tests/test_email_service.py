import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import httpx
import pytest
from sqlalchemy import select

from app.models.email_delivery import EmailDelivery
from app.models.monitor import Monitor
from app.models.notification import Notification
from app.models.notification_preference import NotificationPreference
from app.models.user import User
from app.services.email_service import (
    enqueue_email_for_notification,
    enqueue_test_email,
    process_pending_email_deliveries,
    render_email_content,
    build_email_subject,
)


def utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture
def test_monitor(db, test_user):
    mon = Monitor(
        user_id=test_user.id,
        name="Production Web App",
        url="https://app.raytech.cloud",
        type="http",
        interval_seconds=60,
        timeout_seconds=5,
        status="operational",
        is_active=True,
    )
    db.add(mon)
    db.commit()
    db.refresh(mon)
    return mon


@pytest.fixture
def enabled_preferences(db, test_user):
    pref = NotificationPreference(
        user_id=test_user.id,
        email_enabled=True,
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


def test_email_disabled_globally(db, test_user, test_monitor, enabled_preferences, monkeypatch):
    monkeypatch.setenv("EMAIL_ENABLED", "false")

    notif = Notification(
        user_id=test_user.id,
        monitor_id=test_monitor.id,
        incident_id=1,
        event_type="incident_opened",
        title="Production Web App is DOWN",
        message="HTTP 500 Internal Server Error",
    )
    db.add(notif)
    db.commit()

    delivery = enqueue_email_for_notification(db, notif)
    assert delivery is None

    deliveries = db.scalars(select(EmailDelivery)).all()
    assert len(deliveries) == 0


def test_user_email_alerts_disabled(db, test_user, test_monitor, monkeypatch):
    monkeypatch.setenv("EMAIL_ENABLED", "true")

    # Preference with email_enabled=False
    pref = NotificationPreference(
        user_id=test_user.id,
        email_enabled=False,
    )
    db.add(pref)
    db.commit()

    notif = Notification(
        user_id=test_user.id,
        monitor_id=test_monitor.id,
        incident_id=1,
        event_type="incident_opened",
        title="Production Web App is DOWN",
        message="HTTP 500",
    )
    db.add(notif)
    db.commit()

    delivery = enqueue_email_for_notification(db, notif)
    assert delivery is None


def test_http_down_and_recovered_delivery(db, test_user, test_monitor, enabled_preferences, monkeypatch):
    monkeypatch.setenv("EMAIL_ENABLED", "true")

    # 1. Down notification
    down_notif = Notification(
        user_id=test_user.id,
        monitor_id=test_monitor.id,
        incident_id=10,
        event_type="incident_opened",
        title="Production Web App is DOWN",
        message="Connection refused",
    )
    db.add(down_notif)
    db.commit()

    d1 = enqueue_email_for_notification(db, down_notif)
    assert d1 is not None
    assert d1.event_type == "incident_opened"
    assert d1.recipient_email == test_user.email
    assert d1.status == "pending"
    assert "[Raytech Monitor] DOWN — Production Web App" in d1.subject

    # 2. Recovered notification
    rec_notif = Notification(
        user_id=test_user.id,
        monitor_id=test_monitor.id,
        incident_id=10,
        event_type="incident_resolved",
        title="Production Web App is back online",
        message="Service recovered after 2m 15s",
    )
    db.add(rec_notif)
    db.commit()

    d2 = enqueue_email_for_notification(db, rec_notif)
    assert d2 is not None
    assert d2.event_type == "incident_resolved"
    assert d2.status == "pending"
    assert "[Raytech Monitor] RECOVERED — Production Web App" in d2.subject


def test_heartbeat_and_ssl_events(db, test_user, test_monitor, enabled_preferences, monkeypatch):
    monkeypatch.setenv("EMAIL_ENABLED", "true")

    # Heartbeat missed
    hb_missed = Notification(
        user_id=test_user.id,
        monitor_id=test_monitor.id,
        incident_id=20,
        event_type="heartbeat_missed",
        title="Production Web App heartbeat missed",
        message="No ping received",
    )
    db.add(hb_missed)
    db.commit()
    d_hb = enqueue_email_for_notification(db, hb_missed)
    assert d_hb is not None
    assert "[Raytech Monitor] HEARTBEAT MISSED" in d_hb.subject

    # SSL expiring
    ssl_notif = Notification(
        user_id=test_user.id,
        monitor_id=test_monitor.id,
        incident_id=None,
        event_type="ssl_expiring_14d",
        title="SSL Certificate Expiring",
        message="Expires in 14 days",
    )
    db.add(ssl_notif)
    db.commit()
    d_ssl = enqueue_email_for_notification(db, ssl_notif)
    assert d_ssl is not None
    assert "[Raytech Monitor] SSL EXPIRING" in d_ssl.subject


def test_duplicate_notification_idempotency(db, test_user, test_monitor, enabled_preferences, monkeypatch):
    monkeypatch.setenv("EMAIL_ENABLED", "true")

    notif = Notification(
        user_id=test_user.id,
        monitor_id=test_monitor.id,
        incident_id=30,
        event_type="incident_opened",
        title="Production Web App is DOWN",
        message="Timeout",
    )
    db.add(notif)
    db.commit()

    d1 = enqueue_email_for_notification(db, notif)
    db.commit()
    d2 = enqueue_email_for_notification(db, notif)
    db.commit()

    assert d1.id == d2.id
    deliveries = db.scalars(
        select(EmailDelivery).where(EmailDelivery.notification_id == notif.id)
    ).all()
    assert len(deliveries) == 1


def test_html_escaping_security():
    malicious_name = '<script>alert("hacked")</script> & "API"'
    malicious_cause = '<img src=x onerror=alert(1)> Connection failed <bad>'

    text, html_out = render_email_content(
        event_type="incident_opened",
        monitor_name=malicious_name,
        status_label="DOWN",
        cause_or_message=malicious_cause,
        occurred_at=utcnow(),
        monitor_id=1,
    )

    assert "<script>" not in html_out
    assert "&lt;script&gt;alert(&quot;hacked&quot;)&lt;/script&gt;" in html_out
    assert "<img src=x onerror=alert(1)>" not in html_out
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_out


def test_worker_processing_success(db, test_user, monkeypatch):
    monkeypatch.setenv("EMAIL_ENABLED", "true")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_123456789")
    monkeypatch.setenv("EMAIL_TEST_MODE", "true")
    monkeypatch.setenv("EMAIL_TEST_RECIPIENT", "alerts-test@raytech.cloud")

    delivery = enqueue_test_email(db, test_user)
    db.commit()

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"id": "resend_msg_abc123"}

    with patch("httpx.Client.post", return_value=fake_response) as mock_post:
        res = process_pending_email_deliveries(db)
        assert res["sent"] == 1
        assert res["failed"] == 0

        # Verify call payload routed to test recipient
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["json"]["to"] == ["alerts-test@raytech.cloud"]

    db.refresh(delivery)
    assert delivery.status == "sent"
    assert delivery.provider_message_id == "resend_msg_abc123"
    assert delivery.sent_at is not None


def test_worker_processing_retry_on_429(db, test_user, monkeypatch):
    monkeypatch.setenv("EMAIL_ENABLED", "true")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_123456789")
    monkeypatch.setenv("EMAIL_TEST_MODE", "false")

    delivery = enqueue_test_email(db, test_user)
    db.commit()

    fake_response = MagicMock()
    fake_response.status_code = 429
    fake_response.json.return_value = {"message": "Rate limit exceeded"}

    with patch("httpx.Client.post", return_value=fake_response):
        res = process_pending_email_deliveries(db)
        assert res["failed"] == 0

    db.refresh(delivery)
    assert delivery.status == "retry"
    assert delivery.attempt_count == 1
    assert delivery.last_error_code == "429"
    assert "Rate limit exceeded" in delivery.last_error_message


def test_worker_processing_permanent_failure_401(db, test_user, monkeypatch):
    monkeypatch.setenv("EMAIL_ENABLED", "true")
    monkeypatch.setenv("RESEND_API_KEY", "re_invalid")
    monkeypatch.setenv("EMAIL_TEST_MODE", "false")

    delivery = enqueue_test_email(db, test_user)
    db.commit()

    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.json.return_value = {"message": "Invalid API key"}

    with patch("httpx.Client.post", return_value=fake_response):
        res = process_pending_email_deliveries(db)
        assert res["failed"] == 1

    db.refresh(delivery)
    assert delivery.status == "failed"
    assert delivery.attempt_count == 1
    assert delivery.last_error_code == "401"


def test_quota_limits(db, test_user, monkeypatch):
    monkeypatch.setenv("EMAIL_ENABLED", "true")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_123")
    monkeypatch.setenv("EMAIL_TEST_MODE", "false")
    monkeypatch.setenv("EMAIL_DAILY_SEND_LIMIT", "2")

    # Create 2 already sent deliveries
    for i in range(2):
        d = EmailDelivery(
            user_id=test_user.id,
            recipient_email=test_user.email,
            event_type="test_email",
            subject=f"Sent {i}",
            status="sent",
            sent_at=utcnow(),
        )
        db.add(d)
    db.commit()

    # Now enqueue a pending one
    pending = enqueue_test_email(db, test_user)
    db.commit()

    res = process_pending_email_deliveries(db)
    assert res["skipped"] == 1

    db.refresh(pending)
    assert pending.status == "skipped"
    assert "limit reached" in pending.last_error_message
