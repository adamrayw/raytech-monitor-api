import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models.email_delivery import EmailDelivery
from app.models.payment_transaction import PaymentTransaction
from app.models.subscription import UserSubscription
from app.models.user import User
from app.services.duitku_service import (
    PRO_SUBSCRIPTION_AMOUNT_IDR,
    PRO_SUBSCRIPTION_DURATION_DAYS,
    DuitkuApiError,
    DuitkuConfigurationError,
    create_duitku_transaction,
    generate_check_transaction_signature,
    generate_duitku_order_id,
    generate_inquiry_signature,
    get_duitku_api_key,
    get_duitku_merchant_code,
    get_duitku_mode,
    get_duitku_transaction_status,
    verify_duitku_callback_signature,
)
from app.services.payment_service import reconcile_and_process_payment
from app.services.subscription_service import activate_pro_from_payment, ensure_utc, get_effective_plan, utcnow

TEST_MERCHANT_CODE = "D12345"
TEST_API_KEY = "test_duitku_api_key_secret_12345678"

os.environ["DUITKU_MODE"] = "sandbox"
os.environ["DUITKU_MERCHANT_CODE"] = TEST_MERCHANT_CODE
os.environ["DUITKU_API_KEY"] = TEST_API_KEY
os.environ["SANDBOX_CHECKOUT_ALLOWLIST"] = "@raytech.cloud,test_buyer@raytech.cloud"


@pytest.fixture
def duitku_user(db):
    user = User(
        email="duitku_buyer@raytech.cloud",
        name="Duitku Tester",
        google_sub="sub-duitku-buyer-001",
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
def duitku_client(db, duitku_user):
    from app.database import get_db
    from app.dependencies.auth import get_current_user

    def override_db():
        yield db

    def override_user():
        return duitku_user

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


# =========================================================================
# 1. Signature & Core Duitku Service Specification Tests
# =========================================================================

def test_duitku_mode_enforcement():
    """Live mode is strictly blocked; fail closed with DuitkuConfigurationError."""
    with patch.dict(os.environ, {"DUITKU_MODE": "production"}):
        with pytest.raises(DuitkuConfigurationError) as exc:
            get_duitku_mode()
        assert "strictly disabled" in str(exc.value)

    with patch.dict(os.environ, {"DUITKU_MODE": "sandbox"}):
        assert get_duitku_mode() == "sandbox"


def test_inquiry_signature_computation():
    """Inquiry signature follows HMAC-SHA256(merchantCode + merchantOrderId + paymentAmount, apiKey)."""
    m_code = "D12345"
    order_id = "dtk_order_001"
    amount = 49000
    key = "secret_key"

    expected = hmac.new(
        key.encode("utf-8"),
        f"{m_code}{order_id}{amount}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    computed = generate_inquiry_signature(m_code, order_id, amount, key)
    assert computed == expected


def test_check_transaction_signature_computation():
    """Check transaction signature follows HMAC-SHA256(merchantCode + merchantOrderId, apiKey)."""
    m_code = "D12345"
    order_id = "dtk_order_001"
    key = "secret_key"

    expected = hmac.new(
        key.encode("utf-8"),
        f"{m_code}{order_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    computed = generate_check_transaction_signature(m_code, order_id, key)
    assert computed == expected


def test_callback_signature_verification_success_and_failure():
    """Callback signature verification verifies HMAC-SHA256(merchantCode + amount + merchantOrderId, apiKey)."""
    m_code = TEST_MERCHANT_CODE
    order_id = "dtk_pro_1001_1720000_abc"
    amount = 49000

    valid_sig = hmac.new(
        TEST_API_KEY.encode("utf-8"),
        f"{m_code}{amount}{order_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    # Valid signature
    assert verify_duitku_callback_signature(m_code, amount, order_id, valid_sig) is True

    # Bad signature
    assert verify_duitku_callback_signature(m_code, amount, order_id, "forged_signature_1234") is False

    # Mismatched amount
    bad_amount_sig = hmac.new(
        TEST_API_KEY.encode("utf-8"),
        f"{m_code}10000{order_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert verify_duitku_callback_signature(m_code, amount, order_id, bad_amount_sig) is False


# =========================================================================
# 2. Checkout Creation & Idempotency Tests
# =========================================================================

def test_unauthenticated_checkout_rejected(client):
    """Unauthenticated user cannot create checkout."""
    from app.dependencies.auth import get_current_user
    if get_current_user in app.dependency_overrides:
        del app.dependency_overrides[get_current_user]

    res = client.post("/billing/checkout", json={"plan": "pro", "provider": "duitku"})
    assert res.status_code == 401


def test_duitku_checkout_creation(db, duitku_user, duitku_client):
    """Authenticated user creates Duitku checkout; returns transaction_id, paymentUrl, qr_string."""
    mock_duitku_resp = {
        "statusCode": "00",
        "statusMessage": "SUCCESS",
        "reference": "DTK_REF_MOCK_999",
        "paymentUrl": "https://sandbox.duitku.com/topup/topupdirectv2.aspx?ref=MOCK_999",
        "qrString": "00020101021226660014ID.DANA.WWW011893600911002151500102152006170915150010303UME",
        "amount": "49000",
    }

    with patch("app.routes.billing.create_duitku_transaction", return_value=mock_duitku_resp) as mock_create:
        res = duitku_client.post(
            "/billing/checkout",
            json={"plan": "pro", "provider": "duitku", "payment_method": "SP"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["provider"] == "duitku"
        assert body["provider_mode"] == "sandbox"
        assert body["amount"] == 49000
        assert body["currency"] == "IDR"
        assert body["checkout_url"] == mock_duitku_resp["paymentUrl"]
        assert body["qr_string"] == mock_duitku_resp["qrString"]
        assert body["status"] == "PENDING"

        # Verify database record
        tx = db.scalar(select(PaymentTransaction).where(PaymentTransaction.id == body["transaction_id"]))
        assert tx is not None
        assert tx.user_id == duitku_user.id
        assert tx.provider == "duitku"
        assert tx.provider_mode == "sandbox"
        assert tx.provider_payment_id == "DTK_REF_MOCK_999"
        assert tx.qr_string == mock_duitku_resp["qrString"]


def test_duitku_checkout_rapid_click_idempotency(db, duitku_user, duitku_client):
    """Rapid repeated clicks reuse the active pending checkout within 15 minutes."""
    mock_duitku_resp = {
        "statusCode": "00",
        "statusMessage": "SUCCESS",
        "reference": "DTK_REF_IDEMPOTENT_1",
        "paymentUrl": "https://sandbox.duitku.com/topup/topupdirectv2.aspx?ref=IDEM_1",
        "qrString": "mock_qr_string",
        "amount": "49000",
    }

    with patch("app.routes.billing.create_duitku_transaction", return_value=mock_duitku_resp) as mock_create:
        res1 = duitku_client.post("/billing/checkout", json={"plan": "pro", "provider": "duitku"})
        assert res1.status_code == 200
        tx1_id = res1.json()["transaction_id"]

        # Immediate second click
        res2 = duitku_client.post("/billing/checkout", json={"plan": "pro", "provider": "duitku"})
        assert res2.status_code == 200
        tx2_id = res2.json()["transaction_id"]

        # Should be the identical transaction
        assert tx1_id == tx2_id
        # Duitku create function called only once
        assert mock_create.call_count == 1


def test_sandbox_allowlist_protection(db):
    """Non-allowlisted public accounts are blocked from Sandbox checkout with 403."""
    from app.database import get_db
    from app.dependencies.auth import get_current_user

    public_user = User(
        email="public_stranger@gmail.com",
        name="Public Stranger",
        google_sub="sub-public-stranger",
        is_active=True,
    )
    db.add(public_user)
    db.commit()

    def override_db():
        yield db

    def override_user():
        return public_user

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user

    with patch.dict(os.environ, {"SANDBOX_CHECKOUT_ALLOWLIST": "@raytech.cloud"}):
        with TestClient(app) as test_client:
            res = test_client.post("/billing/checkout", json={"plan": "pro", "provider": "duitku"})
            assert res.status_code == 403
            assert res.json()["detail"]["code"] == "SANDBOX_CHECKOUT_RESTRICTED"

    app.dependency_overrides.clear()


# =========================================================================
# 3. Webhook Callback & Authoritative Verification Tests
# =========================================================================

def test_unauthorized_callback_rejected(client):
    """Callback with invalid signature is rejected with HTTP 403."""
    res = client.post(
        "/webhooks/duitku",
        data={
            "merchantCode": TEST_MERCHANT_CODE,
            "amount": "49000",
            "merchantOrderId": "dtk_fake_order_123",
            "signature": "invalid_forged_sig",
            "resultCode": "00",
        },
    )
    assert res.status_code == 403
    assert "Invalid or missing callback signature" in res.json()["detail"]["error"]


def test_valid_callback_triggers_authoritative_reconciliation_and_activates_pro(db, duitku_user, client):
    """Authoritative Duitku callback settles transaction, activates Pro for 30 days, queues email."""
    # 1. Create pending transaction
    order_id = generate_duitku_order_id(duitku_user.id)
    tx = PaymentTransaction(
        user_id=duitku_user.id,
        plan="pro",
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="duitku",
        provider_mode="sandbox",
        external_id=order_id,
        provider_payment_id="DTK_REF_SETTLED_001",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    # 2. Compute authentic callback signature
    sig = hmac.new(
        TEST_API_KEY.encode("utf-8"),
        f"{TEST_MERCHANT_CODE}49000{order_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    # 3. Mock authoritative Duitku Check Transaction API response
    mock_status_resp = {
        "merchantOrderId": order_id,
        "reference": "DTK_REF_SETTLED_001",
        "amount": "49000",
        "fee": "0.00",
        "statusCode": "00",  # Success
        "statusMessage": "SUCCESS",
    }

    with patch("app.services.payment_service.get_duitku_transaction_status", return_value=mock_status_resp):
        # Invoke callback endpoint with x-www-form-urlencoded
        res = client.post(
            "/webhooks/duitku",
            data={
                "merchantCode": TEST_MERCHANT_CODE,
                "amount": "49000",
                "merchantOrderId": order_id,
                "productDetail": "Raytech Monitor Pro (30 Days)",
                "paymentCode": "SP",
                "resultCode": "00",
                "reference": "DTK_REF_SETTLED_001",
                "signature": sig,
            },
        )
        assert res.status_code == 200
        assert res.text == "SUCCESS"

    # 4. Verify transaction settled as PAID
    db.refresh(tx)
    assert tx.status == "PAID"
    assert tx.paid_at is not None
    assert tx.payment_method == "SP"

    # 5. Verify Pro subscription activated for 30 days
    plan, sub = get_effective_plan(db, duitku_user.id)
    assert plan == "pro"
    assert sub.status == "active"
    assert sub.current_period_end is not None

    # 6. Verify transactional billing email was enqueued
    delivery = db.scalar(
        select(EmailDelivery)
        .where(EmailDelivery.user_id == duitku_user.id)
        .order_by(EmailDelivery.id.desc())
    )
    assert delivery is not None
    assert delivery.event_type == "payment_confirmed"
    assert delivery.status == "pending"


def test_forged_paid_callback_rejected_when_check_transaction_still_pending(db, duitku_user, client):
    """If callback claims resultCode=00 but authoritative API returns statusCode=01 (Pending), reject!"""
    order_id = generate_duitku_order_id(duitku_user.id)
    tx = PaymentTransaction(
        user_id=duitku_user.id,
        plan="pro",
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="duitku",
        provider_mode="sandbox",
        external_id=order_id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    sig = hmac.new(
        TEST_API_KEY.encode("utf-8"),
        f"{TEST_MERCHANT_CODE}49000{order_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    mock_status_resp = {
        "merchantOrderId": order_id,
        "reference": "DTK_REF_PENDING_001",
        "amount": "49000",
        "statusCode": "01",  # Authoritatively still PENDING
        "statusMessage": "PENDING",
    }

    with patch("app.services.payment_service.get_duitku_transaction_status", return_value=mock_status_resp):
        res = client.post(
            "/webhooks/duitku",
            data={
                "merchantCode": TEST_MERCHANT_CODE,
                "amount": "49000",
                "merchantOrderId": order_id,
                "resultCode": "00",  # Forged claims success
                "signature": sig,
            },
        )
        assert res.status_code == 422
        assert "still PENDING" in res.json()["detail"]["error"]

    db.refresh(tx)
    assert tx.status == "PENDING"
    plan, _ = get_effective_plan(db, duitku_user.id)
    assert plan == "free"


def test_authoritative_amount_mismatch_rejected(db, duitku_user, client):
    """If authoritative transaction amount is not IDR 49,000, reject with 422."""
    order_id = generate_duitku_order_id(duitku_user.id)
    tx = PaymentTransaction(
        user_id=duitku_user.id,
        plan="pro",
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="duitku",
        provider_mode="sandbox",
        external_id=order_id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    sig = hmac.new(
        TEST_API_KEY.encode("utf-8"),
        f"{TEST_MERCHANT_CODE}49000{order_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    mock_status_resp = {
        "merchantOrderId": order_id,
        "reference": "DTK_REF_MISMATCH",
        "amount": "10000",  # Mismatch: paid only 10,000 instead of 49,000
        "statusCode": "00",
        "statusMessage": "SUCCESS",
    }

    with patch("app.services.payment_service.get_duitku_transaction_status", return_value=mock_status_resp):
        res = client.post(
            "/webhooks/duitku",
            data={
                "merchantCode": TEST_MERCHANT_CODE,
                "amount": "49000",
                "merchantOrderId": order_id,
                "resultCode": "00",
                "signature": sig,
            },
        )
        assert res.status_code == 422
        assert "Amount must be IDR" in res.json()["detail"]["error"]

    db.refresh(tx)
    assert tx.status == "PENDING"


def test_duplicate_callback_idempotency(db, duitku_user, client):
    """Duplicate callbacks for an already settled PAID transaction are handled safely without duplicate extension."""
    order_id = generate_duitku_order_id(duitku_user.id)
    now = utcnow()
    tx = PaymentTransaction(
        user_id=duitku_user.id,
        plan="pro",
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PAID",  # Already settled
        provider="duitku",
        provider_mode="sandbox",
        external_id=order_id,
        paid_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(tx)

    sub = db.scalar(select(UserSubscription).where(UserSubscription.user_id == duitku_user.id))
    if not sub:
        sub = UserSubscription(user_id=duitku_user.id)
        db.add(sub)
    sub.plan = "pro"
    sub.status = "active"
    sub.current_period_start = now
    sub.current_period_end = now + timedelta(days=30)
    db.commit()

    initial_end = sub.current_period_end

    sig = hmac.new(
        TEST_API_KEY.encode("utf-8"),
        f"{TEST_MERCHANT_CODE}49000{order_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    # Second callback delivery
    res = client.post(
        "/webhooks/duitku",
        data={
            "merchantCode": TEST_MERCHANT_CODE,
            "amount": "49000",
            "merchantOrderId": order_id,
            "resultCode": "00",
            "signature": sig,
        },
    )
    assert res.status_code == 200
    assert res.text == "SUCCESS"

    # End date must not be extended again
    db.refresh(sub)
    assert sub.current_period_end == initial_end


# =========================================================================
# 4. Status Polling & Billing History Tests
# =========================================================================

def test_polling_triggers_authoritative_sync(db, duitku_user, duitku_client):
    """Polling /billing/transactions/{id}/status authoritatively reconciles with Duitku."""
    order_id = generate_duitku_order_id(duitku_user.id)
    tx = PaymentTransaction(
        user_id=duitku_user.id,
        plan="pro",
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="duitku",
        provider_mode="sandbox",
        external_id=order_id,
        created_at=utcnow() - timedelta(seconds=15),
        updated_at=utcnow() - timedelta(seconds=15),
    )
    db.add(tx)
    db.commit()

    mock_status_resp = {
        "merchantOrderId": order_id,
        "reference": "DTK_REF_POLL_SUCCESS",
        "amount": "49000",
        "statusCode": "00",  # Settled
        "statusMessage": "SUCCESS",
    }

    with patch("app.services.payment_service.get_duitku_transaction_status", return_value=mock_status_resp):
        res = duitku_client.get(f"/billing/transactions/{tx.id}/status")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "PAID"
        assert data["is_pro_active"] is True
        assert data["provider"] == "duitku"
        assert data["provider_mode"] == "sandbox"


def test_transactions_history_returns_provider_fields(db, duitku_user, duitku_client):
    """Transaction history includes provider, provider_mode, and qr_string."""
    tx = PaymentTransaction(
        user_id=duitku_user.id,
        plan="pro",
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="duitku",
        provider_mode="sandbox",
        external_id="dtk_history_test_001",
        qr_string="mock_history_qr_string",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    res = duitku_client.get("/billing/transactions")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) >= 1
    found = next((item for item in items if item["external_id"] == "dtk_history_test_001"), None)
    assert found is not None
    assert found["provider"] == "duitku"
    assert found["provider_mode"] == "sandbox"
    assert found["qr_string"] == "mock_history_qr_string"


# =========================================================================
# 5. Part 10 Comprehensive Regression Tests
# =========================================================================

def test_qr_creation_does_not_activate_pro_or_set_paid_at(db, duitku_user, duitku_client):
    """Checkout QR generation MUST NOT activate Pro and MUST NOT populate paid_at."""
    mock_duitku_resp = {
        "statusCode": "00",
        "statusMessage": "SUCCESS",
        "reference": "DTK_REF_MOCK_QR_TEST",
        "paymentUrl": "https://sandbox.duitku.com/topup/topupdirectv2.aspx?ref=MOCK_QR",
        "qrString": "00020101021226660014ID.DANA.WWW011893600911002151500102152006170915150010303UME",
        "amount": "49000",
    }

    with patch("app.routes.billing.create_duitku_transaction", return_value=mock_duitku_resp):
        res = duitku_client.post("/billing/checkout", json={"plan": "pro", "provider": "duitku"})
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "PENDING"
        tx = db.scalar(select(PaymentTransaction).where(PaymentTransaction.id == body["transaction_id"]))
        assert tx.status == "PENDING"
        assert tx.paid_at is None

        # Verify user remains FREE
        effective_plan, sub = get_effective_plan(db, duitku_user.id)
        assert effective_plan == "free"
        assert sub.status == "active"
        assert sub.current_period_end is None


def test_polling_provider_status_01_remains_pending(db, duitku_user, duitku_client):
    """When Duitku Check Transaction returns 01 (Process/Pending), transaction remains PENDING and Pro is not active."""
    order_id = generate_duitku_order_id(duitku_user.id)
    tx = PaymentTransaction(
        user_id=duitku_user.id,
        plan="pro",
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="duitku",
        provider_mode="sandbox",
        external_id=order_id,
        created_at=utcnow() - timedelta(seconds=15),
        updated_at=utcnow() - timedelta(seconds=15),
    )
    db.add(tx)
    db.commit()

    mock_status_pending = {
        "merchantOrderId": order_id,
        "reference": "DTK_REF_STILL_PENDING",
        "amount": "49000",
        "statusCode": "01",  # Pending / In Process
        "statusMessage": "In Process",
    }

    with patch("app.services.payment_service.get_duitku_transaction_status", return_value=mock_status_pending):
        res = duitku_client.get(f"/billing/transactions/{tx.id}/status")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "PENDING"
        assert data["is_pro_active"] is False

    db.refresh(tx)
    assert tx.status == "PENDING"
    assert tx.paid_at is None
    effective_plan, _ = get_effective_plan(db, duitku_user.id)
    assert effective_plan == "free"


def test_provider_status_02_marks_failed_and_does_not_activate_pro(db, duitku_user, duitku_client):
    """When Duitku Check Transaction returns 02 (Failed/Expired/Canceled), status is set to FAILED and Pro is not activated."""
    order_id = generate_duitku_order_id(duitku_user.id)
    tx = PaymentTransaction(
        user_id=duitku_user.id,
        plan="pro",
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="duitku",
        provider_mode="sandbox",
        external_id=order_id,
        created_at=utcnow() - timedelta(seconds=15),
        updated_at=utcnow() - timedelta(seconds=15),
    )
    db.add(tx)
    db.commit()

    mock_status_failed = {
        "merchantOrderId": order_id,
        "reference": "DTK_REF_EXPIRED",
        "amount": "49000",
        "statusCode": "02",  # Failed / Expired
        "statusMessage": "Expired / Canceled",
    }

    with patch("app.services.payment_service.get_duitku_transaction_status", return_value=mock_status_failed):
        res = duitku_client.get(f"/billing/transactions/{tx.id}/status")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] in ("FAILED", "EXPIRED")
        assert data["is_pro_active"] is False

    db.refresh(tx)
    assert tx.status in ("FAILED", "EXPIRED")
    assert tx.paid_at is None
    effective_plan, _ = get_effective_plan(db, duitku_user.id)
    assert effective_plan == "free"


def test_callback_mismatched_merchant_code_rejected(db, duitku_user, client):
    """Callback with wrong merchantCode is rejected immediately."""
    order_id = generate_duitku_order_id(duitku_user.id)
    tx = PaymentTransaction(
        user_id=duitku_user.id,
        plan="pro",
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="duitku",
        provider_mode="sandbox",
        external_id=order_id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()

    sig = hmac.new(
        TEST_API_KEY.encode("utf-8"),
        f"WRONG_CODE49000{order_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    res = client.post(
        "/webhooks/duitku",
        data={
            "merchantCode": "WRONG_CODE",
            "amount": "49000",
            "merchantOrderId": order_id,
            "resultCode": "00",
            "signature": sig,
        },
    )
    assert res.status_code == 403
    assert "Invalid" in res.json()["detail"]["error"]


def test_subscription_extension_duration_matrix(db):
    """Test subscription extension calculations for Free, Expired Pro, and Active Pro."""
    user = User(
        email="duration_matrix@raytech.cloud",
        name="Duration Matrix User",
        google_sub="sub-matrix-user",
        is_active=True,
    )
    db.add(user)
    db.commit()

    # Case A: Free user -> adds exactly 30 days
    sub_a = activate_pro_from_payment(db, user.id, transaction_id=1, duration_days=30, commit=True)
    assert sub_a.plan == "pro"
    assert sub_a.status == "active"
    expected_end_a = utcnow() + timedelta(days=30)
    diff_a = abs((ensure_utc(sub_a.current_period_end) - expected_end_a).total_seconds())
    assert diff_a < 5.0  # within 5 seconds

    # Case B: Active Pro -> extends by 30 days from existing current_period_end
    current_end_before = ensure_utc(sub_a.current_period_end)
    sub_b = activate_pro_from_payment(db, user.id, transaction_id=2, duration_days=30, commit=True)
    expected_end_b = current_end_before + timedelta(days=30)
    diff_b = abs((ensure_utc(sub_b.current_period_end) - expected_end_b).total_seconds())
    assert diff_b < 5.0

    # Case C: Expired Pro -> resets from now + 30 days
    sub_b.current_period_end = utcnow() - timedelta(days=10)
    sub_b.plan = "free"
    db.commit()

    sub_c = activate_pro_from_payment(db, user.id, transaction_id=3, duration_days=30, commit=True)
    expected_end_c = utcnow() + timedelta(days=30)
    diff_c = abs((ensure_utc(sub_c.current_period_end) - expected_end_c).total_seconds())
    assert diff_c < 5.0

