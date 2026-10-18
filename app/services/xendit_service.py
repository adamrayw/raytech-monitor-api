import hmac
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("raytech.xendit")

PRO_SUBSCRIPTION_AMOUNT_IDR = 49000
PRO_SUBSCRIPTION_DURATION_DAYS = 30
PRO_PLAN_ID = "pro"

OFFICIAL_XENDIT_BASE_URL = "https://api.xendit.co"
TEST_KEY_PREFIX = "xnd_development_"
PROD_KEY_PREFIX = "xnd_production_"


class XenditConfigurationError(Exception):
    pass


class XenditApiError(Exception):
    def __init__(self, message: str, status_code: int = 502, data: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.data = data


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_xendit_mode() -> str:
    """Returns configured mode. Enforces 'test' as a strict security boundary."""
    raw_mode = os.environ.get("XENDIT_MODE", "test").strip().lower()
    if raw_mode not in ("test", "sandbox"):
        # Strictly fail closed: never permit live mode by default or silently
        raise XenditConfigurationError(
            "Live payment processing is strictly disabled in this release. "
            "Only Xendit Sandbox (XENDIT_MODE=test) is supported."
        )
    return "test"


def get_xendit_secret_key() -> str:
    """Retrieves and validates Xendit secret key against the enforced Sandbox environment."""
    # Ensure mode is test
    get_xendit_mode()

    key = os.environ.get("XENDIT_SECRET_KEY", "").strip()
    if not key:
        return ""

    if key.startswith(PROD_KEY_PREFIX):
        raise XenditConfigurationError(
            "Production Xendit key detected while running in Sandbox mode. "
            "Never use live credentials in test mode."
        )

    if not key.startswith(TEST_KEY_PREFIX):
        raise XenditConfigurationError(
            f"Invalid secret key format for Xendit Sandbox. "
            f"Test secret keys must start with '{TEST_KEY_PREFIX}'."
        )

    return key


def get_xendit_webhook_token() -> str:
    return os.environ.get("XENDIT_WEBHOOK_TOKEN", "").strip()


def get_xendit_base_url() -> str:
    """Returns official Xendit API base URL.

    To prevent SSRF and arbitrary host redirection, user-controlled or request-level
    base URLs are strictly prohibited. Test override is permitted ONLY when running
    under pytest or explicit test harness.
    """
    is_test_env = bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("TESTING", "").lower() in ("true", "1")
    )
    test_override = os.environ.get("_XENDIT_TEST_OVERRIDE_URL", "").strip().rstrip("/")
    if is_test_env and test_override:
        return test_override

    return OFFICIAL_XENDIT_BASE_URL


def get_frontend_url() -> str:
    return os.environ.get("FRONTEND_URL", "http://localhost:3000").rstrip("/")


def generate_external_id(user_id: int) -> str:
    """Generates a unique, collision-resistant order external_id."""
    ts = int(utcnow().timestamp())
    rand = uuid.uuid4().hex[:8]
    return f"rtm_pro_{user_id}_{ts}_{rand}"


def verify_webhook_token(incoming_token: str | None) -> bool:
    """Secure constant-time verification of Xendit webhook callback token.

    Xendit passes this in the 'x-callback-token' header.
    """
    configured_token = get_xendit_webhook_token()
    if not configured_token or not incoming_token:
        logger.warning("Xendit webhook token verification failed: missing token")
        return False

    return hmac.compare_digest(incoming_token.strip(), configured_token.strip())


def validate_xendit_environment() -> None:
    """Startup validation for Xendit environment variables and security constraints."""
    get_xendit_mode()
    key = get_xendit_secret_key()
    if key and not key.startswith(TEST_KEY_PREFIX):
        raise XenditConfigurationError(
            f"Configured XENDIT_SECRET_KEY does not start with '{TEST_KEY_PREFIX}'."
        )


def create_xendit_invoice(
    external_id: str,
    amount: int,
    payer_email: str,
    description: str,
    success_redirect_url: str,
    failure_redirect_url: str,
) -> dict[str, Any]:
    """Requests a hosted checkout invoice from Xendit Invoice API v2.

    Uses HTTP Basic Auth with Secret Key as username and blank password.
    """
    secret_key = get_xendit_secret_key()
    if not secret_key:
        logger.error("XENDIT_SECRET_KEY is not configured")
        raise XenditConfigurationError("Payment gateway credentials are not configured on the server.")

    base_url = get_xendit_base_url()
    endpoint = f"{base_url}/v2/invoices"

    payload = {
        "external_id": external_id,
        "amount": amount,
        "currency": "IDR",
        "description": description,
        "payer_email": payer_email,
        "invoice_duration": 86400,  # 24 hours validity
        "success_redirect_url": success_redirect_url,
        "failure_redirect_url": failure_redirect_url,
        "items": [
            {
                "name": f"Raytech Monitor Pro ({PRO_SUBSCRIPTION_DURATION_DAYS} Days)",
                "quantity": 1,
                "price": amount,
                "category": "Subscription",
            }
        ],
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                endpoint,
                json=payload,
                auth=(secret_key, ""),
                headers={"Content-Type": "application/json"},
            )
    except httpx.TimeoutException as exc:
        logger.error(f"Timeout connecting to Xendit API: {exc}")
        raise XenditApiError("Payment gateway timed out. Please try again later.", status_code=504)
    except httpx.RequestError as exc:
        logger.error(f"Network error connecting to Xendit API: {exc}")
        raise XenditApiError("Unable to connect to payment gateway. Please try again later.", status_code=502)

    if resp.status_code >= 400:
        error_data = {}
        try:
            error_data = resp.json()
        except Exception:
            error_data = {"raw": resp.text}

        logger.error(f"Xendit API returned error {resp.status_code}: {error_data}")
        msg = error_data.get("message") or "Payment gateway was unable to create a checkout session."
        raise XenditApiError(msg, status_code=resp.status_code, data=error_data)

    data = resp.json()
    logger.info(f"Created Xendit invoice {data.get('id')} for order {external_id}")
    return data


def get_xendit_invoice(invoice_id: str) -> dict[str, Any]:
    """Authoritative server-side retrieval of invoice status directly from Xendit."""
    secret_key = get_xendit_secret_key()
    if not secret_key:
        raise XenditConfigurationError("XENDIT_SECRET_KEY is not configured.")

    base_url = get_xendit_base_url()
    endpoint = f"{base_url}/v2/invoices/{invoice_id}"

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(endpoint, auth=(secret_key, ""))
    except httpx.TimeoutException as exc:
        logger.error(f"Timeout fetching Xendit invoice {invoice_id}: {exc}")
        raise XenditApiError("Payment gateway timed out while verifying invoice.", status_code=504)
    except httpx.RequestError as exc:
        logger.error(f"Network error fetching Xendit invoice {invoice_id}: {exc}")
        raise XenditApiError("Unable to connect to payment gateway to verify status.", status_code=502)

    if resp.status_code >= 400:
        logger.error(f"Xendit API error fetching invoice {invoice_id}: {resp.status_code}")
        raise XenditApiError("Failed to fetch invoice status from payment gateway.", status_code=resp.status_code)

    return resp.json()
