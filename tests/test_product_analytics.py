from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select

from app.models.payment_transaction import PaymentTransaction
from app.models.product_event import ProductEvent
from app.models.user import User
from app.models.user_feedback import UserFeedback
from app.services.payment_service import reconcile_and_process_payment
from app.services.product_analytics_service import build_admin_product_analytics
from app.services.product_event_service import record_product_event


def test_authenticated_client_event_is_created_once_per_day(client, db, test_user):
    payload = {
        "event_name": "dashboard_viewed",
        "properties": {"page_path": "/app?secret=removed"},
    }

    first = client.post("/analytics/events", json=payload)
    second = client.post("/analytics/events", json=payload)

    assert first.status_code == 202
    assert second.status_code == 202
    events = db.scalars(
        select(ProductEvent).where(
            ProductEvent.user_id == test_user.id,
            ProductEvent.event_name == "dashboard_viewed",
        )
    ).all()
    assert len(events) == 1
    assert events[0].properties == {"page_path": "/app"}


def test_invalid_or_sensitive_client_event_is_rejected(client, db):
    invalid_name = client.post(
        "/analytics/events",
        json={"event_name": "made_up_event", "properties": {}},
    )
    sensitive = client.post(
        "/analytics/events",
        json={
            "event_name": "dashboard_viewed",
            "properties": {"api_key": "must-not-be-stored"},
        },
    )

    assert invalid_name.status_code == 422
    assert sensitive.status_code == 422
    assert int(db.scalar(select(func.count(ProductEvent.id))) or 0) == 0


def test_first_http_monitor_records_activation_once(client, db, test_user):
    payload = {
        "name": "Primary site",
        "type": "http",
        "url": "https://example.com/health",
        "interval_seconds": 300,
        "timeout_seconds": 10,
        "expected_status_min": 200,
        "expected_status_max": 299,
        "degraded_threshold_ms": 1500,
        "ssl_monitoring_enabled": False,
        "ssl_warning_days": 14,
    }

    with patch("app.routes.monitors.validate_target", new=AsyncMock(return_value=None)):
        first = client.post("/monitors", json=payload)
        second = client.post("/monitors", json={**payload, "name": "Secondary site"})

    assert first.status_code == 201
    assert second.status_code == 201
    first_events = db.scalars(
        select(ProductEvent).where(
            ProductEvent.user_id == test_user.id,
            ProductEvent.event_name == "first_monitor_created",
        )
    ).all()
    monitor_events = db.scalars(
        select(ProductEvent).where(
            ProductEvent.user_id == test_user.id,
            ProductEvent.event_name == "monitor_created",
        )
    ).all()
    assert len(first_events) == 1
    assert len(monitor_events) == 2
    assert first_events[0].properties["time_to_activation_seconds"] >= 0


def test_verified_payment_events_are_idempotent(db, other_user):
    tx = PaymentTransaction(
        user_id=other_user.id,
        amount=49000,
        total_amount=49007,
        currency="IDR",
        status="PENDING",
        provider="qrisgate",
        provider_mode="live",
        revenue_classification="REAL",
        external_id="qrg_analytics_payment_449b",
        provider_payment_id="trx_analytics_payment",
    )
    db.add(tx)
    db.commit()

    authoritative = {
        "id": tx.provider_payment_id,
        "external_order_id": "••••449b",
        "status": "PAID",
        "amount": 49000,
        "total_amount": 49007,
        "paid_at": datetime.now(timezone.utc).isoformat(),
    }
    with (
        patch(
            "app.services.payment_service.get_qrisgate_api_key",
            return_value="qg_live_test",
        ),
        patch(
            "app.services.payment_service.get_qrisgate_transaction",
            return_value=authoritative,
        ),
    ):
        first_outcome, _ = reconcile_and_process_payment(db, tx.id)
        second_outcome, _ = reconcile_and_process_payment(db, tx.id)

    assert first_outcome == "activated"
    assert second_outcome == "already_paid"
    assert int(
        db.scalar(
            select(func.count(ProductEvent.id)).where(
                ProductEvent.event_name == "payment_succeeded",
                ProductEvent.user_id == other_user.id,
            )
        ) or 0
    ) == 1
    assert int(
        db.scalar(
            select(func.count(ProductEvent.id)).where(
                ProductEvent.event_name == "pro_activated",
                ProductEvent.user_id == other_user.id,
            )
        ) or 0
    ) == 1


def test_retention_uses_meaningful_user_activity_not_worker_events(db):
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    retained = User(
        email="retained@example.com",
        google_sub="retained-sub",
        is_active=True,
        created_at=now - timedelta(days=8),
    )
    background_only = User(
        email="background@example.com",
        google_sub="background-sub",
        is_active=True,
        created_at=now - timedelta(days=8),
    )
    db.add_all([retained, background_only])
    db.flush()

    for user in (retained, background_only):
        record_product_event(
            db,
            event_name="user_signed_up",
            user_id=user.id,
            source="api",
            properties={"launch_cohort": "beta"},
            idempotency_key=f"signup:{user.id}",
            created_at=now - timedelta(days=8),
        )
    record_product_event(
        db,
        event_name="dashboard_viewed",
        user_id=retained.id,
        source="web",
        properties={"page_path": "/app"},
        idempotency_key="retained-d7",
        created_at=now - timedelta(days=1),
    )
    record_product_event(
        db,
        event_name="pro_expired",
        user_id=background_only.id,
        source="worker",
        properties={"subscription_id": 999},
        idempotency_key="background-d7",
        created_at=now - timedelta(days=1),
    )
    db.commit()

    analytics = build_admin_product_analytics(db, now=now)
    d1 = analytics["retention"]["d1"]
    d7 = analytics["retention"]["d7"]
    assert d1["retained_users"] == 1
    assert d1["retention_percent"] == 50.0
    assert d7["cohort_size"] == 2
    assert d7["retained_users"] == 1
    assert d7["retention_percent"] == 50.0


def test_feedback_submission_and_admin_authorization(client, db, test_user):
    payload = {
        "category": "user_experience",
        "message": "Form monitor mudah dipahami, tetapi bantuan interval perlu diperjelas.",
        "rating": 4,
        "page_path": "/monitors?draft=private",
    }

    submitted = client.post("/feedback", json=payload)
    forbidden = client.get("/admin/feedback")

    assert submitted.status_code == 201
    assert forbidden.status_code == 403
    feedback = db.scalar(select(UserFeedback))
    assert feedback is not None
    assert feedback.page_path == "/monitors"

    test_user.is_admin = True
    db.commit()
    allowed = client.get("/admin/feedback")
    assert allowed.status_code == 200
    assert allowed.json()["count"] == 1
    assert allowed.json()["items"][0]["message"] == payload["message"]


def test_admin_conversion_excludes_sandbox_and_failed_payments(db, other_user):
    real_user = User(
        email="real-payer@example.com",
        google_sub="real-payer-sub",
        is_active=True,
    )
    db.add(real_user)
    db.flush()
    db.add_all(
        [
            PaymentTransaction(
                user_id=real_user.id,
                external_id="real-paid-analytics",
                provider="qrisgate",
                provider_mode="live",
                revenue_classification="REAL",
                amount=49000,
                currency="IDR",
                status="PAID",
                paid_at=datetime.now(timezone.utc),
            ),
            PaymentTransaction(
                user_id=other_user.id,
                external_id="sandbox-paid-analytics",
                provider="qrisgate",
                provider_mode="sandbox",
                revenue_classification="SANDBOX",
                amount=49000,
                currency="IDR",
                status="PAID",
                paid_at=datetime.now(timezone.utc),
            ),
            PaymentTransaction(
                user_id=other_user.id,
                external_id="failed-live-analytics",
                provider="qrisgate",
                provider_mode="live",
                revenue_classification="REAL",
                amount=49000,
                currency="IDR",
                status="FAILED",
            ),
        ]
    )
    db.commit()

    for user, suffix, plan in (
        (real_user, "real", "free"),
        (other_user, "sandbox", "pro"),
    ):
        record_product_event(
            db,
            event_name="pricing_viewed",
            user_id=user.id,
            source="web",
            properties={"page_path": "/settings/billing", "plan": plan},
            idempotency_key=f"pricing:{suffix}",
        )
        record_product_event(
            db,
            event_name="checkout_started",
            user_id=user.id,
            source="api",
            properties={
                "transaction_id": suffix,
                "provider": "qrisgate",
                "amount": 49000,
                "currency": "IDR",
            },
            idempotency_key=f"checkout:{suffix}",
        )
        record_product_event(
            db,
            event_name="payment_succeeded",
            user_id=user.id,
            source="api",
            properties={
                "transaction_id": suffix,
                "provider": "qrisgate",
                "amount": 49000,
                "currency": "IDR",
            },
            idempotency_key=f"paid:{suffix}",
        )
    db.commit()

    analytics = build_admin_product_analytics(db)
    conversion = analytics["overview"]["free_to_pro_conversion"]
    pricing_conversion = analytics["overview"]["pricing_to_checkout"]
    checkout_conversion = analytics["overview"]["checkout_to_payment"]
    assert conversion["numerator"] == 1
    assert conversion["denominator"] >= 2
    assert pricing_conversion == {
        "numerator": 1,
        "denominator": 1,
        "percent": 100.0,
    }
    assert checkout_conversion == {
        "numerator": 1,
        "denominator": 2,
        "percent": 50.0,
    }
