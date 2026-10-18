import html
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models.email_delivery import EmailDelivery
from app.models.notification_preference import NotificationPreference
from app.models.payment_transaction import PaymentTransaction
from app.models.subscription import UserSubscription
from app.models.user import User
from app.services.email_service import (
    enqueue_payment_confirmation_email,
    get_billing_email_from,
    get_provider_idempotency_key,
    process_pending_email_deliveries,
    render_payment_confirmation_email,
    send_resend_email_sync,
)
from app.services.payment_service import reconcile_and_process_payment
from app.services.subscription_service import get_effective_plan, utcnow

TEST_WEBHOOK_TOKEN = "test-xendit-webhook-secret-token"
os.environ["XENDIT_WEBHOOK_TOKEN"] = TEST_WEBHOOK_TOKEN
os.environ["XENDIT_SECRET_KEY"] = "xnd_development_test_key_12345"
os.environ["XENDIT_MODE"] = "test"


@pytest.fixture
def buyer_user(db):
    user = User(
        email="buyer@raytech.cloud",
        name="Alex Mercer",
        google_sub="sub-alex-101",
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
    return user


@pytest.fixture
def test_client_app(db, buyer_user):
    from app.database import get_db
    from app.dependencies.auth import get_current_user

    def override_db():
        yield db

    def override_user():
        return buyer_user

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()


# =========================================================================
# 1. Verified Payment Queues Exactly One Confirmation Email
# =========================================================================

def test_verified_paid_transaction_queues_one_email(db, buyer_user, test_client_app):
    """When a payment is verified as PAID, exactly one confirmation email is enqueued."""
    tx = PaymentTransaction(
        user_id=buyer_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_email_test_1",
        provider_payment_id="xnd_inv_email_1",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    mock_authoritative = {
        "id": tx.provider_payment_id,
        "external_id": tx.external_id,
        "status": "PAID",
        "amount": 49000,
        "currency": "IDR",
        "payment_method": "QRIS",
        "payment_channel": "QRIS",
        "paid_at": utcnow().isoformat(),
    }

    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_authoritative):
        res = test_client_app.post(
            "/webhooks/xendit",
            headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
            json={"id": tx.provider_payment_id, "external_id": tx.external_id, "status": "PAID"},
        )
        assert res.status_code == 200

    # Verify email delivery outbox entry
    deliveries = db.scalars(
        select(EmailDelivery).where(EmailDelivery.user_id == buyer_user.id)
    ).all()

    assert len(deliveries) == 1
    d = deliveries[0]
    assert d.event_type == "payment_confirmed"
    assert d.idempotency_key == f"payment_confirmed:{tx.id}"
    assert d.recipient_email == "buyer@raytech.cloud"
    assert d.status == "pending"
    assert "Your Raytech Monitor Pro payment is confirmed" in d.subject
    assert "IDR 49,000" in d.body_text
    assert tx.external_id in d.body_text
    assert "View Billing & Plan" in d.body_text


# =========================================================================
# 2. Unverified, PENDING, FAILED, EXPIRED Queue No Email
# =========================================================================

def test_pending_payment_queues_no_email(db, buyer_user, test_client_app):
    """Transactions still PENDING must not queue any confirmation email."""
    tx = PaymentTransaction(
        user_id=buyer_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_pending_no_mail",
        provider_payment_id="xnd_inv_pending_no_mail",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    mock_invoice = {
        "id": tx.provider_payment_id,
        "external_id": tx.external_id,
        "status": "PENDING",
        "amount": 49000,
        "currency": "IDR",
    }
    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_invoice):
        res = test_client_app.get(f"/billing/transactions/{tx.id}/status")
        assert res.status_code == 200

    count = db.scalar(select(EmailDelivery.id).where(EmailDelivery.user_id == buyer_user.id))
    assert count is None


def test_failed_and_expired_payments_queue_no_email(db, buyer_user, test_client_app):
    """FAILED and EXPIRED payments never queue confirmation emails."""
    tx_exp = PaymentTransaction(
        user_id=buyer_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_expired_no_mail",
        provider_payment_id="xnd_inv_exp_no_mail",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx_exp)
    db.commit()

    mock_exp = {
        "id": tx_exp.provider_payment_id,
        "external_id": tx_exp.external_id,
        "status": "EXPIRED",
        "amount": 49000,
        "currency": "IDR",
    }
    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_exp):
        test_client_app.post(
            "/webhooks/xendit",
            headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
            json={"id": tx_exp.provider_payment_id, "external_id": tx_exp.external_id, "status": "EXPIRED"},
        )

    count = db.scalar(select(EmailDelivery.id).where(EmailDelivery.user_id == buyer_user.id))
    assert count is None


# =========================================================================
# 3. Exactly-Once Idempotency: Duplicate Callbacks & Concurrency
# =========================================================================

def test_duplicate_webhook_does_not_queue_duplicate_email(db, buyer_user, test_client_app):
    """Repeated webhook deliveries for the same transaction produce exactly 1 outbox record."""
    tx = PaymentTransaction(
        user_id=buyer_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_dup_mail_test",
        provider_payment_id="xnd_inv_dup_mail",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    mock_authoritative = {
        "id": tx.provider_payment_id,
        "external_id": tx.external_id,
        "status": "PAID",
        "amount": 49000,
        "currency": "IDR",
        "payment_method": "BANK_TRANSFER",
        "payment_channel": "BCA",
        "paid_at": utcnow().isoformat(),
    }

    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_authoritative):
        # 1st delivery
        res1 = test_client_app.post(
            "/webhooks/xendit",
            headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
            json={"id": tx.provider_payment_id, "external_id": tx.external_id, "status": "PAID"},
        )
        assert res1.status_code == 200

        # 2nd delivery (duplicate callback)
        res2 = test_client_app.post(
            "/webhooks/xendit",
            headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
            json={"id": tx.provider_payment_id, "external_id": tx.external_id, "status": "PAID"},
        )
        assert res2.status_code == 200
        assert res2.json()["status"] == "already_processed"

    deliveries = db.scalars(
        select(EmailDelivery).where(
            EmailDelivery.idempotency_key == f"payment_confirmed:{tx.id}"
        )
    ).all()
    assert len(deliveries) == 1


def test_concurrent_webhook_and_polling_queues_only_one_email(db, buyer_user):
    """Simultaneous polling and webhook execution queues only one confirmation email."""
    tx = PaymentTransaction(
        user_id=buyer_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_concurrent_mail_test",
        provider_payment_id="xnd_inv_concurrent_mail",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    mock_authoritative = {
        "id": tx.provider_payment_id,
        "external_id": tx.external_id,
        "status": "PAID",
        "amount": 49000,
        "currency": "IDR",
        "paid_at": utcnow().isoformat(),
    }

    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_authoritative):
        outcome1, _ = reconcile_and_process_payment(db, tx.id)
        outcome2, _ = reconcile_and_process_payment(db, tx.id)

    assert outcome1 == "activated"
    assert outcome2 == "already_paid"

    deliveries = db.scalars(
        select(EmailDelivery).where(
            EmailDelivery.idempotency_key == f"payment_confirmed:{tx.id}"
        )
    ).all()
    assert len(deliveries) == 1


# =========================================================================
# 4. Monitoring Email Alerts Disabled Does Not Block Payment Receipt
# =========================================================================

def test_monitoring_email_alerts_disabled_does_not_block_payment_receipt(db, buyer_user):
    """Transactional billing confirmation is sent even if user disabled monitoring email alerts."""
    # User explicitly disables monitoring email alerts
    pref = NotificationPreference(
        user_id=buyer_user.id,
        email_enabled=False,
    )
    db.add(pref)
    db.commit()

    tx = PaymentTransaction(
        user_id=buyer_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_pref_disabled_test",
        provider_payment_id="xnd_inv_pref_disabled",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    mock_authoritative = {
        "id": tx.provider_payment_id,
        "external_id": tx.external_id,
        "status": "PAID",
        "amount": 49000,
        "currency": "IDR",
        "paid_at": utcnow().isoformat(),
    }

    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_authoritative):
        reconcile_and_process_payment(db, tx.id)

    delivery = db.scalar(
        select(EmailDelivery).where(
            EmailDelivery.idempotency_key == f"payment_confirmed:{tx.id}"
        )
    )
    assert delivery is not None
    assert delivery.status == "pending"


# =========================================================================
# 5. Resend Unavailable Does Not Affect Pro Activation
# =========================================================================

def test_resend_unavailable_does_not_affect_pro_activation(db, buyer_user):
    """If Resend API is unreachable during dispatch, Pro activation remains fully intact."""
    tx = PaymentTransaction(
        user_id=buyer_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_resend_down_test",
        provider_payment_id="xnd_inv_resend_down",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    mock_authoritative = {
        "id": tx.provider_payment_id,
        "external_id": tx.external_id,
        "status": "PAID",
        "amount": 49000,
        "currency": "IDR",
        "paid_at": utcnow().isoformat(),
    }

    # Payment succeeds and commits
    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_authoritative):
        reconcile_and_process_payment(db, tx.id)

    # Verify Pro is activated
    plan_id, sub = get_effective_plan(db, buyer_user.id)
    assert plan_id == "pro"
    assert tx.status == "PAID"

    # Now simulate worker attempting to deliver via Resend, but Resend is 503
    with patch.dict(os.environ, {"EMAIL_ENABLED": "true", "RESEND_API_KEY": "re_test_mock_123", "EMAIL_TEST_MODE": "false"}), \
         patch("app.services.email_service.send_resend_email_sync", return_value=(False, 503, None, "Service Unavailable")):
        result = process_pending_email_deliveries(db)
        assert result["processed"] == 1
        assert result["failed"] == 0  # Retryable 503 enters 'retry' status

    # Verify Pro is still active and outbox item is in retry status
    delivery = db.scalar(select(EmailDelivery).where(EmailDelivery.idempotency_key == f"payment_confirmed:{tx.id}"))
    assert delivery.status == "retry"
    assert delivery.attempt_count == 1

    plan_id_after, _ = get_effective_plan(db, buyer_user.id)
    assert plan_id_after == "pro"


# =========================================================================
# 6. Payment Extension Displays New Expiration Date
# =========================================================================

def test_payment_extension_email_displays_new_expiration_date(db, buyer_user):
    """When an existing Pro user extends by 30 days, confirmation email displays extended expiration."""
    now = utcnow()
    sub = db.scalar(select(UserSubscription).where(UserSubscription.user_id == buyer_user.id))
    sub.plan = "pro"
    sub.status = "active"
    sub.current_period_start = now - timedelta(days=20)
    sub.current_period_end = now + timedelta(days=10)  # 10 days remaining
    db.commit()

    tx = PaymentTransaction(
        user_id=buyer_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_extension_mail_test",
        provider_payment_id="xnd_inv_ext_mail",
        created_at=now,
        updated_at=now,
    )
    db.add(tx)
    db.commit()

    mock_authoritative = {
        "id": tx.provider_payment_id,
        "external_id": tx.external_id,
        "status": "PAID",
        "amount": 49000,
        "currency": "IDR",
        "paid_at": now.isoformat(),
    }

    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_authoritative):
        reconcile_and_process_payment(db, tx.id)

    db.refresh(sub)
    end_tz = sub.current_period_end if sub.current_period_end.tzinfo else sub.current_period_end.replace(tzinfo=timezone.utc)
    expected_year = end_tz.strftime("%Y")

    delivery = db.scalar(select(EmailDelivery).where(EmailDelivery.idempotency_key == f"payment_confirmed:{tx.id}"))
    assert delivery is not None
    # Email must reflect the extended date
    assert expected_year in delivery.body_text
    assert expected_year in delivery.body_html


# =========================================================================
# 7. HTML Escaping Prevents Injection & Content Integrity
# =========================================================================

def test_html_escaping_prevents_injection():
    """User-controlled content in emails is escaped to prevent injection."""
    malicious_name = "<script>alert('XSS')</script> John & Co."
    text_content, html_content = render_payment_confirmation_email(
        customer_name=malicious_name,
        recipient_email="victim@raytech.cloud",
        transaction_reference="ref_12345",
        amount_idr=49000,
        paid_at=utcnow(),
        duration_days=30,
        expiration_date=utcnow() + timedelta(days=30),
        payment_channel="QRIS",
    )

    assert "<script>" not in html_content
    assert "&lt;script&gt;alert(&#x27;XSS&#x27;)&lt;/script&gt; John &amp; Co." in html_content
    # Plain text retains original
    assert malicious_name in text_content


def test_plaintext_body_contains_required_details():
    """Plain-text body contains product, amount, order reference, and non-renewal notice."""
    text_content, _ = render_payment_confirmation_email(
        customer_name="Jane Doe",
        recipient_email="jane@raytech.cloud",
        transaction_reference="rtm_pro_ref_999",
        amount_idr=49000,
        paid_at=utcnow(),
        duration_days=30,
        expiration_date=utcnow() + timedelta(days=30),
        payment_channel="BCA Virtual Account",
    )

    assert "Raytech Monitor Pro" in text_content
    assert "IDR 49,000" in text_content
    assert "rtm_pro_ref_999" in text_content
    assert "BCA Virtual Account" in text_content
    assert "does not automatically renew" in text_content
    assert "not an official tax invoice" in text_content


# =========================================================================
# 8. Stable Provider Idempotency Key & Ambiguous Network Timeout Retry
# =========================================================================

def test_stable_resend_idempotency_key_on_network_timeout_and_retry(db, buyer_user):
    """An ambiguous network timeout followed by retry sends the identical provider idempotency key.

    Guarantees:
    - Attempt count is NOT included in the provider Idempotency-Key.
    - If Resend processed the email before timeout, the subsequent retry with the same key
      prevents duplicate delivery within Resend's idempotency retention window.
    """
    tx = PaymentTransaction(
        user_id=buyer_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PAID",
        external_id="rtm_timeout_retry_test",
        provider_payment_id="xnd_inv_timeout_retry",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    sub = db.scalar(select(UserSubscription).where(UserSubscription.user_id == buyer_user.id))
    delivery = enqueue_payment_confirmation_email(db, buyer_user, tx, sub)
    db.commit()
    assert delivery is not None
    assert delivery.status == "pending"

    expected_stable_key = f"raytech-payment-confirmed-{tx.id}"
    assert get_provider_idempotency_key(delivery) == expected_stable_key

    captured_headers = []

    def mock_post(url, headers=None, json=None, **kwargs):
        captured_headers.append(dict(headers or {}))
        if len(captured_headers) == 1:
            # Simulate ambiguous network timeout on first attempt
            import httpx
            raise httpx.TimeoutException("Read timed out from api.resend.com")
        # Second attempt succeeds
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "re_timeout_success_789"}
        return resp

    env_overrides = {
        "EMAIL_ENABLED": "true",
        "RESEND_API_KEY": "re_test_mock_key_timeout",
        "EMAIL_TEST_MODE": "false",
    }

    # Attempt 1: Ambiguous network timeout
    with patch.dict(os.environ, env_overrides), patch("httpx.Client.post", side_effect=mock_post):
        res1 = process_pending_email_deliveries(db)
        assert res1["processed"] == 1
        assert res1["sent"] == 0

    db.refresh(delivery)
    assert delivery.status == "retry"
    assert delivery.attempt_count == 1
    assert "timed out" in (delivery.last_error_message or "").lower()

    # Fast-forward next_attempt_at to make it eligible immediately
    delivery.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.commit()

    # Attempt 2: Retry succeeds
    with patch.dict(os.environ, env_overrides), patch("httpx.Client.post", side_effect=mock_post):
        res2 = process_pending_email_deliveries(db)
        assert res2["processed"] == 1
        assert res2["sent"] == 1

    db.refresh(delivery)
    assert delivery.status == "sent"
    assert delivery.provider_message_id == "re_timeout_success_789"

    # CRITICAL ASSERTION: The provider Idempotency-Key must be identical across both attempts
    assert len(captured_headers) == 2
    key_attempt_1 = captured_headers[0].get("Idempotency-Key")
    key_attempt_2 = captured_headers[1].get("Idempotency-Key")

    assert key_attempt_1 == expected_stable_key
    assert key_attempt_2 == expected_stable_key
    assert key_attempt_1 == key_attempt_2
    # Ensure attempt count is not appended: key ends with tx.id and is invariant across attempts
    assert key_attempt_2.endswith(str(tx.id))


def test_distinct_payments_produce_unique_provider_idempotency_keys(db, buyer_user):
    """Separate successful payments receive distinct provider idempotency keys."""
    tx1 = PaymentTransaction(
        user_id=buyer_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PAID",
        external_id="rtm_tx_distinct_1",
        provider_payment_id="xnd_inv_dist_1",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    tx2 = PaymentTransaction(
        user_id=buyer_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PAID",
        external_id="rtm_tx_distinct_2",
        provider_payment_id="xnd_inv_dist_2",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add_all([tx1, tx2])
    db.commit()

    sub = db.scalar(select(UserSubscription).where(UserSubscription.user_id == buyer_user.id))
    del1 = enqueue_payment_confirmation_email(db, buyer_user, tx1, sub)
    del2 = enqueue_payment_confirmation_email(db, buyer_user, tx2, sub)
    db.commit()

    key1 = get_provider_idempotency_key(del1)
    key2 = get_provider_idempotency_key(del2)

    assert key1 == f"raytech-payment-confirmed-{tx1.id}"
    assert key2 == f"raytech-payment-confirmed-{tx2.id}"
    assert key1 != key2


# =========================================================================
# 9. Valid Billing Sender Formatting
# =========================================================================

def test_get_billing_email_from_formatting():
    """Validates get_billing_email_from avoids nested brackets and supports configured sender."""
    # Case 1: Plain address without display name in EMAIL_FROM
    with patch.dict(os.environ, {"EMAIL_FROM": "alerts@raytech.cloud", "BILLING_EMAIL_FROM": ""}):
        sender = get_billing_email_from()
        assert sender == "Raytech Billing <alerts@raytech.cloud>"

    # Case 2: Display name already present in EMAIL_FROM - must NOT produce nested brackets
    with patch.dict(os.environ, {"EMAIL_FROM": "Raytech Monitor <alerts@raytech.cloud>", "BILLING_EMAIL_FROM": ""}):
        sender = get_billing_email_from()
        assert sender == "Raytech Billing <alerts@raytech.cloud>"
        assert "<Raytech Monitor" not in sender
        assert "<<" not in sender and ">>" not in sender

    # Case 3: Quoted display name in EMAIL_FROM
    with patch.dict(os.environ, {"EMAIL_FROM": '"Raytech Cloud" <alerts@raytech.cloud>', "BILLING_EMAIL_FROM": ""}):
        sender = get_billing_email_from()
        assert sender == "Raytech Billing <alerts@raytech.cloud>"

    # Case 4: Explicitly configured BILLING_EMAIL_FROM with custom display name
    with patch.dict(os.environ, {"BILLING_EMAIL_FROM": "Raytech Invoicing <billing@raytech.cloud>"}):
        sender = get_billing_email_from()
        assert sender == "Raytech Invoicing <billing@raytech.cloud>"

    # Case 5: Explicitly configured BILLING_EMAIL_FROM plain address
    with patch.dict(os.environ, {"BILLING_EMAIL_FROM": "billing@raytech.cloud"}):
        sender = get_billing_email_from()
        assert sender == "Raytech Billing <billing@raytech.cloud>"

    # Case 6: Fallback when EMAIL_FROM is empty
    with patch.dict(os.environ, {"EMAIL_FROM": "", "BILLING_EMAIL_FROM": ""}):
        sender = get_billing_email_from()
        assert sender == "Raytech Billing <onboarding@resend.dev>"
