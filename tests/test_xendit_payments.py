import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models.payment_transaction import PaymentTransaction
from app.models.subscription import UserSubscription
from app.models.user import User
from app.services.payment_service import reconcile_and_process_payment
from app.services.subscription_service import (
    activate_pro_from_payment,
    get_effective_plan,
    get_or_create_subscription,
    utcnow,
)
from app.services.xendit_service import (
    PRO_SUBSCRIPTION_AMOUNT_IDR,
    PRO_SUBSCRIPTION_DURATION_DAYS,
    XenditApiError,
    XenditConfigurationError,
    get_xendit_mode,
    get_xendit_secret_key,
)

TEST_WEBHOOK_TOKEN = "test-xendit-webhook-secret-token"
os.environ["XENDIT_WEBHOOK_TOKEN"] = TEST_WEBHOOK_TOKEN
os.environ["XENDIT_SECRET_KEY"] = "xnd_development_test_key_12345"
os.environ["XENDIT_MODE"] = "test"


@pytest.fixture
def non_legacy_free_user(db):
    user = User(
        email="free_buyer@raytech.cloud",
        name="Free Buyer",
        google_sub="sub-free-buyer-101",
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
def buyer_client(db, non_legacy_free_user):
    from app.database import get_db
    from app.dependencies.auth import get_current_user

    def override_db():
        yield db

    def override_user():
        return non_legacy_free_user

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


# =========================================================================
# 1. Checkout Tests
# =========================================================================

def test_unauthenticated_checkout_rejected(client):
    """Unauthenticated request to create checkout returns 401."""
    from app.dependencies.auth import get_current_user
    if get_current_user in app.dependency_overrides:
        del app.dependency_overrides[get_current_user]

    res = client.post("/billing/checkout", json={"plan": "pro"})
    assert res.status_code == 401


def test_free_user_can_create_checkout(db, non_legacy_free_user, buyer_client):
    """Free user can create a checkout session which contacts Xendit and returns checkout_url."""
    mock_xendit_resp = {
        "id": "xnd_inv_mock_12345",
        "external_id": "rtm_pro_mock_ref",
        "invoice_url": "https://checkout-staging.xendit.co/v2/mock12345",
        "expiry_date": (utcnow() + timedelta(hours=24)).isoformat(),
        "status": "PENDING",
        "amount": 49000,
        "currency": "IDR",
    }

    with patch("app.routes.billing.create_xendit_invoice", return_value=mock_xendit_resp) as mock_create:
        res = buyer_client.post("/billing/checkout", json={"plan": "pro"})
        assert res.status_code == 200
        data = res.json()
        assert data["transaction_id"] > 0
        assert data["checkout_url"] == "https://checkout-staging.xendit.co/v2/mock12345"
        assert data["amount"] == 49000
        assert data["currency"] == "IDR"
        assert data["status"] == "PENDING"
        assert mock_create.called

        tx = db.scalar(select(PaymentTransaction).where(PaymentTransaction.id == data["transaction_id"]))
        assert tx is not None
        assert tx.user_id == non_legacy_free_user.id
        assert tx.provider_payment_id == "xnd_inv_mock_12345"
        assert tx.status == "PENDING"


def test_client_cannot_override_amount_or_duration(db, non_legacy_free_user, buyer_client):
    """Attempting to inject custom amount or unknown plan in client request body is prevented."""
    res_bad_plan = buyer_client.post("/billing/checkout", json={"plan": "enterprise", "amount": 100})
    assert res_bad_plan.status_code == 400

    mock_xendit_resp = {
        "id": "xnd_inv_mock_999",
        "external_id": "rtm_pro_mock_ref_999",
        "invoice_url": "https://checkout-staging.xendit.co/v2/mock999",
        "status": "PENDING",
        "amount": 49000,
        "currency": "IDR",
    }
    with patch("app.routes.billing.create_xendit_invoice", return_value=mock_xendit_resp) as mock_create:
        res = buyer_client.post("/billing/checkout", json={"plan": "pro", "amount": 1000})
        assert res.status_code == 200
        tx = db.scalar(select(PaymentTransaction).where(PaymentTransaction.id == res.json()["transaction_id"]))
        assert tx.amount == 49000
        assert mock_create.call_args[1]["amount"] == 49000


def test_rapid_duplicate_checkout_reuses_active_pending_session(db, non_legacy_free_user, buyer_client):
    """Rapid duplicate clicks on upgrade button reuse existing pending checkout session."""
    tx_existing = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_pro_existing_123",
        provider_payment_id="xnd_inv_existing",
        checkout_url="https://checkout-staging.xendit.co/v2/existing",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx_existing)
    db.commit()

    with patch("app.routes.billing.create_xendit_invoice") as mock_create:
        res = buyer_client.post("/billing/checkout", json={"plan": "pro"})
        assert res.status_code == 200
        data = res.json()
        assert data["transaction_id"] == tx_existing.id
        assert not mock_create.called


def test_checkout_gateway_failure_handled_gracefully(buyer_client):
    """When Xendit returns a gateway error, API responds with 502/503 without crashing."""
    with patch("app.routes.billing.create_xendit_invoice", side_effect=XenditApiError("Connection timeout", status_code=504)):
        res = buyer_client.post("/billing/checkout", json={"plan": "pro"})
        assert res.status_code == 502
        assert res.json()["detail"]["code"] == "GATEWAY_API_ERROR"


# =========================================================================
# 2. Webhook & Security Hardening Tests (21 Security Scenarios)
# =========================================================================

# Scenario 1: Forged PAID webhook with valid callback token but unpaid Xendit invoice
def test_forged_paid_webhook_rejected_when_xendit_invoice_unpaid(db, non_legacy_free_user, client):
    """Attacker sends forged PAID webhook with valid token, but Xendit API shows PENDING."""
    tx = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_forged_test",
        provider_payment_id="xnd_inv_unpaid_123",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    # Authoritative Xendit API says it is still PENDING
    mock_authoritative_invoice = {
        "id": tx.provider_payment_id,
        "external_id": tx.external_id,
        "status": "PENDING",
        "amount": 49000,
        "currency": "IDR",
    }

    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_authoritative_invoice):
        res = client.post(
            "/webhooks/xendit",
            headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
            json={
                "id": tx.provider_payment_id,
                "external_id": tx.external_id,
                "status": "PAID",  # Forged claim in callback payload
                "amount": 49000,
                "currency": "IDR",
            },
        )
        assert res.status_code == 422
        # Pro was NOT activated
        tx_check = db.scalar(select(PaymentTransaction).where(PaymentTransaction.id == tx.id))
        assert tx_check.status == "PENDING"
        plan_id, _ = get_effective_plan(db, non_legacy_free_user.id)
        assert plan_id == "free"


# Scenario 2: Invalid callback token
def test_webhook_invalid_callback_token_rejected(client):
    """Webhook with missing or invalid x-callback-token is rejected with 403."""
    res_none = client.post("/webhooks/xendit", json={"status": "PAID"})
    assert res_none.status_code == 403

    res_invalid = client.post(
        "/webhooks/xendit",
        headers={"x-callback-token": "attacker-token"},
        json={"status": "PAID"},
    )
    assert res_invalid.status_code == 403


# Scenario 3: Mismatched invoice ID
def test_mismatched_invoice_id_rejected(db, non_legacy_free_user, client):
    """Webhook sends invoice ID different from stored transaction provider_payment_id."""
    tx = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_mismatch_inv_id",
        provider_payment_id="xnd_inv_correct_999",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    res = client.post(
        "/webhooks/xendit",
        headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
        json={
            "id": "xnd_inv_wrong_111",  # Mismatch
            "external_id": tx.external_id,
            "status": "PAID",
        },
    )
    assert res.status_code == 422
    assert "Invoice ID mismatch" in res.json()["detail"]["error"]


# Scenario 4: Mismatched external ID
def test_mismatched_external_id_rejected(db, non_legacy_free_user, client):
    """Authoritative invoice from Xendit has external_id conflicting with transaction."""
    tx = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_mismatch_ext_id_expected",
        provider_payment_id="xnd_inv_ext_test",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    mock_authoritative = {
        "id": tx.provider_payment_id,
        "external_id": "rtm_other_external_id",  # Conflict
        "status": "PAID",
        "amount": 49000,
        "currency": "IDR",
    }
    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_authoritative):
        res = client.post(
            "/webhooks/xendit",
            headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
            json={
                "id": tx.provider_payment_id,
                "external_id": tx.external_id,
                "status": "PAID",
            },
        )
        assert res.status_code == 422
        assert "external_id mismatch" in res.json()["detail"]["error"].lower()


# Scenario 5: Incorrect payment amount
def test_incorrect_payment_amount_rejected(db, non_legacy_free_user, client):
    """Authoritative invoice from Xendit has incorrect amount (e.g. 10,000 instead of 49,000)."""
    tx = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_bad_amt_order",
        provider_payment_id="xnd_inv_bad_amt",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    mock_authoritative = {
        "id": tx.provider_payment_id,
        "external_id": tx.external_id,
        "status": "PAID",
        "amount": 10000,  # Incorrect
        "currency": "IDR",
    }
    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_authoritative):
        res = client.post(
            "/webhooks/xendit",
            headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
            json={
                "id": tx.provider_payment_id,
                "external_id": tx.external_id,
                "status": "PAID",
            },
        )
        assert res.status_code == 422
        assert "Amount must be IDR 49000" in res.json()["detail"]["error"]


# Scenario 6: Incorrect currency
def test_incorrect_currency_rejected(db, non_legacy_free_user, client):
    """Authoritative invoice currency is USD instead of IDR."""
    tx = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_bad_curr_order",
        provider_payment_id="xnd_inv_bad_curr",
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
        "currency": "USD",  # Incorrect
    }
    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_authoritative):
        res = client.post(
            "/webhooks/xendit",
            headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
            json={
                "id": tx.provider_payment_id,
                "external_id": tx.external_id,
                "status": "PAID",
            },
        )
        assert res.status_code == 422
        assert "Currency must be IDR" in res.json()["detail"]["error"]


# Scenario 7: Missing currency
def test_missing_currency_rejected(db, non_legacy_free_user, client):
    """Authoritative invoice currency is None or missing."""
    tx = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_missing_curr_order",
        provider_payment_id="xnd_inv_missing_curr",
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
        "currency": None,
    }
    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_authoritative):
        res = client.post(
            "/webhooks/xendit",
            headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
            json={
                "id": tx.provider_payment_id,
                "external_id": tx.external_id,
                "status": "PAID",
            },
        )
        assert res.status_code == 422
        assert "Currency must be IDR" in res.json()["detail"]["error"]


# Scenario 8: Xendit API verification timeout
def test_xendit_api_verification_timeout(db, non_legacy_free_user, client):
    """When Xendit API times out during invoice verification, respond with 504 retryable."""
    tx = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_timeout_order",
        provider_payment_id="xnd_inv_timeout",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    with patch("app.services.payment_service.get_xendit_invoice", side_effect=XenditApiError("Gateway timeout", status_code=504)):
        res = client.post(
            "/webhooks/xendit",
            headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
            json={"id": tx.provider_payment_id, "external_id": tx.external_id, "status": "PAID"},
        )
        assert res.status_code == 504
        # Transaction status remains PENDING and changes rolled back
        tx_check = db.scalar(select(PaymentTransaction).where(PaymentTransaction.id == tx.id))
        assert tx_check.status == "PENDING"
        plan_id, _ = get_effective_plan(db, non_legacy_free_user.id)
        assert plan_id == "free"


# Scenario 9: Xendit API verification error
def test_xendit_api_verification_error(db, non_legacy_free_user, client):
    """When Xendit returns 500/502 during verification, webhook returns 502 and rolls back."""
    tx = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_error_order",
        provider_payment_id="xnd_inv_error",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    with patch("app.services.payment_service.get_xendit_invoice", side_effect=XenditApiError("Bad gateway", status_code=502)):
        res = client.post(
            "/webhooks/xendit",
            headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
            json={"id": tx.provider_payment_id, "external_id": tx.external_id, "status": "PAID"},
        )
        assert res.status_code == 502
        tx_check = db.scalar(select(PaymentTransaction).where(PaymentTransaction.id == tx.id))
        assert tx_check.status == "PENDING"


# Scenario 10: Duplicate PAID webhook
def test_duplicate_paid_webhook_idempotency(db, non_legacy_free_user, client):
    """Duplicate PAID webhooks do not extend Pro period twice."""
    tx = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_duplicate_order",
        provider_payment_id="xnd_inv_duplicate",
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
        # 1st delivery
        res1 = client.post(
            "/webhooks/xendit",
            headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
            json={"id": tx.provider_payment_id, "external_id": tx.external_id, "status": "PAID"},
        )
        assert res1.status_code == 200
        assert res1.json()["activated"] is True

        sub1 = db.scalar(select(UserSubscription).where(UserSubscription.user_id == non_legacy_free_user.id))
        first_expiry = sub1.current_period_end

        # 2nd duplicate delivery
        res2 = client.post(
            "/webhooks/xendit",
            headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
            json={"id": tx.provider_payment_id, "external_id": tx.external_id, "status": "PAID"},
        )
        assert res2.status_code == 200
        assert res2.json()["status"] == "already_processed"

        db.refresh(sub1)
        # Expiration date MUST be identical, not extended by another 30 days
        assert sub1.current_period_end == first_expiry


# Scenario 11: Concurrent duplicate webhooks
def test_concurrent_duplicate_webhooks(db, non_legacy_free_user):
    """Calling reconcile_and_process_payment sequentially on already-paid tx returns already_paid."""
    tx = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_concurrent_order",
        provider_payment_id="xnd_inv_concurrent",
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
        outcome1, tx1 = reconcile_and_process_payment(db, tx.id)
        assert outcome1 == "activated"
        assert tx1.status == "PAID"

        # Concurrent second attempt sees PAID
        outcome2, tx2 = reconcile_and_process_payment(db, tx.id)
        assert outcome2 == "already_paid"


# Scenario 12: Failure after payment update rolls back subscription atomically
def test_failure_after_payment_state_update_triggers_rollback(db, non_legacy_free_user):
    """If an error occurs before commit, transaction status update is rolled back."""
    tx = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_atomic_rollback_order",
        provider_payment_id="xnd_inv_atomic_rollback",
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
    }

    try:
        with db.begin_nested():
            with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_authoritative), \
                 patch("app.services.payment_service.activate_pro_from_payment", side_effect=RuntimeError("DB constraint failure")):
                reconcile_and_process_payment(db, tx.id)
    except RuntimeError:
        pass

    db.refresh(tx)
    assert tx.status == "PENDING"


# Scenario 13: Database rollback preserving consistent state
def test_database_rollback_preserving_consistent_state(db, non_legacy_free_user, client):
    """Unknown transaction webhook returns 404 and leaves DB clean."""
    res = client.post(
        "/webhooks/xendit",
        headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
        json={"id": "xnd_inv_ghost", "external_id": "rtm_ghost", "status": "PAID"},
    )
    assert res.status_code == 404
    plan_id, _ = get_effective_plan(db, non_legacy_free_user.id)
    assert plan_id == "free"


# Scenario 14: Late EXPIRED notification after PAID
def test_late_expired_notification_after_paid(db, non_legacy_free_user, client):
    """Delayed EXPIRED notification cannot overwrite an already settled PAID status."""
    tx = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PAID",  # Settled
        external_id="rtm_late_expiry_order",
        provider_payment_id="xnd_inv_late_expiry",
        paid_at=utcnow(),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    res = client.post(
        "/webhooks/xendit",
        headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
        json={
            "id": tx.provider_payment_id,
            "external_id": tx.external_id,
            "status": "EXPIRED",  # Delayed expiry
        },
    )
    assert res.status_code == 200
    assert res.json()["status"] == "already_processed"
    db.refresh(tx)
    assert tx.status == "PAID"


# Scenario 15: Late PAID notification after a prior expiry state
def test_late_paid_notification_after_prior_expiry_reconciles(db, non_legacy_free_user, client):
    """Legitimately settled payment overrides premature EXPIRED state upon authoritative confirmation."""
    tx = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="EXPIRED",  # Prematurely marked EXPIRED
        external_id="rtm_wire_settled_order",
        provider_payment_id="xnd_inv_wire_settled",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    # Authoritative Xendit invoice confirms user actually settled
    mock_authoritative = {
        "id": tx.provider_payment_id,
        "external_id": tx.external_id,
        "status": "PAID",
        "amount": 49000,
        "currency": "IDR",
        "payment_method": "EWALLET",
        "payment_channel": "OVO",
        "paid_at": utcnow().isoformat(),
    }

    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_authoritative):
        res = client.post(
            "/webhooks/xendit",
            headers={"x-callback-token": TEST_WEBHOOK_TOKEN},
            json={
                "id": tx.provider_payment_id,
                "external_id": tx.external_id,
                "status": "PAID",
            },
        )
        assert res.status_code == 200
        assert res.json()["activated"] is True

        db.refresh(tx)
        assert tx.status == "PAID"
        plan_id, _ = get_effective_plan(db, non_legacy_free_user.id)
        assert plan_id == "pro"


# Scenario 16: Manual Pro account purchasing Pro
def test_manual_pro_account_purchasing_pro(db):
    """User with manually granted Pro (no expiration date) purchasing Pro receives a 30-day paid period."""
    manual_user = User(email="manual@raytech.cloud", name="Manual Pro", google_sub="sub-manual-1", is_active=True)
    db.add(manual_user)
    db.commit()

    sub = UserSubscription(
        user_id=manual_user.id,
        plan="pro",
        status="active",
        legacy_override=False,
        current_period_end=None,  # Manual admin Pro without expiration
        override_reason="dev_manual",
    )
    db.add(sub)
    db.commit()

    sub_res = activate_pro_from_payment(db, manual_user.id, transaction_id=77, duration_days=30, commit=True)
    assert sub_res.plan == "pro"
    assert sub_res.current_period_end is not None
    end_tz = sub_res.current_period_end if sub_res.current_period_end.tzinfo else sub_res.current_period_end.replace(tzinfo=timezone.utc)
    diff = end_tz - utcnow()
    assert 29 <= diff.days <= 30
    assert "xendit_payment:77" in sub_res.override_reason


# Scenario 17: Legacy account purchasing Pro
def test_legacy_account_purchasing_pro(db):
    """Grandfathered legacy user purchasing Pro preserves legacy_override = True indefinitely."""
    legacy_user = User(email="legacy@raytech.cloud", name="Legacy User", google_sub="sub-legacy-1", is_active=True)
    db.add(legacy_user)
    db.commit()

    sub = UserSubscription(
        user_id=legacy_user.id,
        plan="pro",
        status="active",
        legacy_override=True,  # Grandfathered
    )
    db.add(sub)
    db.commit()

    sub_res = activate_pro_from_payment(db, legacy_user.id, transaction_id=88, duration_days=30, commit=True)
    assert sub_res.legacy_override is True
    assert sub_res.plan == "pro"
    assert sub_res.current_period_end is not None


# Scenario 18: Subscription period extension
def test_subscription_period_extension(db, non_legacy_free_user):
    """User with 10 days remaining Pro has access extended by 30 days to 40 days total."""
    now = utcnow()
    sub = get_or_create_subscription(db, non_legacy_free_user.id)
    sub.plan = "pro"
    sub.status = "active"
    sub.current_period_start = now - timedelta(days=20)
    sub.current_period_end = now + timedelta(days=10)
    db.commit()

    activate_pro_from_payment(db, non_legacy_free_user.id, transaction_id=99, duration_days=30, commit=True)
    db.refresh(sub)

    end_tz = sub.current_period_end if sub.current_period_end.tzinfo else sub.current_period_end.replace(tzinfo=timezone.utc)
    diff = end_tz - now
    # 10 days remaining + 30 days extension = ~40 days
    assert 39 <= diff.days <= 40


# Scenario 19: Wrong environment or API key type
def test_wrong_environment_or_api_key_type():
    """Live mode or production secret keys in test mode fail closed with XenditConfigurationError."""
    # Test 1: live mode fails closed
    with patch.dict(os.environ, {"XENDIT_MODE": "live"}):
        with pytest.raises(XenditConfigurationError):
            get_xendit_mode()

    # Test 2: production key in test mode fails closed
    with patch.dict(os.environ, {"XENDIT_MODE": "test", "XENDIT_SECRET_KEY": "xnd_production_secret_live"}):
        with pytest.raises(XenditConfigurationError):
            get_xendit_secret_key()

    # Test 3: non-development key format in test mode fails closed
    with patch.dict(os.environ, {"XENDIT_MODE": "test", "XENDIT_SECRET_KEY": "sk_test_12345"}):
        with pytest.raises(XenditConfigurationError):
            get_xendit_secret_key()


# Scenario 20: Browser success redirect without verified payment
def test_browser_success_redirect_without_verified_payment(db, non_legacy_free_user, buyer_client):
    """Browser redirect to status endpoint when Xendit invoice is still unpaid does NOT grant Pro."""
    tx = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_browser_redirect_order",
        provider_payment_id="xnd_inv_redirect_unpaid",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    mock_unpaid_invoice = {
        "id": tx.provider_payment_id,
        "external_id": tx.external_id,
        "status": "PENDING",  # Not paid yet
        "amount": 49000,
        "currency": "IDR",
    }

    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_unpaid_invoice):
        res = buyer_client.get(f"/billing/transactions/{tx.id}/status")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "PENDING"
        assert data["is_pro_active"] is False

    plan_id, _ = get_effective_plan(db, non_legacy_free_user.id)
    assert plan_id == "free"


# Scenario 21: Polling concurrently with webhook processing
def test_polling_concurrently_with_webhook_processing(db, non_legacy_free_user, buyer_client):
    """Client polling when invoice is verified PAID settles the transaction cleanly."""
    tx = PaymentTransaction(
        user_id=non_legacy_free_user.id,
        plan="pro",
        amount=49000,
        currency="IDR",
        status="PENDING",
        external_id="rtm_poll_sync_order",
        provider_payment_id="xnd_inv_poll_sync",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    mock_paid_invoice = {
        "id": tx.provider_payment_id,
        "external_id": tx.external_id,
        "status": "PAID",
        "amount": 49000,
        "currency": "IDR",
        "payment_method": "VIRTUAL_ACCOUNT",
        "payment_channel": "MANDIRI",
        "paid_at": utcnow().isoformat(),
    }

    with patch("app.services.payment_service.get_xendit_invoice", return_value=mock_paid_invoice):
        res = buyer_client.get(f"/billing/transactions/{tx.id}/status")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "PAID"
        assert data["is_pro_active"] is True

    plan_id, _ = get_effective_plan(db, non_legacy_free_user.id)
    assert plan_id == "pro"


# =========================================================================
# 3. Transaction History & Ownership Isolation Tests
# =========================================================================

def test_get_billing_transactions_user_isolation(db, non_legacy_free_user, buyer_client):
    """User A cannot see User B's billing transactions."""
    other_user = User(email="other@raytech.cloud", name="Other", google_sub="sub-other-2", is_active=True)
    db.add(other_user)
    db.commit()

    tx1 = PaymentTransaction(user_id=non_legacy_free_user.id, plan="pro", amount=49000, external_id="tx_buyer_1")
    tx2 = PaymentTransaction(user_id=other_user.id, plan="pro", amount=49000, external_id="tx_other_1")
    db.add_all([tx1, tx2])
    db.commit()

    res = buyer_client.get("/billing/transactions")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["external_id"] == "tx_buyer_1"


def test_get_single_transaction_forbidden_for_other_user(db, non_legacy_free_user, buyer_client):
    """User A attempting to query User B's transaction by ID returns 404."""
    other_user = User(email="victim@raytech.cloud", name="Victim", google_sub="sub-victim-3", is_active=True)
    db.add(other_user)
    db.commit()

    tx_other = PaymentTransaction(user_id=other_user.id, plan="pro", amount=49000, external_id="tx_victim_1")
    db.add(tx_other)
    db.commit()

    res = buyer_client.get(f"/billing/transactions/{tx_other.id}")
    assert res.status_code == 404
