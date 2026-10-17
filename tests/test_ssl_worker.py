import pytest
from datetime import datetime, timezone, timedelta
from app.models.monitor import Monitor
from app.models.notification import Notification
from app.models.ssl_check import SSLCheck
from app.models.ssl_notification import SSLNotificationLog
from app.services.ssl_checker import SSLCheckResult
from app.services.ssl_service import (
    evaluate_ssl_result_and_notify,
    get_due_ssl_monitor_ids,
    CHECK_INTERVAL_HOURS,
    RETRY_INTERVAL_MINUTES,
)


def test_renewal_detection(db, test_user):
    """When a certificate renews after an active warning/expiry, a renewal notification is emitted."""
    now = datetime.now(timezone.utc)
    old_expiry = now - timedelta(days=1)
    new_expiry = now + timedelta(days=90)

    # Monitor in 'expired' state
    monitor = Monitor(
        user_id=test_user.id,
        name="Renewal Test Monitor",
        url="https://renewal.raytech.cloud",
        ssl_monitoring_enabled=True,
        ssl_warning_days=14,
        ssl_status="expired",
        ssl_fingerprint_sha256="OLD_FINGERPRINT_111",
        ssl_expires_at=old_expiry,
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)

    result = SSLCheckResult(
        status="valid",
        hostname="renewal.raytech.cloud",
        port=443,
        issuer="Let's Encrypt",
        subject="renewal.raytech.cloud",
        valid_from=now - timedelta(days=1),
        expires_at=new_expiry,
        days_remaining=90,
        serial_number="987654321",
        fingerprint_sha256="NEW_FINGERPRINT_222",
    )

    evaluate_ssl_result_and_notify(db, monitor, result, now)
    db.commit()

    # Check notification emitted
    notifs = db.query(Notification).filter(Notification.monitor_id == monitor.id).all()
    assert len(notifs) == 1
    assert notifs[0].event_type == "ssl_renewed"
    assert notifs[0].title == "SSL Certificate Renewed"

    # Check SSLNotificationLog entry
    logs = db.query(SSLNotificationLog).filter(SSLNotificationLog.monitor_id == monitor.id).all()
    assert len(logs) == 1
    assert logs[0].milestone == "renewed"
    assert logs[0].cert_identifier == "NEW_FINGERPRINT_222"

    # Monitor updated
    assert monitor.ssl_status == "valid"
    assert monitor.ssl_fingerprint_sha256 == "NEW_FINGERPRINT_222"


def test_no_renewal_notification_on_early_rotation_without_warning(db, test_user):
    """Early certificate rotation when previously healthy should not trigger recovery notification."""
    now = datetime.now(timezone.utc)
    old_expiry = now + timedelta(days=60)
    new_expiry = now + timedelta(days=90)

    monitor = Monitor(
        user_id=test_user.id,
        name="Healthy Rotation",
        url="https://healthy.raytech.cloud",
        ssl_monitoring_enabled=True,
        ssl_warning_days=14,
        ssl_status="valid",  # Previously valid, no warning
        ssl_fingerprint_sha256="HEALTHY_FINGERPRINT_111",
        ssl_expires_at=old_expiry,
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)

    result = SSLCheckResult(
        status="valid",
        hostname="healthy.raytech.cloud",
        port=443,
        issuer="Let's Encrypt",
        subject="healthy.raytech.cloud",
        valid_from=now - timedelta(days=1),
        expires_at=new_expiry,
        days_remaining=90,
        serial_number="555555",
        fingerprint_sha256="NEW_HEALTHY_FINGERPRINT_222",
    )

    evaluate_ssl_result_and_notify(db, monitor, result, now)
    db.commit()

    # No renewal notification should be emitted
    notifs = db.query(Notification).filter(Notification.monitor_id == monitor.id).all()
    assert len(notifs) == 0


def test_milestone_notification_and_deduplication(db, test_user):
    """Milestone notification fires once and does not duplicate on subsequent worker checks."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=6)

    monitor = Monitor(
        user_id=test_user.id,
        name="Milestone Dedup Monitor",
        url="https://dedup.raytech.cloud",
        ssl_monitoring_enabled=True,
        ssl_warning_days=14,
        ssl_status="valid",
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)

    result = SSLCheckResult(
        status="expiring_soon",
        hostname="dedup.raytech.cloud",
        port=443,
        issuer="DigiCert",
        subject="dedup.raytech.cloud",
        valid_from=now - timedelta(days=84),
        expires_at=expires,
        days_remaining=6,
        serial_number="123456",
        fingerprint_sha256="DEDUP_CERT_SHA256",
    )

    # First run: should emit 7-day milestone notification
    evaluate_ssl_result_and_notify(db, monitor, result, now)
    db.commit()

    notifs = db.query(Notification).filter(Notification.monitor_id == monitor.id).all()
    assert len(notifs) == 1
    assert notifs[0].event_type == "ssl_expiring_7d"
    assert "expires in 6 days" in notifs[0].message

    # Second run immediately afterward with same certificate: should NOT duplicate
    evaluate_ssl_result_and_notify(db, monitor, result, now + timedelta(hours=1))
    db.commit()

    notifs_after = db.query(Notification).filter(Notification.monitor_id == monitor.id).all()
    assert len(notifs_after) == 1  # Still 1, deduplicated

    # Third run when certificate drops to 2 days remaining (3-day milestone)
    result_3d = SSLCheckResult(
        status="expiring_soon",
        hostname="dedup.raytech.cloud",
        port=443,
        issuer="DigiCert",
        subject="dedup.raytech.cloud",
        valid_from=now - timedelta(days=88),
        expires_at=now + timedelta(days=2),
        days_remaining=2,
        serial_number="123456",
        fingerprint_sha256="DEDUP_CERT_SHA256",
    )
    evaluate_ssl_result_and_notify(db, monitor, result_3d, now + timedelta(days=4))
    db.commit()

    notifs_3d = db.query(Notification).filter(Notification.monitor_id == monitor.id).all()
    assert len(notifs_3d) == 2
    assert any(n.event_type == "ssl_expiring_3d" for n in notifs_3d)


def test_warning_threshold_filtering(db, test_user):
    """Notifications are suppressed if remaining days are above user's warning threshold."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=10)

    # User configured 7-day threshold, but cert has 10 days left
    monitor = Monitor(
        user_id=test_user.id,
        name="Threshold Filtering",
        url="https://threshold.raytech.cloud",
        ssl_monitoring_enabled=True,
        ssl_warning_days=7,
        ssl_status="valid",
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)

    result = SSLCheckResult(
        status="valid",
        hostname="threshold.raytech.cloud",
        port=443,
        issuer="Let's Encrypt",
        subject="threshold.raytech.cloud",
        valid_from=now - timedelta(days=80),
        expires_at=expires,
        days_remaining=10,
        serial_number="123456",
        fingerprint_sha256="THRESHOLD_CERT_SHA256",
    )

    evaluate_ssl_result_and_notify(db, monitor, result, now)
    db.commit()

    notifs = db.query(Notification).filter(Notification.monitor_id == monitor.id).all()
    assert len(notifs) == 0


def test_worker_retry_on_transient_failure(db, test_user):
    """Worker retries after 30 minutes on transient check failure without creating false notifications."""
    now = datetime.now(timezone.utc)

    monitor = Monitor(
        user_id=test_user.id,
        name="Transient Failure Test",
        url="https://flaky.raytech.cloud",
        ssl_monitoring_enabled=True,
        ssl_warning_days=14,
        ssl_status="valid",
    )
    db.add(monitor)
    db.commit()
    db.refresh(monitor)

    # Transient DNS failure
    result = SSLCheckResult(
        status="check_failed",
        hostname="flaky.raytech.cloud",
        port=443,
        error_type="dns_resolution_failed",
        error_message="DNS name not resolved",
    )

    evaluate_ssl_result_and_notify(db, monitor, result, now)
    db.commit()

    # Verify no false invalid or expired notification was emitted
    notifs = db.query(Notification).filter(Notification.monitor_id == monitor.id).all()
    assert len(notifs) == 0

    # Verify status is check_failed and scheduled for 30m retry
    assert monitor.ssl_status == "check_failed"
    expected_retry = now + timedelta(minutes=RETRY_INTERVAL_MINUTES)
    assert monitor.ssl_next_check_at is not None
    sched = monitor.ssl_next_check_at
    if sched.tzinfo is None:
        sched = sched.replace(tzinfo=timezone.utc)
    assert abs((sched - expected_retry).total_seconds()) < 5

    # Verify check history was recorded
    history = db.query(SSLCheck).filter(SSLCheck.monitor_id == monitor.id).all()
    assert len(history) == 1
    assert history[0].status == "check_failed"
    assert history[0].error_type == "dns_resolution_failed"


def test_get_due_ssl_monitors_filtering(db, test_user):
    """get_due_ssl_monitor_ids only returns active, SSL-enabled monitors due for checking."""
    now = datetime.now(timezone.utc)

    # 1. Due monitor (ssl_next_check_at in past)
    m1 = Monitor(
        user_id=test_user.id,
        name="Due 1",
        url="https://due1.com",
        is_active=True,
        ssl_monitoring_enabled=True,
        ssl_next_check_at=now - timedelta(minutes=10),
    )
    # 2. Due monitor (ssl_next_check_at is None)
    m2 = Monitor(
        user_id=test_user.id,
        name="Due 2",
        url="https://due2.com",
        is_active=True,
        ssl_monitoring_enabled=True,
        ssl_next_check_at=None,
    )
    # 3. Not due (ssl_next_check_at in future)
    m3 = Monitor(
        user_id=test_user.id,
        name="Future",
        url="https://future.com",
        is_active=True,
        ssl_monitoring_enabled=True,
        ssl_next_check_at=now + timedelta(hours=6),
    )
    # 4. Inactive monitor
    m4 = Monitor(
        user_id=test_user.id,
        name="Paused",
        url="https://paused.com",
        is_active=False,
        ssl_monitoring_enabled=True,
        ssl_next_check_at=now - timedelta(hours=1),
    )
    # 5. SSL disabled monitor
    m5 = Monitor(
        user_id=test_user.id,
        name="SSL Disabled",
        url="https://disabled.com",
        is_active=True,
        ssl_monitoring_enabled=False,
        ssl_next_check_at=now - timedelta(hours=1),
    )
    db.add_all([m1, m2, m3, m4, m5])
    db.commit()

    due_ids = get_due_ssl_monitor_ids(db)
    assert m1.id in due_ids
    assert m2.id in due_ids
    assert m3.id not in due_ids
    assert m4.id not in due_ids
    assert m5.id not in due_ids
