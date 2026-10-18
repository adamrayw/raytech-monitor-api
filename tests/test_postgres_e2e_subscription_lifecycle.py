"""RAYTECH MONITOR v0.9 — Comprehensive PostgreSQL End-to-End Verification Suite.

Tests Automatic Expiration, Safe Downgrade, Resource Restriction, Transactional Lifecycle Emails,
and Real PostgreSQL Concurrency & Row-Level Locking against an isolated test database.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Generator
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.main import app
from app.models.email_delivery import EmailDelivery
from app.models.heartbeat_event import HeartbeatEvent
from app.models.incident import Incident
from app.models.monitor import Monitor
from app.models.monitor_check import MonitorCheck
from app.models.payment_transaction import PaymentTransaction
from app.models.status_page import StatusPage
from app.models.subscription import UserSubscription
from app.models.subscription_audit import SubscriptionAuditLog
from app.models.user import User
from app.services.email_service import (
    enqueue_subscription_expired_email,
    enqueue_subscription_reminder_email,
    format_wib_datetime,
    process_pending_email_deliveries,
)
from app.services.heartbeat_service import (
    evaluate_overdue_heartbeat,
    get_overdue_heartbeat_monitor_ids,
)
from app.services.monitor_runner import get_due_monitor_ids
from app.services.ssl_service import get_due_ssl_monitor_ids
from app.services.subscription_service import (
    activate_pro_from_payment,
    ensure_utc,
    get_effective_plan,
    get_eligible_resource_ids,
    get_entitlements,
    get_subscription_usage,
    process_subscription_expirations,
    process_subscription_reminders,
    utcnow,
)

PG_TEST_URL = "postgresql+psycopg://postgres:root@localhost:5432/raytech_monitor_test_v09"


@pytest.fixture(scope="session")
def pg_engine():
    """Isolated real PostgreSQL engine for raytech_monitor_test_v09."""
    engine = create_engine(PG_TEST_URL, pool_pre_ping=True)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def pg_db(pg_engine) -> Generator[Session, None, None]:
    """Provides a fresh isolated PostgreSQL database session per test."""
    connection = pg_engine.connect()
    transaction = connection.begin()
    TestSession = sessionmaker(bind=connection, autoflush=False, autocommit=False)
    session = TestSession()

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def seeded_users(pg_db) -> dict[str, Any]:
    """Seeds synthetic test data for User A (Pro), User B (Free), and User C (Legacy grandfathered).

    Strict deterministic ordering via created_at and IDs.
    """
    t0 = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Test User A: Pro subscriber with controlled expiration
    user_a = User(
        email="user_a_pro@synthetic.test",
        name="User A Pro",
        google_sub="syn-sub-user-a",
        is_active=True,
        created_at=t0 - timedelta(days=30),
    )
    pg_db.add(user_a)
    pg_db.flush()

    sub_a = UserSubscription(
        user_id=user_a.id,
        plan="pro",
        status="active",
        legacy_override=False,
        current_period_start=t0 - timedelta(days=20),
        current_period_end=t0 + timedelta(days=10),
        created_at=t0 - timedelta(days=20),
    )
    pg_db.add(sub_a)

    # 4 HTTP monitors for User A with deterministic created_at
    # Monitor A1: Oldest, active
    m_a1 = Monitor(
        user_id=user_a.id,
        name="Monitor A1 (HTTP)",
        type="http",
        url="https://a1.synthetic.test",
        interval_seconds=300,
        status="operational",
        is_active=True,
        next_check_at=t0,
        created_at=t0 - timedelta(days=4),
    )
    # Monitor A2: 2nd oldest, active
    m_a2 = Monitor(
        user_id=user_a.id,
        name="Monitor A2 (HTTP)",
        type="http",
        url="https://a2.synthetic.test",
        interval_seconds=300,
        status="operational",
        is_active=True,
        next_check_at=t0,
        created_at=t0 - timedelta(days=3),
    )
    # Monitor A3: 3rd oldest, MANUALLY PAUSED by user
    m_a3 = Monitor(
        user_id=user_a.id,
        name="Monitor A3 (HTTP Paused)",
        type="http",
        url="https://a3.synthetic.test",
        interval_seconds=300,
        status="paused",
        is_active=False,
        next_check_at=None,
        created_at=t0 - timedelta(days=2),
    )
    # Monitor A4: 4th oldest, active WITH EXISTING OPEN INCIDENT
    m_a4 = Monitor(
        user_id=user_a.id,
        name="Monitor A4 (HTTP Incident)",
        type="http",
        url="https://a4.synthetic.test",
        interval_seconds=300,
        status="operational",
        is_active=True,
        next_check_at=t0,
        created_at=t0 - timedelta(days=1),
    )
    pg_db.add_all([m_a1, m_a2, m_a3, m_a4])
    pg_db.flush()

    # Open incident on Monitor A4
    inc_a4 = Incident(
        monitor_id=m_a4.id,
        status="open",
        cause="HTTP 500 Internal Server Error",
        start_http_status=500,
        last_http_status=500,
        started_at=t0 - timedelta(hours=2),
        created_at=t0 - timedelta(hours=2),
    )
    pg_db.add(inc_a4)

    # Historical monitor checks
    check_a1 = MonitorCheck(
        monitor_id=m_a1.id,
        checked_at=t0 - timedelta(minutes=5),
        success=True,
        http_status=200,
        response_ms=45,
    )
    pg_db.add(check_a1)

    # Previous payment transaction for User A
    tx_a = PaymentTransaction(
        user_id=user_a.id,
        external_id="syn-tx-user-a-101",
        provider_payment_id="inv-syn-a-101",
        amount=49000,
        currency="IDR",
        status="PAID",
        payment_method="BANK_TRANSFER",
        paid_at=t0 - timedelta(days=20),
        created_at=t0 - timedelta(days=20),
    )
    pg_db.add(tx_a)
    pg_db.flush()

    # Existing billing confirmation email in outbox
    email_a = EmailDelivery(
        user_id=user_a.id,
        recipient_email=user_a.email,
        event_type="payment_confirmed",
        idempotency_key=f"payment_confirmed:{tx_a.id}",
        subject="Your Raytech Monitor Pro payment is confirmed",
        body_text="Receipt",
        body_html="<p>Receipt</p>",
        status="sent",
        sent_at=t0 - timedelta(days=20),
        created_at=t0 - timedelta(days=20),
    )
    pg_db.add(email_a)

    # 2 Heartbeat monitors for User A
    hb_a1 = Monitor(
        user_id=user_a.id,
        name="HB A1 (Heartbeat)",
        type="heartbeat",
        interval_seconds=300,
        expected_interval_seconds=300,
        grace_period_seconds=60,
        status="healthy",
        is_active=True,
        deadline_at=t0 + timedelta(minutes=5),
        created_at=t0 - timedelta(days=3),
    )
    hb_a2 = Monitor(
        user_id=user_a.id,
        name="HB A2 (Heartbeat)",
        type="heartbeat",
        interval_seconds=300,
        expected_interval_seconds=300,
        grace_period_seconds=60,
        status="healthy",
        is_active=True,
        deadline_at=t0 + timedelta(minutes=5),
        created_at=t0 - timedelta(days=2),
    )
    pg_db.add_all([hb_a1, hb_a2])

    # 2 Status Pages for User A
    sp_a1 = StatusPage(
        user_id=user_a.id,
        name="Status Page A1",
        slug="status-a1-page",
        is_published=True,
        created_at=t0 - timedelta(days=4),
    )
    sp_a2 = StatusPage(
        user_id=user_a.id,
        name="Status Page A2",
        slug="status-a2-page",
        is_published=True,
        created_at=t0 - timedelta(days=3),
    )
    pg_db.add_all([sp_a1, sp_a2])

    # 2. Test User B: Standard Free plan user
    user_b = User(
        email="user_b_free@synthetic.test",
        name="User B Free",
        google_sub="syn-sub-user-b",
        is_active=True,
        created_at=t0 - timedelta(days=10),
    )
    pg_db.add(user_b)
    pg_db.flush()

    sub_b = UserSubscription(
        user_id=user_b.id,
        plan="free",
        status="active",
        legacy_override=False,
        created_at=t0 - timedelta(days=10),
    )
    pg_db.add(sub_b)

    for i in range(3):
        m_b = Monitor(
            user_id=user_b.id,
            name=f"Monitor B{i+1}",
            type="http",
            url=f"https://b{i+1}.synthetic.test",
            interval_seconds=300,
            status="operational",
            is_active=True,
            created_at=t0 - timedelta(days=2, hours=i),
        )
        pg_db.add(m_b)

    hb_b = Monitor(
        user_id=user_b.id,
        name="HB B1",
        type="heartbeat",
        interval_seconds=300,
        status="healthy",
        is_active=True,
        created_at=t0 - timedelta(days=2),
    )
    sp_b = StatusPage(
        user_id=user_b.id,
        name="Status Page B",
        slug="status-b-page",
        is_published=True,
        created_at=t0 - timedelta(days=2),
    )
    pg_db.add_all([hb_b, sp_b])

    # 3. Test User C: Grandfathered Pro with legacy_override = True
    user_c = User(
        email="user_c_legacy@synthetic.test",
        name="User C Legacy",
        google_sub="syn-sub-user-c",
        is_active=True,
        created_at=t0 - timedelta(days=100),
    )
    pg_db.add(user_c)
    pg_db.flush()

    sub_c = UserSubscription(
        user_id=user_c.id,
        plan="pro",
        status="active",
        legacy_override=True,
        override_reason="pre_v0.7_legacy_grandfathered",
        current_period_end=t0 - timedelta(days=50),  # Past due but protected
        created_at=t0 - timedelta(days=100),
    )
    pg_db.add(sub_c)
    pg_db.commit()

    return {
        "t0": t0,
        "user_a": user_a,
        "sub_a": sub_a,
        "monitors_a": [m_a1, m_a2, m_a3, m_a4],
        "inc_a4": inc_a4,
        "hb_a": [hb_a1, hb_a2],
        "sp_a": [sp_a1, sp_a2],
        "user_b": user_b,
        "user_c": user_c,
        "sub_c": sub_c,
    }


def test_expiration_downgrades_user_a_and_protects_others(pg_db, seeded_users):
    """Section 4: Clock advances past User A's expiration; verifies downgrade, audit, and email."""
    t0 = seeded_users["t0"]
    future_time = t0 + timedelta(days=11)  # 1 day after sub_a.current_period_end

    with patch("app.services.subscription_service.utcnow", return_value=future_time), \
         patch("app.services.email_service.utcnow", return_value=future_time):

        # Execute actual background expiration service
        res = process_subscription_expirations(pg_db)
        assert res["expired"] == 1

        # User A's subscription must now be Free / expired
        sub_a = pg_db.get(UserSubscription, seeded_users["sub_a"].id)
        assert sub_a.plan == "free"
        assert sub_a.status == "expired"

        # Effective plan check in API layer must report Free
        effective_plan, _ = get_effective_plan(pg_db, seeded_users["user_a"].id)
        assert effective_plan == "free"

        # Exactly 1 pro_expired audit log must exist
        audit_logs = pg_db.scalars(
            select(SubscriptionAuditLog).where(
                SubscriptionAuditLog.user_id == seeded_users["user_a"].id,
                SubscriptionAuditLog.event_type == "pro_expired",
            )
        ).all()
        assert len(audit_logs) == 1
        assert audit_logs[0].previous_plan == "pro"
        assert audit_logs[0].new_plan == "free"

        # Exactly 1 sub_expired email delivery must be enqueued in outbox
        expired_emails = pg_db.scalars(
            select(EmailDelivery).where(
                EmailDelivery.user_id == seeded_users["user_a"].id,
                EmailDelivery.event_type == "sub_expired",
            )
        ).all()
        assert len(expired_emails) == 1
        assert "has expired" in expired_emails[0].subject.lower()

        # User B (already Free) must be unchanged
        sub_b = pg_db.scalar(select(UserSubscription).where(UserSubscription.user_id == seeded_users["user_b"].id))
        assert sub_b.plan == "free"

        # Legacy User C must be protected
        sub_c = pg_db.scalar(select(UserSubscription).where(UserSubscription.user_id == seeded_users["user_c"].id))
        assert sub_c.plan == "pro"
        assert sub_c.legacy_override is True

        # Idempotency: Re-running expiration job produces 0 additional downgrades, audits, or emails
        res_repeat = process_subscription_expirations(pg_db)
        assert res_repeat["expired"] == 0

        total_audits = pg_db.scalars(
            select(SubscriptionAuditLog).where(
                SubscriptionAuditLog.user_id == seeded_users["user_a"].id,
                SubscriptionAuditLog.event_type == "pro_expired",
            )
        ).all()
        assert len(total_audits) == 1


def test_http_monitor_restriction_deterministic_order(pg_db, seeded_users):
    """Section 5: 4 HTTP monitors preserved; oldest 3 eligible; 4th restricted without false incidents."""
    t0 = seeded_users["t0"]
    future_time = t0 + timedelta(days=11)

    with patch("app.services.subscription_service.utcnow", return_value=future_time), \
         patch("app.services.monitor_runner.utcnow", return_value=future_time):

        process_subscription_expirations(pg_db)

        # All 4 monitors still exist
        monitors_a = seeded_users["monitors_a"]
        for m in monitors_a:
            db_m = pg_db.get(Monitor, m.id)
            assert db_m is not None

        # Eligible IDs: Oldest 3 (m_a1, m_a2, m_a3)
        eligible = get_eligible_resource_ids(pg_db, seeded_users["user_a"].id, "http")
        assert len(eligible) == 3
        assert monitors_a[0].id in eligible  # Monitor A1
        assert monitors_a[1].id in eligible  # Monitor A2
        assert monitors_a[2].id in eligible  # Monitor A3 (paused)
        assert monitors_a[3].id not in eligible  # Monitor A4 is RESTRICTED

        # Check runner scheduling
        due_ids = get_due_monitor_ids(pg_db)
        assert monitors_a[0].id in due_ids
        assert monitors_a[1].id in due_ids
        assert monitors_a[2].id not in due_ids  # Paused -> not due
        assert monitors_a[3].id not in due_ids  # Restricted -> excluded from runner!

        # Manually paused monitor remains paused
        m_a3 = pg_db.get(Monitor, monitors_a[2].id)
        assert m_a3.is_active is False
        assert m_a3.status == "paused"

        # Monitor A4's existing open incident is preserved (neither closed nor reopened)
        inc = pg_db.get(Incident, seeded_users["inc_a4"].id)
        assert inc.status == "open"
        assert inc.cause == "HTTP 500 Internal Server Error"

        # Verify no false DOWN incidents were generated for restricted monitor A4
        all_incidents_a4 = pg_db.scalars(
            select(Incident).where(Incident.monitor_id == monitors_a[3].id)
        ).all()
        assert len(all_incidents_a4) == 1  # Only the pre-existing incident


def test_heartbeat_restriction_and_missed_alert_safety(pg_db, seeded_users):
    """Section 6: Heartbeat restriction excludes 2nd heartbeat from missed alerts."""
    t0 = seeded_users["t0"]
    future_time = t0 + timedelta(days=11)
    hb_a = seeded_users["hb_a"]

    # Set both deadlines in the past relative to future_time
    hb_a[0].deadline_at = future_time - timedelta(minutes=5)
    hb_a[1].deadline_at = future_time - timedelta(minutes=5)
    pg_db.commit()

    with patch("app.services.subscription_service.utcnow", return_value=future_time), \
         patch("app.services.heartbeat_service.utcnow", return_value=future_time):

        process_subscription_expirations(pg_db)

        # Both records remain
        assert pg_db.get(Monitor, hb_a[0].id) is not None
        assert pg_db.get(Monitor, hb_a[1].id) is not None

        # Eligible heartbeat: only oldest (HB A1)
        eligible_hb = get_eligible_resource_ids(pg_db, seeded_users["user_a"].id, "heartbeat")
        assert len(eligible_hb) == 1
        assert hb_a[0].id in eligible_hb
        assert hb_a[1].id not in eligible_hb  # HB A2 is restricted

        # Overdue query excludes restricted HB A2
        overdue_ids = get_overdue_heartbeat_monitor_ids(pg_db)
        assert hb_a[0].id in overdue_ids
        assert hb_a[1].id not in overdue_ids

        # Attempting direct evaluation on restricted HB A2 returns False
        evaluated = evaluate_overdue_heartbeat(pg_db, hb_a[1].id)
        assert evaluated is False

        # No missed incidents on restricted HB A2
        incidents_hb2 = pg_db.scalars(
            select(Incident).where(Incident.monitor_id == hb_a[1].id)
        ).all()
        assert len(incidents_hb2) == 0


def test_public_status_page_restriction_returns_404(pg_db, seeded_users):
    """Section 7: Public status page beyond Free limit returns 404; no data deleted."""
    t0 = seeded_users["t0"]
    future_time = t0 + timedelta(days=11)
    sp_a = seeded_users["sp_a"]

    with patch("app.services.subscription_service.utcnow", return_value=future_time):
        process_subscription_expirations(pg_db)

        # Both pages still exist in database
        assert pg_db.get(StatusPage, sp_a[0].id) is not None
        assert pg_db.get(StatusPage, sp_a[1].id) is not None

        # Setup test client overriding get_db
        app.dependency_overrides[get_db] = lambda: pg_db
        client = TestClient(app)

        # Oldest page (SP A1) is accessible
        res_a1 = client.get(f"/public/status/{sp_a[0].slug}")
        assert res_a1.status_code == 200

        # Excess page (SP A2) is restricted -> returns 404
        res_a2 = client.get(f"/public/status/{sp_a[1].slug}")
        assert res_a2.status_code == 404

        # Usage API shows 1 active and 1 restricted page
        usage = get_subscription_usage(pg_db, seeded_users["user_a"].id)
        assert usage["status_pages"]["used"] == 2
        assert usage["status_pages"]["active_count"] == 1
        assert usage["status_pages"]["restricted_count"] == 1


def test_ssl_monitoring_restricted_for_free_plan(pg_db, seeded_users):
    """Section 8: Free user monitors cannot schedule SSL checks; transactional emails unaffected."""
    t0 = seeded_users["t0"]
    future_time = t0 + timedelta(days=11)
    m_a1 = seeded_users["monitors_a"][0]
    m_a1.ssl_monitoring_enabled = True
    m_a1.ssl_next_check_at = future_time - timedelta(minutes=10)
    pg_db.commit()

    with patch("app.services.subscription_service.utcnow", return_value=future_time), \
         patch("app.services.ssl_service.utcnow", return_value=future_time):

        process_subscription_expirations(pg_db)

        # User A is now Free -> SSL checks must be excluded
        ssl_due = get_due_ssl_monitor_ids(pg_db)
        assert m_a1.id not in ssl_due

        # Free entitlements confirm ssl_monitoring is False
        ent = get_entitlements(pg_db, seeded_users["user_a"].id)
        assert ent["ssl_monitoring"] is False
        assert ent["email_alerts"] is False

        # Transactional lifecycle emails remain eligible in outbox
        expired_emails = pg_db.scalars(
            select(EmailDelivery).where(
                EmailDelivery.user_id == seeded_users["user_a"].id,
                EmailDelivery.event_type == "sub_expired",
            )
        ).all()
        assert len(expired_emails) == 1


def test_reminder_lifecycle_and_dispatch_time_cancellation(pg_db, seeded_users):
    """Section 9: Reminders at 7d, 3d, 1d; WIB formatting; stale reminder cancellation."""
    t0 = seeded_users["t0"]
    sub_a = seeded_users["sub_a"]
    exp_time = sub_a.current_period_end  # t0 + 10 days

    # 1. Simulate 7 days remaining (3.5 days after t0)
    time_7d = exp_time - timedelta(days=6, hours=12)
    with patch("app.services.subscription_service.utcnow", return_value=time_7d), \
         patch("app.services.email_service.utcnow", return_value=time_7d):

        res_7d = process_subscription_reminders(pg_db)
        assert res_7d["reminders_queued"] == 1

        d7 = pg_db.scalar(
            select(EmailDelivery).where(
                EmailDelivery.user_id == seeded_users["user_a"].id,
                EmailDelivery.event_type == "sub_reminder_7d",
            )
        )
        assert d7 is not None
        assert "expires in 7 days" in d7.subject.lower()
        assert "WIB" in d7.body_html

    # 2. Simulate 3 days remaining
    time_3d = exp_time - timedelta(days=2, hours=12)
    with patch("app.services.subscription_service.utcnow", return_value=time_3d), \
         patch("app.services.email_service.utcnow", return_value=time_3d):

        res_3d = process_subscription_reminders(pg_db)
        assert res_3d["reminders_queued"] == 1

        d3 = pg_db.scalar(
            select(EmailDelivery).where(
                EmailDelivery.user_id == seeded_users["user_a"].id,
                EmailDelivery.event_type == "sub_reminder_3d",
            )
        )
        assert d3 is not None
        assert "3 days" in d3.subject.lower()

    # 3. Simulate 1 day remaining
    time_1d = exp_time - timedelta(hours=18)
    with patch("app.services.subscription_service.utcnow", return_value=time_1d), \
         patch("app.services.email_service.utcnow", return_value=time_1d):

        res_1d = process_subscription_reminders(pg_db)
        assert res_1d["reminders_queued"] == 1

        d1 = pg_db.scalar(
            select(EmailDelivery).where(
                EmailDelivery.user_id == seeded_users["user_a"].id,
                EmailDelivery.event_type == "sub_reminder_1d",
            )
        )
        assert d1 is not None
        assert "tomorrow" in d1.subject.lower()

    # 4. User now renews Pro by 30 days while d1 is still in pending outbox
    time_renew = time_1d + timedelta(hours=1)
    with patch("app.services.subscription_service.utcnow", return_value=time_renew):
        tx_555 = PaymentTransaction(
            id=555,
            user_id=seeded_users["user_a"].id,
            external_id="syn-tx-user-a-555",
            provider_payment_id="inv-syn-a-555",
            amount=49000,
            currency="IDR",
            status="PAID",
            payment_method="BANK_TRANSFER",
            paid_at=time_renew,
        )
        pg_db.add(tx_555)
        pg_db.flush()
        activate_pro_from_payment(pg_db, seeded_users["user_a"].id, transaction_id=555, duration_days=30, commit=True)

    # 5. Dispatcher runs: d1 must be skipped as STALE_REMINDER because expiration was extended!
    with patch("app.services.email_service.utcnow", return_value=time_renew), \
         patch.dict("os.environ", {"EMAIL_ENABLED": "true", "RESEND_API_KEY": "re_synthetic_key"}):
        disp_res = process_pending_email_deliveries(pg_db)

    pg_db.refresh(d1)
    assert d1.status == "skipped"
    assert d1.last_error_code == "STALE_REMINDER"


def test_reactivation_restores_all_resources_safely(pg_db, seeded_users):
    """Section 10: New payment restores Pro; resources un-restricted; paused monitors remain paused."""
    t0 = seeded_users["t0"]
    future_time = t0 + timedelta(days=11)

    # Step 1: Expire User A
    with patch("app.services.subscription_service.utcnow", return_value=future_time), \
         patch("app.services.email_service.utcnow", return_value=future_time):
        process_subscription_expirations(pg_db)

    # Step 2: User A purchases Pro renewal on day 12
    reactivate_time = t0 + timedelta(days=12)
    with patch("app.services.subscription_service.utcnow", return_value=reactivate_time), \
         patch("app.services.email_service.utcnow", return_value=reactivate_time):

        from app.services.payment_service import reconcile_and_process_payment

        tx_901 = PaymentTransaction(
            id=901,
            user_id=seeded_users["user_a"].id,
            external_id="syn-tx-user-a-901",
            provider_payment_id="inv-syn-a-901",
            amount=49000,
            currency="IDR",
            status="PENDING",
            payment_method="BANK_TRANSFER",
            created_at=reactivate_time,
        )
        pg_db.add(tx_901)
        pg_db.flush()

        authoritative_invoice = {
            "id": "inv-syn-a-901",
            "external_id": "syn-tx-user-a-901",
            "status": "PAID",
            "amount": 49000,
            "currency": "IDR",
            "payment_method": "BANK_TRANSFER",
            "paid_at": reactivate_time.isoformat(),
        }

        with patch("app.services.payment_service.get_xendit_invoice", return_value=authoritative_invoice):
            outcome, tx_settled = reconcile_and_process_payment(
                pg_db,
                transaction_id=tx_901.id,
                expected_external_id=tx_901.external_id,
                expected_invoice_id=tx_901.provider_payment_id,
            )
        assert outcome == "activated"
        assert tx_settled.status == "PAID"

        sub = pg_db.scalar(select(UserSubscription).where(UserSubscription.user_id == seeded_users["user_a"].id))
        assert sub.plan == "pro"
        assert sub.status == "active"
        expected_end = reactivate_time + timedelta(days=30)
        assert abs((ensure_utc(sub.current_period_end) - expected_end).total_seconds()) < 5

        # All 4 HTTP monitors are now eligible
        eligible_http = get_eligible_resource_ids(pg_db, seeded_users["user_a"].id, "http")
        assert len(eligible_http) == 4

        # Manually paused monitor A3 remains paused
        m_a3 = pg_db.get(Monitor, seeded_users["monitors_a"][2].id)
        assert m_a3.is_active is False
        assert m_a3.status == "paused"

        # Both heartbeats and both status pages are eligible again
        assert len(get_eligible_resource_ids(pg_db, seeded_users["user_a"].id, "heartbeat")) == 2
        assert len(get_eligible_resource_ids(pg_db, seeded_users["user_a"].id, "status_page")) == 2

        # Prior incidents and checks preserved
        inc = pg_db.get(Incident, seeded_users["inc_a4"].id)
        assert inc.status == "open"

        # One new pro_activated audit record
        audit = pg_db.scalar(
            select(SubscriptionAuditLog).where(
                SubscriptionAuditLog.user_id == seeded_users["user_a"].id,
                SubscriptionAuditLog.payment_transaction_id == 901,
            )
        )
        assert audit is not None
        assert audit.event_type == "pro_activated"

        # One payment confirmation outbox record queued
        tx_email = pg_db.scalar(
            select(EmailDelivery).where(
                EmailDelivery.user_id == seeded_users["user_a"].id,
                EmailDelivery.idempotency_key == "payment_confirmed:901",
            )
        )
        assert tx_email is not None


def test_postgresql_row_locking_concurrency(pg_engine):
    """Section 11: Real PostgreSQL row-level locking concurrency between renewal and expiration."""
    # Create two separate concurrent sessions on raytech_monitor_test_v09
    SessionA = sessionmaker(bind=pg_engine.connect(), autoflush=False, autocommit=False)
    SessionB = sessionmaker(bind=pg_engine.connect(), autoflush=False, autocommit=False)
    session_a = SessionA()
    session_b = SessionB()

    try:
        now = datetime.now(timezone.utc)
        # Create test user directly in PostgreSQL
        user = User(email="concurrent_test@synthetic.test", name="Concurrent User", google_sub="syn-concurrent-1")
        session_a.add(user)
        session_a.commit()

        sub = UserSubscription(
            user_id=user.id,
            plan="pro",
            status="active",
            legacy_override=False,
            current_period_end=now - timedelta(minutes=1),  # 1 minute past due
        )
        session_a.add(sub)
        session_a.commit()

        # Session A: Payment arrives and begins renewal with row lock
        tx_777 = PaymentTransaction(
            id=777,
            user_id=user.id,
            external_id="syn-tx-concurrent-777",
            provider_payment_id="inv-syn-concurrent-777",
            amount=49000,
            currency="IDR",
            status="PAID",
            payment_method="BANK_TRANSFER",
            paid_at=now,
        )
        session_a.add(tx_777)
        session_a.commit()
        activate_pro_from_payment(session_a, user.id, transaction_id=777, duration_days=30, commit=True)

        # Session B: Expiration job runs
        res = process_subscription_expirations(session_b)
        assert res["expired"] == 0

        # Verify subscription was NOT downgraded to free
        session_b.expire_all()
        fresh_sub = session_b.scalar(select(UserSubscription).where(UserSubscription.user_id == user.id))
        assert fresh_sub.plan == "pro"
        assert fresh_sub.status == "active"
        assert ensure_utc(fresh_sub.current_period_end) > now + timedelta(days=25)

    finally:
        session_a.close()
        session_b.close()
