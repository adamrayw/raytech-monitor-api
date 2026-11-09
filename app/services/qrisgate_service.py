import hashlib
import hmac
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from app.services.plan_config import (
    PRO_PLAN_ID,
    PRO_PRICE_IDR,
    PRO_SUBSCRIPTION_DURATION_DAYS,
)

logger = logging.getLogger("raytech.qrisgate")

PRO_SUBSCRIPTION_AMOUNT_IDR = PRO_PRICE_IDR

DEFAULT_QRISGATE_BASE_URL = "https://qrisgate.web.id"


class QRISGateConfigurationError(Exception):
    """Raised when QRISGate environment variables or merchant settings are missing or misconfigured."""
    pass


class QRISGateApiError(Exception):
    """Base exception for errors returned by QRISGate API."""
    def __init__(self, message: str, status_code: int = 502, data: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.data = data


class QRISGateValidationError(QRISGateApiError):
    """Raised when QRISGate returns HTTP 400 (validation failed)."""
    def __init__(self, message: str = "QRISGate validation failed", data: Any = None):
        super().__init__(message, status_code=400, data=data)


class QRISGateInsufficientCreditError(QRISGateApiError):
    """Raised when QRISGate returns HTTP 402 (merchant credit balance exhausted)."""
    def __init__(self, message: str = "Saldo kredit merchant QRISGate habis", data: Any = None):
        super().__init__(message, status_code=402, data=data)


class QRISGateRateLimitError(QRISGateApiError):
    """Raised when QRISGate returns HTTP 429 (rate limited)."""
    def __init__(self, message: str = "QRISGate rate limit reached", data: Any = None):
        super().__init__(message, status_code=429, data=data)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_qrisgate_base_url() -> str:
    """Returns official QRISGate base URL from environment or default."""
    is_test_env = bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("TESTING", "").lower() in ("true", "1")
    )
    test_override = os.environ.get("_QRISGATE_TEST_OVERRIDE_URL", "").strip().rstrip("/")
    if is_test_env and test_override:
        return test_override

    base = os.environ.get("QRISGATE_BASE_URL", DEFAULT_QRISGATE_BASE_URL).strip().rstrip("/")
    return base or DEFAULT_QRISGATE_BASE_URL


def get_qrisgate_api_key() -> str:
    """Returns QRISGate API Key (e.g. qg_live_...)."""
    return os.environ.get("QRISGATE_API_KEY", "").strip()


def get_qrisgate_webhook_secret() -> str:
    """Returns QRISGate Webhook Secret (e.g. whsec_...)."""
    return os.environ.get("QRISGATE_WEBHOOK_SECRET", "").strip()


def get_qrisgate_merchant_id() -> str:
    """Returns the expected merchant identifier when QRISGate exposes one."""
    return os.environ.get("QRISGATE_MERCHANT_ID", "").strip()


def validate_qrisgate_environment() -> None:
    """Validates that essential QRISGate credentials exist."""
    api_key = get_qrisgate_api_key()
    webhook_secret = get_qrisgate_webhook_secret()
    if not api_key:
        logger.warning("QRISGATE_API_KEY is not configured in environment.")
    if not webhook_secret:
        logger.warning("QRISGATE_WEBHOOK_SECRET is not configured in environment.")


def generate_qrisgate_order_id(user_id: int | str) -> str:
    """Generates unique, collision-resistant external order ID."""
    ts = int(utcnow().timestamp())
    rand = uuid.uuid4().hex[:8]
    return f"qrg_{user_id}_{ts}_{rand}"


def get_frontend_url() -> str:
    return os.environ.get("FRONTEND_URL", "https://monitor.raytech.cloud").rstrip("/")


def verify_qrisgate_webhook_signature(
    raw_body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
    secret: str | None = None,
) -> bool:
    """Verify QRISGate's HMAC-SHA256 signature over timestamp + raw body."""
    if not signature_header or not timestamp_header:
        return False

    webhook_secret = secret or get_qrisgate_webhook_secret()
    if not webhook_secret:
        logger.error("QRISGATE_WEBHOOK_SECRET is not configured; cannot verify signature")
        return False

    signed_payload = timestamp_header.strip().encode("utf-8") + b"." + raw_body
    expected_signature = hmac.new(
        webhook_secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    clean_incoming = signature_header.removeprefix("sha256=").strip().lower()
    return hmac.compare_digest(expected_signature.lower(), clean_incoming)


def verify_qrisgate_timestamp(
    timestamp_header: str | None,
    max_age_seconds: int = 300,
) -> bool:
    """Verifies that the x-qrisgate-timestamp header is within max_age_seconds (default 5 minutes).

    Prevents replay attacks. Supports unix epoch seconds, milliseconds, or ISO-8601 strings.
    """
    if not timestamp_header:
        return False

    raw_ts = timestamp_header.strip()
    ts_float: float | None = None

    # Try numeric parsing first (epoch seconds or ms)
    try:
        val = float(raw_ts)
        # If timestamp is in milliseconds (> 10^11), convert to seconds
        if val > 1e11:
            val = val / 1000.0
        ts_float = val
    except ValueError:
        # Try ISO 8601 parsing
        try:
            clean = raw_ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean)
            ts_float = dt.timestamp()
        except Exception:
            return False

    now_float = utcnow().timestamp()
    time_diff = abs(now_float - ts_float)
    return time_diff <= max_age_seconds


def _execute_qrisgate_request_with_retry(
    method: str,
    url: str,
    headers: dict[str, str],
    json_data: dict[str, Any] | None = None,
    max_retries: int = 3,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Executes HTTP request to QRISGate with exponential backoff retry for HTTP 429 rate limits."""
    backoff = 1.0

    for attempt in range(1, max_retries + 1):
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=json_data,
                )

            # Handle Rate Limiting (429)
            if response.status_code == 429:
                if attempt < max_retries:
                    retry_after = response.headers.get("Retry-After")
                    sleep_time = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                    logger.warning(
                        f"QRISGate 429 rate limit reached on attempt {attempt}/{max_retries}. Backing off {sleep_time}s..."
                    )
                    time.sleep(sleep_time)
                    backoff *= 2.0
                    continue
                else:
                    raise QRISGateRateLimitError(
                        message="QRISGate rate limit reached. Please retry shortly.",
                        data=response.text,
                    )

            # Handle Insufficient Merchant Credit Balance (402)
            if response.status_code == 402:
                logger.error("QRISGate error 402: Saldo kredit merchant habis")
                raise QRISGateInsufficientCreditError(
                    message="Saldo kredit merchant QRISGate habis. Silakan hubungi administrator.",
                    data=response.text,
                )

            # Handle Bad Request / Validation Failure (400)
            if response.status_code == 400:
                logger.error("QRISGate validation error (HTTP 400)")
                try:
                    err_json = response.json()
                    err_msg = err_json.get("message") or err_json.get("error") or "Validation error from QRISGate"
                except Exception:
                    err_msg = response.text or "Validation error from QRISGate"
                raise QRISGateValidationError(message=err_msg, data=response.text)

            # Handle Unauthorized (401)
            if response.status_code == 401:
                logger.error("QRISGate authorization error (401): Invalid API key")
                raise QRISGateApiError(
                    message="QRISGate API key is invalid or unauthorized.",
                    status_code=401,
                    data=response.text,
                )

            # Raise for any other 4xx/5xx status
            if response.is_error:
                logger.error("QRISGate HTTP error: status=%s", response.status_code)
                raise QRISGateApiError(
                    message=f"QRISGate API returned status {response.status_code}",
                    status_code=response.status_code,
                    data=response.text,
                )

            # Parse JSON
            try:
                return response.json()
            except Exception as exc:
                raise QRISGateApiError(
                    message=f"Invalid JSON response from QRISGate: {exc}",
                    status_code=502,
                )

        except (QRISGateApiError, QRISGateValidationError, QRISGateInsufficientCreditError, QRISGateRateLimitError):
            raise
        except httpx.RequestError as exc:
            if attempt < max_retries:
                logger.warning("QRISGate network error on attempt %s; retrying", attempt)
                time.sleep(backoff)
                backoff *= 2.0
                continue
            raise QRISGateApiError(
                message="Network error communicating with QRISGate",
                status_code=502,
            )

    raise QRISGateApiError("Failed to communicate with QRISGate after retries", status_code=502)


def create_qrisgate_transaction(
    external_order_id: str,
    amount: int,
    customer_name: str,
    return_url: str | None = None,
    payment_method: str = "QRIS",
    bank_code: str | None = None,
) -> dict[str, Any]:
    """Creates a new transaction on QRISGate via POST /api/v1/transactions.

    Header: Authorization: Bearer <QRISGATE_API_KEY>
    Body:
    {
        "external_order_id": external_order_id,
        "amount": amount,
        "customer_name": customer_name,
        "return_url": return_url,
        "payment_method": "QRIS"
    }

    All amounts are strictly integer IDR.
    Returns normalized dict containing checkout_url, total_amount, unique_code, qr_string, expires_at, id.
    """
    api_key = get_qrisgate_api_key()
    if not api_key:
        raise QRISGateConfigurationError(
            "QRISGATE_API_KEY is not configured in server environment."
        )

    # Ensure amount is strict integer IDR without floating point or decimals
    amount_int = int(round(float(amount)))
    if amount_int <= 0:
        raise QRISGateValidationError("Transaction amount must be a positive integer IDR.")

    base_url = get_qrisgate_base_url()
    url = f"{base_url}/api/v1/transactions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    effective_return_url = return_url or f"{get_frontend_url()}/order/{external_order_id}"

    payload: dict[str, Any] = {
        "external_order_id": external_order_id,
        "amount": amount_int,
        "customer_name": customer_name or "Raytech Subscriber",
        "return_url": effective_return_url,
    }

    if payment_method:
        payload["payment_method"] = payment_method.strip().upper()
    if bank_code:
        payload["bank_code"] = bank_code.strip().upper()

    logger.info(
        f"Initiating QRISGate transaction for order {external_order_id}, amount: Rp {amount_int}"
    )

    resp_data = _execute_qrisgate_request_with_retry(
        method="POST",
        url=url,
        headers=headers,
        json_data=payload,
    )

    # Support either { "data": { ... } } or flat { ... } response
    inner_data = resp_data.get("data") if isinstance(resp_data.get("data"), dict) else resp_data

    transaction_id = str(inner_data.get("id") or inner_data.get("transaction_id") or "")
    checkout_url = inner_data.get("checkout_url")
    total_amount = inner_data.get("total_amount") or inner_data.get("amount") or amount_int
    unique_code = inner_data.get("unique_code") or 0
    qr_string = inner_data.get("qr_string")
    expires_at = inner_data.get("expires_at")

    try:
        total_amount_int = int(total_amount)
    except (ValueError, TypeError):
        total_amount_int = amount_int

    try:
        unique_code_int = int(unique_code)
    except (ValueError, TypeError):
        unique_code_int = 0

    return {
        "id": transaction_id,
        "external_order_id": external_order_id,
        "checkout_url": checkout_url,
        "total_amount": total_amount_int,
        "unique_code": unique_code_int,
        "qr_string": qr_string,
        "expires_at": expires_at,
        "raw_response": resp_data,
    }


def get_qrisgate_transaction(transaction_id_or_external_id: str | int) -> dict[str, Any]:
    """Retrieves authoritative transaction status from QRISGate via GET /api/v1/transactions/{id}.

    Header: Authorization: Bearer <QRISGATE_API_KEY>
    """
    api_key = get_qrisgate_api_key()
    if not api_key:
        raise QRISGateConfigurationError(
            "QRISGATE_API_KEY is not configured in server environment."
        )

    base_url = get_qrisgate_base_url()
    clean_id = str(transaction_id_or_external_id).strip()
    url = f"{base_url}/api/v1/transactions/{clean_id}"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    resp_data = _execute_qrisgate_request_with_retry(
        method="GET",
        url=url,
        headers=headers,
    )

    inner = resp_data.get("data") if isinstance(resp_data.get("data"), dict) else resp_data

    raw_status = str(inner.get("status") or "").strip().upper()
    normalized_status = "PENDING"
    if raw_status in ("PAID", "SETTLED", "SUCCESS", "COMPLETED"):
        normalized_status = "PAID"
    elif raw_status in ("EXPIRED", "TIMEOUT"):
        normalized_status = "EXPIRED"
    elif raw_status in ("FAILED", "CANCELED", "CANCELLED", "REJECTED"):
        normalized_status = "FAILED"

    base_amount = inner.get("base_amount")
    if base_amount is None:
        base_amount = inner.get("amount")

    return {
        "id": str(inner.get("transaction_id") or inner.get("id") or ""),
        "external_order_id": inner.get("external_order_id"),
        "status": normalized_status,
        "raw_status": raw_status,
        "amount": base_amount,
        "total_amount": inner.get("total_amount"),
        "currency": inner.get("currency"),
        "merchant_id": inner.get("merchant_id") or inner.get("merchant_code"),
        "unique_code": inner.get("unique_code"),
        "checkout_url": inner.get("checkout_url"),
        "qr_string": inner.get("qr_string"),
        "paid_at": inner.get("paid_at"),
        "expires_at": inner.get("expires_at"),
        "raw_response": resp_data,
    }


def get_qrisgate_payment_methods() -> list[dict[str, Any]]:
    """Returns available QRISGate payment methods."""
    return [
        {
            "code": "QRIS",
            "name": "QRIS (Semua E-Wallet & Mobile Banking)",
            "category": "qr",
            "category_label": "QR Payment",
            "fee": 0,
            "active": True,
            "image_url": "https://qrisgate.web.id/qris-logo.png",
        }
    ]

