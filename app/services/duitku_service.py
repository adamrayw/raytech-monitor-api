import hashlib
import hmac
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("raytech.duitku")

PRO_SUBSCRIPTION_AMOUNT_IDR = 49000
PRO_SUBSCRIPTION_DURATION_DAYS = 30
PRO_PLAN_ID = "pro"

OFFICIAL_DUITKU_SANDBOX_BASE = "https://sandbox.duitku.com"
OFFICIAL_DUITKU_PROD_BASE = "https://passport.duitku.com"


class DuitkuConfigurationError(Exception):
    pass


class DuitkuApiError(Exception):
    def __init__(self, message: str, status_code: int = 502, data: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.data = data


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_duitku_mode() -> str:
    """Returns configured mode. Enforces 'sandbox' as a strict security boundary."""
    raw_mode = os.environ.get("DUITKU_MODE", "sandbox").strip().lower()
    if raw_mode not in ("sandbox", "test"):
        # Strictly fail closed: never permit live mode by default or silently
        raise DuitkuConfigurationError(
            "Live payment processing is strictly disabled in this release. "
            "Only Duitku Sandbox (DUITKU_MODE=sandbox) is supported."
        )
    return "sandbox"


def get_duitku_merchant_code() -> str:
    get_duitku_mode()
    return os.environ.get("DUITKU_MERCHANT_CODE", "").strip()


def get_duitku_api_key() -> str:
    get_duitku_mode()
    return os.environ.get("DUITKU_API_KEY", "").strip()


def get_duitku_base_url() -> str:
    """Returns official Duitku API base URL.

    To prevent SSRF and arbitrary host redirection, user-controlled or request-level
    base URLs are strictly prohibited. Test override is permitted ONLY when running
    under pytest or explicit test harness.
    """
    is_test_env = bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("TESTING", "").lower() in ("true", "1")
    )
    test_override = os.environ.get("_DUITKU_TEST_OVERRIDE_URL", "").strip().rstrip("/")
    if is_test_env and test_override:
        return test_override

    get_duitku_mode()
    return OFFICIAL_DUITKU_SANDBOX_BASE


def get_frontend_url() -> str:
    return os.environ.get("FRONTEND_URL", "http://localhost:3000").rstrip("/")


def get_backend_url() -> str:
    return os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/")


def get_duitku_callback_url() -> str:
    custom_url = os.environ.get("DUITKU_CALLBACK_URL", "").strip()
    if custom_url:
        return custom_url
    return f"{get_backend_url()}/webhooks/duitku"


def get_duitku_return_url() -> str:
    custom_url = os.environ.get("DUITKU_RETURN_URL", "").strip()
    if custom_url:
        return custom_url
    return f"{get_frontend_url()}/settings/billing"


def generate_duitku_order_id(user_id: int) -> str:
    """Generates a unique, collision-resistant merchantOrderId for Duitku."""
    ts = int(utcnow().timestamp())
    rand = uuid.uuid4().hex[:8]
    return f"dtk_pro_{user_id}_{ts}_{rand}"


def generate_inquiry_signature(merchant_code: str, merchant_order_id: str, amount: int, api_key: str) -> str:
    """Generates official HMAC-SHA256 signature for Duitku v2/inquiry request:
    Formula: HMAC_SHA256(merchantCode + merchantOrderId + paymentAmount, apiKey)
    """
    string_to_sign = f"{merchant_code}{merchant_order_id}{amount}"
    return hmac.new(
        api_key.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_check_transaction_signature(merchant_code: str, merchant_order_id: str, api_key: str) -> str:
    """Generates official HMAC-SHA256 signature for Duitku transactionStatus request:
    Formula: HMAC_SHA256(merchantCode + merchantOrderId, apiKey)
    """
    string_to_sign = f"{merchant_code}{merchant_order_id}"
    return hmac.new(
        api_key.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_duitku_callback_signature(
    merchant_code: str,
    amount: Any,
    merchant_order_id: str,
    incoming_signature: str | None,
) -> bool:
    """Verifies Duitku callback notification authenticity using official HMAC-SHA256.
    Formula: HMAC_SHA256(merchantCode + amount + merchantOrderId, apiKey)
    """
    api_key = get_duitku_api_key()
    configured_code = get_duitku_merchant_code()

    if not api_key or not incoming_signature or not merchant_code:
        logger.warning("Duitku callback verification failed: missing key or credentials")
        return False

    # Check merchant code matches our configured merchant code if configured
    if configured_code and merchant_code.strip() != configured_code:
        logger.warning(
            f"Duitku callback merchantCode mismatch: expected {configured_code}, got {merchant_code}"
        )
        return False

    # Amount as formatted in string (e.g. '49000' or 49000)
    str_amount = str(amount).strip()
    string_to_sign = f"{merchant_code}{str_amount}{merchant_order_id}"
    expected_sig = hmac.new(
        api_key.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(incoming_signature.strip().lower(), expected_sig.lower())


def validate_duitku_environment() -> None:
    """Startup validation for Duitku environment variables."""
    get_duitku_mode()
    # Log sanitized status
    m_code = get_duitku_merchant_code()
    has_key = bool(get_duitku_api_key())
    logger.info(
        f"Duitku environment initialized: mode=sandbox, merchant_code={'[SET]' if m_code else '[EMPTY]'}, api_key={'[SET]' if has_key else '[EMPTY]'}"
    )


def create_duitku_transaction(
    merchant_order_id: str,
    amount: int,
    payer_email: str,
    customer_name: str | None = None,
    phone_number: str | None = None,
    payment_method: str = "SP",  # Default to QRIS (ShopeePay QRIS)
    product_details: str | None = None,
) -> dict[str, Any]:
    """Requests a payment transaction / QRIS checkout from Duitku v2/inquiry API.

    Prioritizes QRIS (paymentMethod='SP' or 'NQ') and returns paymentUrl and qrString.
    """
    merchant_code = get_duitku_merchant_code()
    api_key = get_duitku_api_key()

    if not merchant_code or not api_key:
        logger.error("DUITKU_MERCHANT_CODE or DUITKU_API_KEY is not configured")
        raise DuitkuConfigurationError("Payment gateway credentials are not configured on the server.")

    base_url = get_duitku_base_url()
    endpoint = f"{base_url}/webapi/api/merchant/v2/inquiry"

    signature = generate_inquiry_signature(
        merchant_code=merchant_code,
        merchant_order_id=merchant_order_id,
        amount=amount,
        api_key=api_key,
    )

    clean_name = (customer_name or "Raytech Subscriber").strip()[:20]
    desc = product_details or f"Raytech Monitor Pro ({PRO_SUBSCRIPTION_DURATION_DAYS} Days)"

    payload = {
        "merchantCode": merchant_code,
        "paymentAmount": amount,
        "paymentMethod": payment_method or "SP",
        "merchantOrderId": merchant_order_id,
        "productDetails": desc,
        "additionalParam": "",
        "merchantUserInfo": payer_email,
        "customerVaName": clean_name,
        "email": payer_email,
        "phoneNumber": phone_number or "081230008890",
        "itemDetails": [
            {
                "name": f"Raytech Monitor Pro ({PRO_SUBSCRIPTION_DURATION_DAYS} Days)",
                "price": amount,
                "quantity": 1,
            }
        ],
        "customerDetail": {
            "firstName": clean_name,
            "lastName": "",
            "email": payer_email,
            "phoneNumber": phone_number or "081230008890",
        },
        "callbackUrl": get_duitku_callback_url(),
        "returnUrl": get_duitku_return_url(),
        "signature": signature,
        "expiryPeriod": 1440,  # 24 hours in minutes
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
    except httpx.TimeoutException as exc:
        logger.error(f"Timeout connecting to Duitku API: {exc}")
        raise DuitkuApiError("Payment gateway timed out. Please try again later.", status_code=504)
    except httpx.RequestError as exc:
        logger.error(f"Network error connecting to Duitku API: {exc}")
        raise DuitkuApiError("Unable to connect to payment gateway. Please try again later.", status_code=502)

    if resp.status_code >= 400:
        error_data = {}
        try:
            error_data = resp.json()
        except Exception:
            error_data = {"raw": resp.text}

        logger.error(f"Duitku API returned HTTP error {resp.status_code}: {error_data}")
        msg = error_data.get("statusMessage") or error_data.get("Message") or "Payment gateway was unable to create checkout session."
        raise DuitkuApiError(msg, status_code=resp.status_code, data=error_data)

    data = resp.json()
    status_code = str(data.get("statusCode", "")).strip()

    if status_code != "00":
        msg = data.get("statusMessage") or f"Duitku returned non-success code: {status_code}"
        logger.error(f"Duitku transaction creation rejected: {data}")
        raise DuitkuApiError(msg, status_code=422, data=data)

    logger.info(
        f"Created Duitku transaction for order {merchant_order_id}: reference={data.get('reference')}, has_qr={'qrString' in data}"
    )
    return data


def get_duitku_transaction_status(merchant_order_id: str) -> dict[str, Any]:
    """Authoritative server-side retrieval of transaction status via Duitku Check Transaction API."""
    merchant_code = get_duitku_merchant_code()
    api_key = get_duitku_api_key()

    if not merchant_code or not api_key:
        raise DuitkuConfigurationError("DUITKU_MERCHANT_CODE or DUITKU_API_KEY is not configured.")

    base_url = get_duitku_base_url()
    endpoint = f"{base_url}/webapi/api/merchant/transactionStatus"

    signature = generate_check_transaction_signature(
        merchant_code=merchant_code,
        merchant_order_id=merchant_order_id,
        api_key=api_key,
    )

    payload = {
        "merchantCode": merchant_code,
        "merchantOrderId": merchant_order_id,
        "signature": signature,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
    except httpx.TimeoutException as exc:
        logger.error(f"Timeout checking Duitku transaction status for {merchant_order_id}: {exc}")
        raise DuitkuApiError("Payment gateway timed out while checking transaction status.", status_code=504)
    except httpx.RequestError as exc:
        logger.error(f"Network error checking Duitku transaction status for {merchant_order_id}: {exc}")
        raise DuitkuApiError("Unable to connect to payment gateway to check transaction status.", status_code=502)

    if resp.status_code >= 400:
        logger.error(f"Duitku check transaction returned HTTP {resp.status_code}")
        raise DuitkuApiError("Failed to check transaction status with payment gateway.", status_code=resp.status_code)

    data = resp.json()
    logger.info(
        f"Duitku check status for {merchant_order_id}: statusCode={data.get('statusCode')}, statusMessage={data.get('statusMessage')}"
    )
    return data


def interpret_duitku_status(status_data: dict[str, Any]) -> tuple[str, str]:
    """Interprets Duitku Check Transaction response status authoritatively.

    Returns (normalized_status, raw_status_code).
    Valid normalized states: 'PAID', 'PENDING', 'FAILED', 'EXPIRED'.

    Duitku Specification:
    - statusCode '00': SUCCESS (only when statusMessage indicates SUCCESS)
    - statusCode '01': PENDING (Process / Pending payment)
    - statusCode '02': FAILED / EXPIRED / CANCELED
    Transport HTTP 200 MUST NOT be treated as success unless statusCode == '00'.
    """
    raw_code = str(status_data.get("statusCode", "")).strip()
    raw_msg = str(status_data.get("statusMessage", "")).strip().upper()

    if raw_code == "00":
        # Fail closed: statusMessage must not indicate failure/pending
        if any(neg in raw_msg for neg in ("PENDING", "PROCESS", "FAIL", "EXPIRE", "CANCEL", "NOT FOUND")):
            logger.warning(f"Duitku statusCode '00' contradicted by statusMessage: '{raw_msg}'. Treating as PENDING.")
            return "PENDING", raw_code
        return "PAID", raw_code
    elif raw_code == "01":
        return "PENDING", raw_code
    elif raw_code in ("02", "FAILED", "CANCELED", "EXPIRED"):
        if "EXPIRE" in raw_msg:
            return "EXPIRED", raw_code
        return "FAILED", raw_code
    else:
        # Unknown code or missing, fail closed to PENDING
        return "PENDING", raw_code


def is_user_allowed_sandbox_checkout(user: Any) -> bool:
    """Determines whether a user is permitted to initiate Duitku Sandbox checkout."""
    if not user or not getattr(user, "email", None):
        return False
    mode = os.environ.get("DUITKU_MODE", "sandbox").strip().lower()
    if mode != "sandbox":
        return True
    allowlist_raw = os.environ.get("SANDBOX_CHECKOUT_ALLOWLIST", "").strip()
    if not allowlist_raw:
        return user.email.lower().endswith("@raytech.cloud")
    allowed_entries = [entry.strip().lower() for entry in allowlist_raw.split(",") if entry.strip()]
    email_lower = user.email.lower()
    for entry in allowed_entries:
        if entry.startswith("@") and email_lower.endswith(entry):
            return True
        if email_lower == entry:
            return True
    return False
