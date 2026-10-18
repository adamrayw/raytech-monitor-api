from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select

from app.models.monitor import Monitor
from app.models.status_page import StatusPage
from app.models.subscription import UserSubscription
from app.models.subscription_audit import SubscriptionAuditLog
from app.models.email_delivery import EmailDelivery
from app.models.user import User
from app.services.subscription_service import (
    activate_pro_from_payment,
    ensure_utc,
    get_effective_plan,
    get_eligible_resource_ids,
    get_or_create_subscription,
    get_subscription_usage,
    process_subscription_expirations,
    process_subscription_reminders,
    utcnow,
)
from app.services.monitor_runner import get_due_monitor_ids
from app.services.heartbeat_service import get_overdue_heartbeat_monitor_ids
from app.services.ssl_service import get_due_ssl_monitor_ids
from app.services.email_service import (
    WIB,
    enqueue_subscription_expired_email,
    enqueue_subscription_reminder_email,
    format_wib_datetime,
)


@pytest.fixture
def non_legacy_user(db):
    user = User(
        email="lifecycle_user@raytech.cloud",
        name="Lifecycle User",
        google_sub="google-lifecycle-sub-1",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    sub = UserSubscription(
        user_id=user.id,
        plan="free",
        status="active",
        legacy_override=False,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return user


def test_free_to_pro_activation(db, non_legacy_user):
    """Case A: Free -> Pro starts 30-day Pro period from now."""
    before = utcnow()
    sub = activate_pro_from_payment(db, non_legacy_user.id, transaction_id=101, duration_days=30, commit=True)
    after = utcnow()

    assert sub.plan == "pro"
    assert sub.status == "active"
    assert sub.current_period_start is not None
    assert sub.current_period_end is not None

    end_utc = ensure_utc(sub.current_period_end)
    expected_end = before + timedelta(days=30)
    assert end_utc >= expected_end - timedelta(seconds=5)
    assert end_utc <= after + timedelta(days=30) + timedelta(seconds=5)

    # Check audit log
    audit = db.execute(
        select(SubscriptionAuditLog).where(
            SubscriptionAuditLog.user_id == non_legacy_user.id,
            SubscriptionAuditLog.event_type == "pro_activated",
        )
    ).scalar_one_or_none()
    assert audit is not None
    assert audit.new_plan == "pro"
    assert audit.previous_plan == "free"
    assert audit.payment_transaction_id == 101


def test_active_pro_extension_preserves_remaining_time(db, non_legacy_user):
    """Case B: Active Pro extended by 30 days extends from previous current_period_end without losing remaining time."""
    now = utcnow()
    sub = get_or_create_subscription(db, non_legacy_user.id)
    sub.plan = "pro"
    sub.status = "active"
    initial_end = now + timedelta(days=15)
    sub.current_period_start = now - timedelta(days=15)
    sub.current_period_end = initial_end
    db.commit()

    # User extends Pro
    sub = activate_pro_from_payment(db, non_legacy_user.id, transaction_id=102, duration_days=30, commit=True)

    expected_end = initial_end + timedelta(days=30)
    actual_end = ensure_utc(sub.current_period_end)
    assert abs((actual_end - expected_end).total_seconds()) < 5

    # Check audit log
    audit = db.execute(
        select(SubscriptionAuditLog).where(
            SubscriptionAuditLog.user_id == non_legacy_user.id,
            SubscriptionAuditLog.payment_transaction_id == 102,
        )
    ).scalar_one_or_none()
    assert audit is not None
    assert audit.event_type == "pro_extended"
    assert audit.previous_plan == "pro"
    assert audit.new_plan == "pro"


def test_expired_pro_renewal_starts_from_now(db, non_legacy_user):
    """Case C: Expired Pro renewal starts from payment activation time, not from old expiration date."""
    now = utcnow()
    sub = get_or_create_subscription(db, non_legacy_user.id)
    sub.plan = "pro"
    sub.status = "expired"
    old_end = now - timedelta(days=10)
    sub.current_period_start = now - timedelta(days=40)
    sub.current_period_end = old_end
    db.commit()

    sub = activate_pro_from_payment(db, non_legacy_user.id, transaction_id=103, duration_days=30, commit=True)

    actual_end = ensure_utc(sub.current_period_end)
    expected_min = now + timedelta(days=30) - timedelta(seconds=5)
    assert actual_end >= expected_min
    assert actual_end > old_end + timedelta(days=30)  # Must NOT start from old_end!


def test_automatic_expiration_worker_job(db, non_legacy_user):
    """Phase 3: Periodic expiration job downgrades expired Pro, records audit, enqueues email."""
    now = utcnow()
    sub = get_or_create_subscription(db, non_legacy_user.id)
    sub.plan = "pro"
    sub.status = "active"
    sub.current_period_start = now - timedelta(days=31)
    sub.current_period_end = now - timedelta(hours=1)
    db.commit()

    res = process_subscription_expirations(db)
    assert res["expired"] == 1

    db.refresh(sub)
    assert sub.plan == "free"
    assert sub.status == "expired"

    # Verify audit event
    audit = db.execute(
        select(SubscriptionAuditLog).where(
            SubscriptionAuditLog.user_id == non_legacy_user.id,
            SubscriptionAuditLog.event_type == "pro_expired",
        )
    ).scalar_one_or_none()
    assert audit is not None
    assert audit.previous_plan == "pro"
    assert audit.new_plan == "free"

    # Verify expiration email enqueued
    outbox = db.execute(
        select(EmailDelivery).where(
            EmailDelivery.user_id == non_legacy_user.id,
            EmailDelivery.event_type == "sub_expired",
        )
    ).scalar_one_or_none()
    assert outbox is not None
    assert "has expired" in outbox.subject.lower()


def test_no_downgrade_before_expiration(db, non_legacy_user):
    """Active Pro subscriptions with future period end are NOT downgraded."""
    now = utcnow()
    sub = get_or_create_subscription(db, non_legacy_user.id)
    sub.plan = "pro"
    sub.status = "active"
    sub.current_period_start = now
    sub.current_period_end = now + timedelta(days=10)
    db.commit()

    res = process_subscription_expirations(db)
    assert res["expired"] == 0

    db.refresh(sub)
    assert sub.plan == "pro"
    assert sub.status == "active"


def test_fail_safe_effective_plan_downgrades_when_worker_delayed(db, non_legacy_user):
    """API effective plan calculation enforces Free immediately when current_period_end is in past."""
    now = utcnow()
    sub = get_or_create_subscription(db, non_legacy_user.id)
    sub.plan = "pro"
    sub.status = "active"
    sub.current_period_start = now - timedelta(days=35)
    sub.current_period_end = now - timedelta(days=5)  # 5 days past due
    db.commit()

    plan_id, fresh_sub = get_effective_plan(db, non_legacy_user.id)
    assert plan_id == "free"
    assert fresh_sub.plan == "free"
    assert fresh_sub.status == "expired"


def test_legacy_override_protected_from_expiration(db, test_user):
    """Grandfathered legacy_override user is NEVER downgraded by expiration job or get_effective_plan."""
    sub = get_or_create_subscription(db, test_user.id)
    sub.plan = "pro"
    sub.status = "active"
    sub.legacy_override = True
    sub.current_period_end = utcnow() - timedelta(days=100)
    db.commit()

    res = process_subscription_expirations(db)
    assert res["expired"] == 0

    plan_id, fresh_sub = get_effective_plan(db, test_user.id)
    assert plan_id == "pro"
    assert fresh_sub.legacy_override is True


def test_resource_preservation_and_deterministic_restriction(db, non_legacy_user):
    """Downgrade retains all resources; deterministic order picks first 3 HTTP, 1 Heartbeat, 1 Status Page."""
    # Create 5 HTTP monitors with distinct created_at
    base_time = utcnow() - timedelta(days=10)
    monitors = []
    for i in range(5):
        m = Monitor(
            user_id=non_legacy_user.id,
            name=f"HTTP Monitor {i+1}",
            type="http",
            url=f"https://example{i+1}.com",
            interval_seconds=300,
            status="operational",
            is_active=True,
            created_at=base_time + timedelta(hours=i),
        )
        db.add(m)
        monitors.append(m)

    # Create 2 Heartbeat monitors
    hb1 = Monitor(
        user_id=non_legacy_user.id,
        name="HB 1",
        type="heartbeat",
        interval_seconds=300,
        status="operational",
        is_active=True,
        created_at=base_time,
    )
    hb2 = Monitor(
        user_id=non_legacy_user.id,
        name="HB 2",
        type="heartbeat",
        interval_seconds=300,
        status="operational",
        is_active=True,
        created_at=base_time + timedelta(hours=1),
    )
    db.add(hb1)
    db.add(hb2)

    # Create 2 Status Pages
    sp1 = StatusPage(
        user_id=non_legacy_user.id,
        name="Page 1",
        slug="page-1",
        is_published=True,
        created_at=base_time,
    )
    sp2 = StatusPage(
        user_id=non_legacy_user.id,
        name="Page 2",
        slug="page-2",
        is_published=True,
        created_at=base_time + timedelta(hours=1),
    )
    db.add(sp1)
    db.add(sp2)
    db.commit()

    # User is currently on Free
    eligible_http = get_eligible_resource_ids(db, non_legacy_user.id, "http")
    assert len(eligible_http) == 3
    # Must be first 3 created
    assert monitors[0].id in eligible_http
    assert monitors[1].id in eligible_http
    assert monitors[2].id in eligible_http
    assert monitors[3].id not in eligible_http
    assert monitors[4].id not in eligible_http

    eligible_hb = get_eligible_resource_ids(db, non_legacy_user.id, "heartbeat")
    assert len(eligible_hb) == 1
    assert hb1.id in eligible_hb
    assert hb2.id not in eligible_hb

    eligible_sp = get_eligible_resource_ids(db, non_legacy_user.id, "status_page")
    assert len(eligible_sp) == 1
    assert sp1.id in eligible_sp
    assert sp2.id not in eligible_sp

    # Check usage stats
    usage = get_subscription_usage(db, non_legacy_user.id)
    assert usage["http_monitors"]["used"] == 5
    assert usage["http_monitors"]["active_count"] == 3
    assert usage["http_monitors"]["restricted_count"] == 2

    assert usage["heartbeat_monitors"]["used"] == 2
    assert usage["heartbeat_monitors"]["active_count"] == 1
    assert usage["heartbeat_monitors"]["restricted_count"] == 1


def test_restricted_monitors_not_scheduled_for_checks(db, non_legacy_user):
    """Monitor runner and heartbeat worker never schedule restricted monitor IDs."""
    now = utcnow()
    m1 = Monitor(
        user_id=non_legacy_user.id,
        name="Eligible HTTP",
        type="http",
        url="https://eligible.com",
        interval_seconds=300,
        status="operational",
        is_active=True,
        next_check_at=now - timedelta(minutes=1),
        created_at=now - timedelta(days=2),
    )
    m2 = Monitor(
        user_id=non_legacy_user.id,
        name="Eligible HTTP 2",
        type="http",
        url="https://eligible2.com",
        interval_seconds=300,
        status="operational",
        is_active=True,
        next_check_at=now - timedelta(minutes=1),
        created_at=now - timedelta(days=2, hours=1),
    )
    m3 = Monitor(
        user_id=non_legacy_user.id,
        name="Eligible HTTP 3",
        type="http",
        url="https://eligible3.com",
        interval_seconds=300,
        status="operational",
        is_active=True,
        next_check_at=now - timedelta(minutes=1),
        created_at=now - timedelta(days=2, hours=2),
    )
    m4 = Monitor(
        user_id=non_legacy_user.id,
        name="Restricted HTTP",
        type="http",
        url="https://restricted.com",
        interval_seconds=300,
        status="operational",
        is_active=True,
        next_check_at=now - timedelta(minutes=1),
        created_at=now - timedelta(days=1),  # 4th oldest -> restricted on Free
    )
    db.add_all([m1, m2, m3, m4])
    db.commit()

    due_ids = get_due_monitor_ids(db)
    assert m1.id in due_ids
    assert m2.id in due_ids
    assert m3.id in due_ids
    assert m4.id not in due_ids  # Restricted monitor is excluded from runner!


def test_manually_paused_monitors_remain_paused_on_reactivation(db, non_legacy_user):
    """Reactivating Pro restores all monitors to eligibility without unpausing user-paused monitors."""
    m_active = Monitor(
        user_id=non_legacy_user.id,
        name="Active Monitor",
        type="http",
        url="https://active.com",
        interval_seconds=300,
        status="operational",
        is_active=True,
        created_at=utcnow() - timedelta(days=5),
    )
    m_paused = Monitor(
        user_id=non_legacy_user.id,
        name="User Paused Monitor",
        type="http",
        url="https://paused.com",
        interval_seconds=300,
        status="paused",
        is_active=False,
        created_at=utcnow() - timedelta(days=4),
    )
    db.add_all([m_active, m_paused])
    db.commit()

    # User reactivates Pro
    activate_pro_from_payment(db, non_legacy_user.id, transaction_id=105, duration_days=30, commit=True)

    db.refresh(m_active)
    db.refresh(m_paused)

    assert m_active.is_active is True
    assert m_paused.is_active is False
    assert m_paused.status == "paused"


def test_expiration_reminders_7d_3d_1d(db, non_legacy_user):
    """Reminders are enqueued at 7d, 3d, 1d thresholds with durable idempotency keys and WIB dates."""
    now = utcnow()
    sub = get_or_create_subscription(db, non_legacy_user.id)
    sub.plan = "pro"
    sub.status = "active"

    # Set expiration to 6.5 days from now (within 7d reminder window)
    target_end = now + timedelta(days=6, hours=12)
    sub.current_period_end = target_end
    db.commit()

    res = process_subscription_reminders(db)
    assert res["reminders_queued"] == 1

    outbox = db.execute(
        select(EmailDelivery).where(
            EmailDelivery.user_id == non_legacy_user.id,
            EmailDelivery.event_type == "sub_reminder_7d",
        )
    ).scalar_one_or_none()
    assert outbox is not None
    assert "expires in 7 days" in outbox.subject.lower()
    assert "WIB" in outbox.body_html

    # Duplicate run does not send again
    res_again = process_subscription_reminders(db)
    assert res_again["reminders_queued"] == 0

    count = db.scalar(
        select(EmailDelivery).where(
            EmailDelivery.user_id == non_legacy_user.id,
            EmailDelivery.event_type == "sub_reminder_7d",
        )
    )
    assert count is not None


def test_no_stale_reminders_after_extension(db, non_legacy_user):
    """Extending Pro moves expiration date, preventing stale reminders for the old expiration date."""
    now = utcnow()
    sub = get_or_create_subscription(db, non_legacy_user.id)
    sub.plan = "pro"
    sub.status = "active"
    sub.current_period_end = now + timedelta(days=1)  # 1 day remaining
    db.commit()

    # User extends Pro by 30 days before reminder job runs
    activate_pro_from_payment(db, non_legacy_user.id, transaction_id=106, duration_days=30, commit=True)

    # Now expiration is 31 days in future; reminder job must send 0 emails
    res = process_subscription_reminders(db)
    assert res["reminders_queued"] == 0


def test_wib_datetime_formatting():
    """WIB formatting uses Asia/Jakarta timezone (UTC+7) accurately."""
    # 2026-10-18 12:00:00 UTC -> 2026-10-18 19:00:00 WIB
    dt_utc = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
    formatted = format_wib_datetime(dt_utc)
    assert "18 Oct 2026" in formatted
    assert "19:00 WIB" in formatted


def test_public_status_page_restricted_returns_404(db, non_legacy_user, client):
    """When on Free, any status page beyond the limit of 1 returns 404 on the public route."""
    sp_active = StatusPage(
        user_id=non_legacy_user.id,
        name="Active Public Page",
        slug="active-page-public",
        is_published=True,
        created_at=utcnow() - timedelta(days=2),
    )
    sp_restricted = StatusPage(
        user_id=non_legacy_user.id,
        name="Restricted Public Page",
        slug="restricted-page-public",
        is_published=True,
        created_at=utcnow() - timedelta(days=1),
    )
    db.add_all([sp_active, sp_restricted])
    db.commit()

    # Active page is accessible
    res_active = client.get("/public/status/active-page-public")
    assert res_active.status_code == 200

    # Restricted page is not accessible (404)
    res_restricted = client.get("/public/status/restricted-page-public")
    assert res_restricted.status_code == 404


def test_ssl_checks_skipped_for_free_and_restricted(db, non_legacy_user):
    """SSL worker candidate selection excludes monitors of Free users (ssl_monitoring is False)."""
    now = utcnow()
    m = Monitor(
        user_id=non_legacy_user.id,
        name="SSL Monitor",
        type="http",
        url="https://ssl-check.com",
        interval_seconds=300,
        status="operational",
        is_active=True,
        ssl_monitoring_enabled=True,
        ssl_next_check_at=now - timedelta(minutes=1),
        created_at=now - timedelta(days=1),
    )
    db.add(m)
    db.commit()

    ssl_due = get_due_ssl_monitor_ids(db)
    assert m.id not in ssl_due  # Free user -> SSL checks are skipped!

    # Upgrade to Pro -> monitor is now eligible for SSL check
    activate_pro_from_payment(db, non_legacy_user.id, transaction_id=110, duration_days=30, commit=True)
    ssl_due_pro = get_due_ssl_monitor_ids(db)
    assert m.id in ssl_due_pro


def test_subscription_api_wib_and_remaining_time(db, non_legacy_user, client):
    """GET /subscription returns accurate current_period_end_wib, remaining_seconds, and auto_renew=False."""
    from app.dependencies.auth import get_current_user
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: non_legacy_user

    now = utcnow()
    sub = get_or_create_subscription(db, non_legacy_user.id)
    sub.plan = "pro"
    sub.status = "active"
    sub.current_period_start = now
    sub.current_period_end = now + timedelta(days=5, hours=2)
    db.commit()

    res = client.get("/subscription")
    assert res.status_code == 200
    data = res.json()
    assert data["plan"] == "pro"
    assert data["auto_renew"] is False
    assert "WIB" in (data.get("current_period_end_wib") or "")
    assert data["remaining_seconds"] is not None
    assert data["remaining_seconds"] > 4 * 86400  # More than 4 days


def test_expiration_confirmation_email_idempotent(db, non_legacy_user):
    """enqueue_subscription_expired_email enqueues exactly once per expiration timestamp."""
    now = utcnow()
    sub = get_or_create_subscription(db, non_legacy_user.id)
    sub.plan = "pro"
    sub.status = "expired"
    sub.current_period_end = now - timedelta(hours=2)
    db.commit()

    d1 = enqueue_subscription_expired_email(db, non_legacy_user, sub)
    assert d1 is not None

    d2 = enqueue_subscription_expired_email(db, non_legacy_user, sub)
    assert d2 is None  # Second attempt returns None due to idempotency key!


def test_dispatch_time_stale_reminder_canceled_on_extension(db, non_legacy_user):
    """If user extends Pro while a reminder is queued in email_deliveries, dispatcher skips it."""
    import os
    from unittest.mock import patch
    from app.services.email_service import process_pending_email_deliveries

    now = utcnow()
    sub = get_or_create_subscription(db, non_legacy_user.id)
    sub.plan = "pro"
    sub.status = "active"
    sub.current_period_end = now + timedelta(days=6, hours=12)
    db.commit()

    # Queue 7-day reminder
    delivery = enqueue_subscription_reminder_email(db, non_legacy_user, sub, threshold_days=7)
    assert delivery is not None
    db.commit()

    # User now renews/extends before email dispatcher executes
    activate_pro_from_payment(db, non_legacy_user.id, transaction_id=999, duration_days=30, commit=True)

    # Dispatcher runs
    with patch.dict(os.environ, {"EMAIL_ENABLED": "true", "RESEND_API_KEY": "re_mock_test_key"}):
        res = process_pending_email_deliveries(db)

    assert res["sent"] == 0
    assert res["skipped"] >= 1

    db.refresh(delivery)
    assert delivery.status == "skipped"
    assert delivery.last_error_code == "STALE_REMINDER"


def test_dispatch_time_stale_reminder_canceled_if_plan_becomes_free(db, non_legacy_user):
    """If subscription becomes Free while a reminder is queued in email_deliveries, dispatcher skips it."""
    import os
    from unittest.mock import patch
    from app.services.email_service import process_pending_email_deliveries

    now = utcnow()
    sub = get_or_create_subscription(db, non_legacy_user.id)
    sub.plan = "pro"
    sub.status = "active"
    sub.current_period_end = now + timedelta(days=6, hours=12)
    db.commit()

    # Queue reminder
    delivery = enqueue_subscription_reminder_email(db, non_legacy_user, sub, threshold_days=7)
    assert delivery is not None
    db.commit()

    # Subscription is downgraded to Free (e.g. admin action or expiration)
    sub.plan = "free"
    sub.status = "expired"
    db.commit()

    # Dispatcher runs
    with patch.dict(os.environ, {"EMAIL_ENABLED": "true", "RESEND_API_KEY": "re_mock_test_key"}):
        res = process_pending_email_deliveries(db)

    assert res["sent"] == 0
    db.refresh(delivery)
    assert delivery.status == "skipped"
    assert delivery.last_error_code == "STALE_REMINDER"


def test_concurrent_renewal_and_expiration_locking(db, non_legacy_user):
    """A renewal that happens just as an expiration job runs is preserved and never overwritten."""
    now = utcnow()
    sub = get_or_create_subscription(db, non_legacy_user.id)
    sub.plan = "pro"
    sub.status = "active"
    # Was expired 1 minute ago
    sub.current_period_end = now - timedelta(minutes=1)
    db.commit()

    # Payment arrives and extends Pro (+30 days)
    activate_pro_from_payment(db, non_legacy_user.id, transaction_id=888, duration_days=30, commit=True)

    # Expiration job executes immediately after
    res = process_subscription_expirations(db)
    assert res["expired"] == 0

    # Ensure subscription remains active Pro
    db.refresh(sub)
    assert sub.plan == "pro"
    assert sub.status == "active"
    assert ensure_utc(sub.current_period_end) > now + timedelta(days=25)


def test_free_quota_strictly_enforced_at_creation(db, non_legacy_user, client):
    """Free quota is strictly limited to 3 HTTP monitors at creation (never weakened)."""
    from app.dependencies.auth import get_current_user
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: non_legacy_user

    # Create 3 monitors
    for i in range(3):
        res = client.post("/monitors", json={
            "name": f"Free Monitor {i+1}",
            "type": "http",
            "url": f"https://example{i+1}.com",
            "interval_seconds": 300,
        })
        assert res.status_code == 201

    # 4th monitor creation must be rejected with 403 Forbidden
    res_4th = client.post("/monitors", json={
        "name": "Free Monitor 4",
        "type": "http",
        "url": "https://example4.com",
        "interval_seconds": 300,
    })
    assert res_4th.status_code == 403
    detail = res_4th.json()["detail"]
    assert detail["code"] == "PLAN_LIMIT_EXCEEDED"
    assert detail["limit"] == 3


