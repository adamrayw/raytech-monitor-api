import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models.email_delivery import EmailDelivery
from app.models.payment_transaction import PaymentTransaction
from app.models.subscription import UserSubscription
from app.models.user import User
from app.models.webhook_event import ProcessedWebhookEvent
from app.services.qrisgate_service import (
    PRO_SUBSCRIPTION_AMOUNT_IDR,
    PRO_SUBSCRIPTION_DURATION_DAYS,
    QRISGateApiError,
    QRISGateConfigurationError,
    QRISGateInsufficientCreditError,
    QRISGateRateLimitError,
    QRISGateValidationError,
    create_qrisgate_transaction,
    generate_qrisgate_order_id,
    get_qrisgate_api_key,
    get_qrisgate_base_url,
    get_qrisgate_payment_methods,
    get_qrisgate_webhook_secret,
    verify_qrisgate_timestamp,
    verify_qrisgate_webhook_signature,
)
from app.services.payment_service import reconcile_and_process_payment
from app.services.subscription_service import get_effective_plan, utcnow

TEST_API_KEY = "qg_live_test_api_key_secret_abcdef123456"
TEST_WEBHOOK_SECRET = "whsec_test_webhook_secret_7890xyz"

os.environ["QRISGATE_BASE_URL"] = "https://qrisgate.web.id"
os.environ["QRISGATE_API_KEY"] = TEST_API_KEY
os.environ["QRISGATE_WEBHOOK_SECRET"] = TEST_WEBHOOK_SECRET


@pytest.fixture
def qrisgate_user(db):
    user = User(
        email="qrisgate_buyer@raytech.cloud",
        name="QRISGate Tester",
        google_sub="sub-qrg-buyer-001",
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
def qrisgate_client(db, qrisgate_user):
    from app.database import get_db
    from app.dependencies.auth import get_current_user

    def override_db():
        yield db

    def override_user():
        return qrisgate_user

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


# =========================================================================
# 1. Signature & Anti-Replay Verification Tests
# =========================================================================

def test_signature_verification_success():
    """Valid HMAC-SHA256 signature in hex matches expected signature."""
    raw_body = b'{"event":"payment.paid","external_order_id":"qrg_123"}'
    expected_sig = hmac.new(
        TEST_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    assert verify_qrisgate_webhook_signature(
        raw_body=raw_body,
        signature_header=expected_sig,
        secret=TEST_WEBHOOK_SECRET,
    ) is True


def test_signature_verification_failure_on_tampered_body():
    """Altered body produces signature mismatch and fails verification."""
    raw_body = b'{"event":"payment.paid","amount":49000}'
    expected_sig = hmac.new(
        TEST_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    tampered_body = b'{"event":"payment.paid","amount":1000}'
    assert verify_qrisgate_webhook_signature(
        raw_body=tampered_body,
        signature_header=expected_sig,
        secret=TEST_WEBHOOK_SECRET,
    ) is False


def test_signature_verification_failure_on_wrong_secret():
    """Verification fails when secret key does not match."""
    raw_body = b'{"event":"payment.paid"}'
    fake_sig = hmac.new(
        b"wrong_secret",
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    assert verify_qrisgate_webhook_signature(
        raw_body=raw_body,
        signature_header=fake_sig,
        secret=TEST_WEBHOOK_SECRET,
    ) is False


def test_signature_verification_missing_or_empty():
    """Empty signature header fails closed."""
    raw_body = b'{"event":"payment.paid"}'
    assert verify_qrisgate_webhook_signature(raw_body, "", TEST_WEBHOOK_SECRET) is False
    assert verify_qrisgate_webhook_signature(raw_body, None, TEST_WEBHOOK_SECRET) is False


def test_timestamp_anti_replay_within_window():
    """Timestamps within 5 minutes (300s) pass anti-replay checks."""
    now_ts = int(time.time())
    # Exactly now
    assert verify_qrisgate_timestamp(str(now_ts), max_age_seconds=300) is True
    # 2 minutes ago
    assert verify_qrisgate_timestamp(str(now_ts - 120), max_age_seconds=300) is True
    # 4 minutes 50 seconds ago
    assert verify_qrisgate_timestamp(str(now_ts - 290), max_age_seconds=300) is True
    # Milliseconds format
    assert verify_qrisgate_timestamp(str((now_ts - 60) * 1000), max_age_seconds=300) is True
    # ISO-8601 format
    iso_recent = datetime.now(timezone.utc).isoformat()
    assert verify_qrisgate_timestamp(iso_recent, max_age_seconds=300) is True


def test_timestamp_anti_replay_outside_window():
    """Timestamps older than 5 minutes or far in future are rejected."""
    now_ts = int(time.time())
    # 6 minutes ago
    assert verify_qrisgate_timestamp(str(now_ts - 360), max_age_seconds=300) is False
    # 1 hour ago
    assert verify_qrisgate_timestamp(str(now_ts - 3600), max_age_seconds=300) is False
    # Far in future (clock skew > 300s)
    assert verify_qrisgate_timestamp(str(now_ts + 400), max_age_seconds=300) is False
    # Invalid string
    assert verify_qrisgate_timestamp("invalid-date", max_age_seconds=300) is False
    assert verify_qrisgate_timestamp(None, max_age_seconds=300) is False


# =========================================================================
# 2. Webhook Endpoint Rejection & Security Tests
# =========================================================================

def test_webhook_rejects_missing_signature(qrisgate_client):
    """Webhook rejects request with HTTP 401 when signature header is missing."""
    resp = qrisgate_client.post(
        "/api/webhooks/qrisgate",
        json={"event": "payment.paid"},
        headers={"x-qrisgate-timestamp": str(int(time.time()))},
    )
    assert resp.status_code == 401
    assert "signature" in resp.text.lower()


def test_webhook_rejects_invalid_signature(qrisgate_client):
    """Webhook rejects request with HTTP 401 when signature is invalid."""
    body = b'{"event":"payment.paid"}'
    resp = qrisgate_client.post(
        "/api/webhooks/qrisgate",
        content=body,
        headers={
            "content-type": "application/json",
            "x-qrisgate-signature": "bad_hex_signature_12345",
            "x-qrisgate-timestamp": str(int(time.time())),
        },
    )
    assert resp.status_code == 401


def test_webhook_rejects_expired_timestamp(qrisgate_client):
    """Webhook rejects request with HTTP 401 when timestamp > 5 minutes old."""
    body = b'{"event":"payment.paid"}'
    sig = hmac.new(TEST_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    six_minutes_ago = int(time.time()) - 360

    resp = qrisgate_client.post(
        "/api/webhooks/qrisgate",
        content=body,
        headers={
            "content-type": "application/json",
            "x-qrisgate-signature": sig,
            "x-qrisgate-timestamp": str(six_minutes_ago),
        },
    )
    assert resp.status_code == 401
    assert "timestamp" in resp.text.lower()


# =========================================================================
# 3. Webhook Idempotency & Payment Fulfillment Tests
# =========================================================================

def test_webhook_payment_paid_fulfillment(db, qrisgate_client, qrisgate_user):
    """When event 'payment.paid' is received, order becomes PAID, Pro plan is activated, and email enqueued."""
    # Setup pending transaction
    external_order_id = "qrg_test_order_fulfill_001"
    tx = PaymentTransaction(
        user_id=qrisgate_user.id,
        plan="pro",
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="qrisgate",
        external_id=external_order_id,
        provider_payment_id="tx_qrg_999",
        checkout_url="https://qrisgate.web.id/pay/test",
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    payload = {
        "event_id": "evt_qrg_1001",
        "event": "payment.paid",
        "data": {
            "external_order_id": external_order_id,
            "id": "tx_qrg_999",
            "amount": PRO_SUBSCRIPTION_AMOUNT_IDR,
            "total_amount": PRO_SUBSCRIPTION_AMOUNT_IDR + 5,
            "unique_code": 5,
            "paid_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(TEST_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    ts = str(int(time.time()))

    authoritative = {
        "id": "tx_qrg_999",
        "external_order_id": external_order_id,
        "status": "PAID",
        "amount": PRO_SUBSCRIPTION_AMOUNT_IDR,
        "currency": "IDR",
        "paid_at": payload["data"]["paid_at"],
    }
    with patch("app.services.payment_service.get_qrisgate_transaction", return_value=authoritative):
        resp = qrisgate_client.post(
            "/api/webhooks/qrisgate",
            content=raw_body,
            headers={
                "content-type": "application/json",
                "x-qrisgate-signature": sig,
                "x-qrisgate-timestamp": ts,
            },
        )

    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"

    # Verify transaction transitioned to PAID
    db.refresh(tx)
    assert tx.status == "PAID"
    assert tx.paid_at is not None

    # Verify Pro plan entitlement activated
    plan, sub = get_effective_plan(db, qrisgate_user.id)
    assert plan == "pro"
    assert sub.status == "active"
    assert sub.current_period_end is not None
    assert sub.override_reason == f"qrisgate_payment:{tx.id}"

    # Verify confirmation email delivery enqueued
    delivery = db.scalar(
        select(EmailDelivery).where(EmailDelivery.user_id == qrisgate_user.id)
    )
    assert delivery is not None
    assert delivery.event_type == "payment_confirmed"


def test_webhook_idempotency_duplicate_event(db, qrisgate_client, qrisgate_user):
    """Sending the same event_id repeatedly returns 200 without executing fulfillment twice."""
    external_order_id = "qrg_test_order_idempotent_001"
    tx = PaymentTransaction(
        user_id=qrisgate_user.id,
        plan="pro",
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="qrisgate",
        external_id=external_order_id,
        provider_payment_id="tx_qrg_888",
    )
    db.add(tx)
    db.commit()

    payload = {
        "event_id": "evt_repeat_idempotency_777",
        "event": "payment.paid",
        "data": {
            "external_order_id": external_order_id,
            "id": "tx_qrg_888",
            "paid_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(TEST_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    ts = str(int(time.time()))

    headers = {
        "content-type": "application/json",
        "x-qrisgate-signature": sig,
        "x-qrisgate-timestamp": ts,
    }

    authoritative = {
        "id": "tx_qrg_888",
        "external_order_id": external_order_id,
        "status": "PAID",
        "amount": PRO_SUBSCRIPTION_AMOUNT_IDR,
        "currency": "IDR",
        "paid_at": payload["data"]["paid_at"],
    }
    with patch("app.services.payment_service.get_qrisgate_transaction", return_value=authoritative):
        # First attempt: succeeds and fulfills
        resp1 = qrisgate_client.post("/api/webhooks/qrisgate", content=raw_body, headers=headers)
        assert resp1.status_code == 200

        db.refresh(tx)
        assert tx.status == "PAID"
        first_paid_at = tx.paid_at

        # Second attempt: same event_id, must return 200 instantly as already processed
        resp2 = qrisgate_client.post("/api/webhooks/qrisgate", content=raw_body, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json().get("status") == "already_processed"

    db.refresh(tx)
    assert tx.status == "PAID"
    assert tx.paid_at == first_paid_at


# =========================================================================
# 4. Backend Checkout Endpoint (Task 1) Tests
# =========================================================================

def test_checkout_endpoint_success(db, qrisgate_client, qrisgate_user):
    """POST /billing/checkout and /api/checkout create pending transaction and return checkout_url."""
    mock_qrg_response = {
        "id": "qrg_tx_12345",
        "external_order_id": "qrg_buyer_001",
        "checkout_url": "https://qrisgate.web.id/pay/qrg_tx_12345",
        "total_amount": 49005,
        "unique_code": 5,
        "qr_string": "00020101021226...test_qr",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    }

    with patch("app.routes.billing.create_qrisgate_transaction", return_value=mock_qrg_response):
        resp = qrisgate_client.post(
            "/api/checkout",
            json={"plan": "pro", "provider": "qrisgate", "payment_method": "QRIS"},
        )

    assert resp.status_code == 200
    body = resp.json()

    # Verify response structure matching Task 1 requirements
    assert "data" in body
    data = body["data"]
    assert data["checkout_url"] == "https://qrisgate.web.id/pay/qrg_tx_12345"
    assert data["total_amount"] == 49005
    assert data["unique_code"] == 5
    assert data["qr_string"] == "00020101021226...test_qr"
    assert data["expires_at"] is not None

    # Verify transaction in database
    tx = db.scalar(
        select(PaymentTransaction).where(PaymentTransaction.user_id == qrisgate_user.id)
    )
    assert tx is not None
    assert tx.status == "PENDING"
    assert tx.paid_at is None
    assert tx.amount == PRO_SUBSCRIPTION_AMOUNT_IDR
    assert tx.provider == "qrisgate"
    assert tx.revenue_classification == "REAL"
    assert tx.total_amount == 49005
    assert tx.unique_code == 5


def test_checkout_rejects_unsupported_provider(qrisgate_client):
    """Checkout accepts QRISGate as the sole active payment provider."""
    resp = qrisgate_client.post(
        "/billing/checkout",
        json={"plan": "pro", "provider": "unsupported"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data.get("detail", {}).get("code") == "UNSUPPORTED_PROVIDER"
    assert "qrisgate" in data.get("detail", {}).get("message").lower()


def test_checkout_handles_402_merchant_credit_exhausted(qrisgate_client):
    """When QRISGate returns HTTP 402, checkout endpoint returns HTTP 402 with credit exhausted code."""
    with patch(
        "app.routes.billing.create_qrisgate_transaction",
        side_effect=QRISGateInsufficientCreditError("Saldo kredit merchant QRISGate habis"),
    ):
        resp = qrisgate_client.post(
            "/api/checkout",
            json={"plan": "pro", "provider": "qrisgate"},
        )

    assert resp.status_code == 402
    data = resp.json()
    assert data.get("detail", {}).get("code") == "MERCHANT_CREDIT_EXHAUSTED"


def test_checkout_handles_400_validation_error(qrisgate_client):
    """When QRISGate returns HTTP 400, checkout endpoint returns HTTP 400."""
    with patch(
        "app.routes.billing.create_qrisgate_transaction",
        side_effect=QRISGateValidationError("Invalid customer name"),
    ):
        resp = qrisgate_client.post(
            "/api/checkout",
            json={"plan": "pro", "provider": "qrisgate"},
        )

    assert resp.status_code == 400
    data = resp.json()
    assert data.get("detail", {}).get("code") == "GATEWAY_VALIDATION_ERROR"


# =========================================================================
# 5. Order Status Polling & Fallback (Task 3) Tests
# =========================================================================

def test_order_polling_reconciliation(db, qrisgate_client, qrisgate_user):
    """Polling /api/v1/transactions/{id} authoritatively reconciles with QRISGate and activates Pro if paid."""
    external_order_id = "qrg_order_poll_sync_001"
    tx = PaymentTransaction(
        user_id=qrisgate_user.id,
        plan="pro",
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="qrisgate",
        external_id=external_order_id,
        provider_payment_id="tx_qrg_poll_555",
        checkout_url="https://qrisgate.web.id/pay/poll_555",
    )
    db.add(tx)
    db.commit()

    mock_qrg_status = {
        "id": "tx_qrg_poll_555",
        "external_order_id": external_order_id,
        "status": "PAID",
        "amount": PRO_SUBSCRIPTION_AMOUNT_IDR,
        "total_amount": PRO_SUBSCRIPTION_AMOUNT_IDR,
        "unique_code": 0,
        "paid_at": datetime.now(timezone.utc).isoformat(),
    }

    with patch("app.services.payment_service.get_qrisgate_transaction", return_value=mock_qrg_status):
        resp = qrisgate_client.get(f"/api/v1/transactions/{external_order_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PAID"
    assert body.get("is_pro_active") is True

    # Check database was updated
    db.refresh(tx)
    assert tx.status == "PAID"
    assert tx.paid_at is not None


def test_authoritative_amount_mismatch_is_rejected(db, qrisgate_user):
    tx = PaymentTransaction(
        user_id=qrisgate_user.id,
        plan="pro",
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="qrisgate",
        external_id="qrg_amount_mismatch",
        provider_payment_id="tx_qrg_amount_mismatch",
    )
    db.add(tx)
    db.commit()

    authoritative = {
        "id": tx.provider_payment_id,
        "external_order_id": tx.external_id,
        "status": "PAID",
        "amount": 1000,
        "currency": "IDR",
    }
    with patch("app.services.payment_service.get_qrisgate_transaction", return_value=authoritative):
        with pytest.raises(HTTPException) as exc_info:
            reconcile_and_process_payment(db, tx.id)

    assert exc_info.value.status_code == 422
    db.refresh(tx)
    assert tx.status == "PENDING"
    assert get_effective_plan(db, qrisgate_user.id)[0] == "free"


def test_authoritative_identity_mismatch_is_rejected(db, qrisgate_user):
    tx = PaymentTransaction(
        user_id=qrisgate_user.id,
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="qrisgate",
        external_id="qrg_identity_expected",
        provider_payment_id="tx_qrg_identity_expected",
    )
    db.add(tx)
    db.commit()

    authoritative = {
        "id": tx.provider_payment_id,
        "external_order_id": "qrg_identity_other",
        "status": "PAID",
        "amount": PRO_SUBSCRIPTION_AMOUNT_IDR,
        "currency": "IDR",
    }
    with patch("app.services.payment_service.get_qrisgate_transaction", return_value=authoritative):
        with pytest.raises(HTTPException) as exc_info:
            reconcile_and_process_payment(db, tx.id)

    assert exc_info.value.status_code == 422
    db.refresh(tx)
    assert tx.status == "PENDING"


def test_provider_lookup_failure_does_not_trust_paid_webhook(db, qrisgate_client, qrisgate_user):
    tx = PaymentTransaction(
        user_id=qrisgate_user.id,
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="qrisgate",
        external_id="qrg_fail_closed",
        provider_payment_id="tx_qrg_fail_closed",
    )
    db.add(tx)
    db.commit()
    transaction_id = tx.id
    payload = {
        "event_id": "evt_qrg_fail_closed",
        "event": "payment.paid",
        "data": {"id": tx.provider_payment_id, "external_order_id": tx.external_id},
    }
    raw_body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(TEST_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()

    with patch(
        "app.services.payment_service.get_qrisgate_transaction",
        side_effect=QRISGateApiError("provider unavailable", status_code=502),
    ):
        response = qrisgate_client.post(
            "/api/webhooks/qrisgate",
            content=raw_body,
            headers={
                "content-type": "application/json",
                "x-qrisgate-signature": signature,
                "x-qrisgate-timestamp": str(int(time.time())),
            },
        )

    assert response.status_code == 502
    persisted = db.get(PaymentTransaction, transaction_id)
    assert persisted is None or persisted.status == "PENDING"
    assert db.scalar(select(EmailDelivery.id)) is None


def test_failed_authoritative_payment_does_not_activate_pro(db, qrisgate_user):
    tx = PaymentTransaction(
        user_id=qrisgate_user.id,
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="qrisgate",
        external_id="qrg_failed_payment",
        provider_payment_id="tx_qrg_failed_payment",
    )
    db.add(tx)
    db.commit()
    authoritative = {
        "id": tx.provider_payment_id,
        "external_order_id": tx.external_id,
        "status": "FAILED",
        "amount": PRO_SUBSCRIPTION_AMOUNT_IDR,
        "currency": "IDR",
    }

    with patch("app.services.payment_service.get_qrisgate_transaction", return_value=authoritative):
        outcome, _ = reconcile_and_process_payment(db, tx.id)

    assert outcome == "failed"
    assert get_effective_plan(db, qrisgate_user.id)[0] == "free"
