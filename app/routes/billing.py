import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models.payment_transaction import PaymentTransaction
from app.models.user import User
from app.services.duitku_service import (
    PRO_PLAN_ID,
    PRO_SUBSCRIPTION_AMOUNT_IDR,
    PRO_SUBSCRIPTION_DURATION_DAYS,
    DuitkuApiError,
    DuitkuConfigurationError,
    create_duitku_transaction,
    generate_duitku_order_id,
    get_duitku_api_key,
    get_duitku_mode,
    get_duitku_payment_methods,
    is_user_allowed_sandbox_checkout,
)
from app.services.payment_service import reconcile_and_process_payment
from app.services.rate_limiter import api_rate_limiter
from app.services.subscription_service import (
    activate_pro_from_payment,
    ensure_utc,
    get_effective_plan,
    utcnow,
)

logger = logging.getLogger("raytech.billing")

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/payment-methods", summary="List available payment methods from gateway")
def list_payment_methods(
    amount: int = Query(default=PRO_SUBSCRIPTION_AMOUNT_IDR, description="Transaction amount in IDR"),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Returns the list of available payment methods for the active Duitku merchant.
    Categories are derived from active methods only. Safe public data only.
    """
    api_rate_limiter.check_or_raise(
        f"billing_methods:{current_user.id}",
        max_requests=60,
        window_seconds=60.0,
        error_detail="Too many payment method requests. Please wait before refreshing.",
    )
    enforced_amount = PRO_SUBSCRIPTION_AMOUNT_IDR
    try:
        methods = get_duitku_payment_methods(amount=enforced_amount)
    except DuitkuConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "GATEWAY_CONFIG_ERROR", "message": str(exc)},
        )
    except DuitkuApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "GATEWAY_API_ERROR", "message": str(exc)},
        )

    # Derive unique ordered categories that have at least one active method
    seen_cats = set()
    categories = []
    category_order = ["virtual_account", "ewallet", "qr", "card", "retail", "paylater", "other"]
    methods_by_cat = {m["category"]: m["category_label"] for m in methods}

    for cat_key in category_order:
        if cat_key in methods_by_cat and cat_key not in seen_cats:
            seen_cats.add(cat_key)
            categories.append({"key": cat_key, "label": methods_by_cat[cat_key]})

    for cat_key, cat_label in methods_by_cat.items():
        if cat_key not in seen_cats:
            seen_cats.add(cat_key)
            categories.append({"key": cat_key, "label": cat_label})

    return {
        "payment_methods": methods,
        "categories": categories,
        "provider_mode": get_duitku_mode(),
        "base_amount": enforced_amount,
        "currency": "IDR",
    }


class CheckoutRequest(BaseModel):
    plan: str = Field(default=PRO_PLAN_ID, description="Target plan ID ('pro')")
    provider: str = Field(default="duitku", description="Payment provider adapter ('duitku')")
    payment_method: str | None = Field(default="SP", description="Duitku payment method code (e.g. 'SP' for QRIS)")


def parse_iso_datetime(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    try:
        clean_str = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(clean_str)
    except Exception:
        return None


@router.post("/checkout", summary="Create payment checkout session")
def create_checkout_session(
    payload: CheckoutRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Creates a local pending payment transaction and requests a checkout session from Duitku.

    Xendit payment gateway has been retired from active checkout. Duitku is the sole active payment provider.
    Price and duration are authoritative on the backend (IDR 49,000 for 30 days).
    Client cannot supply custom amounts or duration.
    """
    api_rate_limiter.check_or_raise(
        f"billing_checkout:{current_user.id}",
        max_requests=10,
        window_seconds=60.0,
        error_detail="Too many checkout attempts. Please wait a minute before creating another checkout.",
    )
    requested_plan = payload.plan.strip().lower()
    if requested_plan != PRO_PLAN_ID:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_PLAN",
                "message": f"Only '{PRO_PLAN_ID}' plan is available for checkout.",
            },
        )

    requested_provider = (payload.provider or "duitku").strip().lower()
    if requested_provider != "duitku":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "GATEWAY_DISABLED",
                "message": "Xendit payment gateway has been retired. Duitku is the only active payment gateway.",
            },
        )

    # Controlled Sandbox Checkout Protection:
    if not is_user_allowed_sandbox_checkout(current_user):
        logger.warning(
            f"Blocked sandbox checkout attempt for non-allowlisted user {current_user.id} ({current_user.email})"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "SANDBOX_CHECKOUT_RESTRICTED",
                "message": (
                    "Pembelian paket Pro saat ini beroperasi dalam mode Sandbox tertutup (internal testing) "
                    "dan belum dibuka untuk umum. Hubungi tim dukungan di alerts@raytech.cloud untuk "
                    "permohonan akses pengujian."
                ),
            },
        )

    # 1. Validate requested payment method against active Duitku methods
    requested_method = (payload.payment_method or "SP").strip()
    try:
        available_methods = get_duitku_payment_methods(amount=PRO_SUBSCRIPTION_AMOUNT_IDR)
        active_codes = {m["code"] for m in available_methods if m.get("active", True)}
        if active_codes and requested_method not in active_codes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "PAYMENT_METHOD_UNAVAILABLE",
                    "message": f"Payment method '{requested_method}' is currently not available for this merchant.",
                },
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(f"Could not pre-validate payment method availability with Duitku: {exc}")

    # 2. Idempotency protection against rapid repeated clicks:
    # If a pending transaction exists created within last 15 minutes for Duitku with same payment method and valid checkout_url, reuse it.
    fifteen_mins_ago = utcnow() - timedelta(minutes=15)
    existing_pending = db.scalar(
        select(PaymentTransaction)
        .where(
            PaymentTransaction.user_id == current_user.id,
            PaymentTransaction.status == "PENDING",
            PaymentTransaction.provider == "duitku",
            or_(
                PaymentTransaction.payment_method == requested_method,
                PaymentTransaction.payment_method.is_(None) if requested_method == "SP" else False,
            ),
            PaymentTransaction.created_at >= fifteen_mins_ago,
            PaymentTransaction.checkout_url.isnot(None),
        )
        .order_by(PaymentTransaction.created_at.desc())
        .limit(1)
    )

    if existing_pending and existing_pending.checkout_url:
        logger.info(f"Reusing active pending checkout tx {existing_pending.id} for user {current_user.id} ({requested_method})")
        return {
            "transaction_id": existing_pending.id,
            "external_id": existing_pending.external_id,
            "provider": existing_pending.provider,
            "provider_mode": existing_pending.provider_mode,
            "payment_method": existing_pending.payment_method,
            "payment_channel": existing_pending.payment_channel,
            "checkout_url": existing_pending.checkout_url,
            "va_number": getattr(existing_pending, "va_number", None),
            "qr_string": existing_pending.qr_string,
            "amount": existing_pending.amount,
            "currency": existing_pending.currency,
            "status": existing_pending.status,
            "expires_at": existing_pending.expires_at.isoformat() if existing_pending.expires_at else None,
        }

    # 3. Determine mode and unique external order reference
    external_id = generate_duitku_order_id(current_user.id)
    provider_mode = get_duitku_mode()

    # 4. Create local pending transaction record
    tx = PaymentTransaction(
        user_id=current_user.id,
        plan=PRO_PLAN_ID,
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="duitku",
        provider_mode=provider_mode,
        payment_method=requested_method,
        external_id=external_id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    # 5. Initiate provider checkout via Duitku
    try:
        data = create_duitku_transaction(
            merchant_order_id=tx.external_id,
            amount=tx.amount,
            payer_email=current_user.email,
            customer_name=current_user.name or "Raytech Subscriber",
            payment_method=requested_method,
        )
    except DuitkuConfigurationError as exc:
        tx.status = "FAILED"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "GATEWAY_CONFIG_ERROR", "message": str(exc)},
        )
    except DuitkuApiError as exc:
        tx.status = "FAILED"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "GATEWAY_API_ERROR", "message": str(exc)},
        )

    tx.provider_payment_id = data.get("reference")
    tx.checkout_url = data.get("paymentUrl")
    tx.qr_string = data.get("qrString")
    tx.va_number = data.get("vaNumber")
    tx.payment_method = requested_method
    tx.payment_channel = "duitku"
    tx.expires_at = utcnow() + timedelta(hours=24)
    tx.updated_at = utcnow()
    db.commit()
    db.refresh(tx)

    return {
        "transaction_id": tx.id,
        "external_id": tx.external_id,
        "provider": tx.provider,
        "provider_mode": tx.provider_mode,
        "payment_method": tx.payment_method,
        "payment_channel": tx.payment_channel,
        "checkout_url": tx.checkout_url,
        "va_number": tx.va_number,
        "qr_string": tx.qr_string,
        "amount": tx.amount,
        "currency": tx.currency,
        "status": tx.status,
        "expires_at": tx.expires_at.isoformat() if tx.expires_at else None,
    }


@router.get("/transactions", summary="List user billing payment history")
def list_user_transactions(
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Returns authenticated user's payment transaction history."""
    total = (
        db.scalar(
            select(func.count(PaymentTransaction.id)).where(
                PaymentTransaction.user_id == current_user.id
            )
        )
        or 0
    )

    transactions = (
        db.scalars(
            select(PaymentTransaction)
            .where(PaymentTransaction.user_id == current_user.id)
            .order_by(PaymentTransaction.created_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )

    items = [
        {
            "id": tx.id,
            "external_id": tx.external_id,
            "provider": tx.provider,
            "provider_mode": tx.provider_mode,
            "plan": tx.plan,
            "amount": tx.amount,
            "currency": tx.currency,
            "status": tx.status,
            "payment_method": tx.payment_method,
            "payment_channel": tx.payment_channel,
            "checkout_url": tx.checkout_url,
            "va_number": getattr(tx, "va_number", None),
            "qr_string": tx.qr_string,
            "expires_at": tx.expires_at.isoformat() if tx.expires_at else None,
            "paid_at": tx.paid_at.isoformat() if tx.paid_at else None,
            "created_at": tx.created_at.isoformat(),
        }
        for tx in transactions
    ]

    return {"items": items, "count": total}


@router.get("/transactions/{transaction_id}", summary="Get payment transaction detail")
def get_transaction_detail(
    transaction_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Returns details of a specific payment transaction belonging to the current user."""
    tx = db.scalar(
        select(PaymentTransaction).where(
            PaymentTransaction.id == transaction_id,
            PaymentTransaction.user_id == current_user.id,
        )
    )
    if not tx:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        )

    return {
        "id": tx.id,
        "external_id": tx.external_id,
        "provider": tx.provider,
        "provider_mode": tx.provider_mode,
        "plan": tx.plan,
        "amount": tx.amount,
        "currency": tx.currency,
        "status": tx.status,
        "payment_method": tx.payment_method,
        "payment_channel": tx.payment_channel,
        "checkout_url": tx.checkout_url,
        "va_number": getattr(tx, "va_number", None),
        "qr_string": tx.qr_string,
        "expires_at": tx.expires_at.isoformat() if tx.expires_at else None,
        "paid_at": tx.paid_at.isoformat() if tx.paid_at else None,
        "created_at": tx.created_at.isoformat(),
        "updated_at": tx.updated_at.isoformat(),
    }


@router.get("/transactions/{transaction_id}/status", summary="Poll transaction settlement status")
def get_transaction_status(
    transaction_id: int,
    sync: bool = Query(default=False, description="Force sync with provider if pending"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Returns the settlement status of a transaction and whether Pro is active.

    If status is PENDING and credentials exist, performs an authoritative
    sync check with the gateway in case the webhook was delayed.
    """
    api_rate_limiter.check_or_raise(
        f"billing_poll:{current_user.id}",
        max_requests=60,
        window_seconds=60.0,
        error_detail="Too many status polling requests. Please wait before polling again.",
    )
    tx = db.scalar(
        select(PaymentTransaction).where(
            PaymentTransaction.id == transaction_id,
            PaymentTransaction.user_id == current_user.id,
        )
    )
    if not tx:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found.",
        )

    now = utcnow()
    # Reconcile authoritatively if PENDING and credentials exist.
    # Throttle external calls to Duitku to at most once every 10 seconds per transaction unless sync is requested
    if tx.status == "PENDING":
        if tx.provider == "duitku" and bool(get_duitku_api_key()):
            tx_updated = ensure_utc(tx.updated_at)
            seconds_since_update = (now - tx_updated).total_seconds() if tx_updated else 999.0
            if sync or seconds_since_update >= 10.0:
                try:
                    reconcile_and_process_payment(db=db, transaction_id=tx.id)
                    db.refresh(tx)
                except Exception as err:
                    logger.warning(f"Authoritative Duitku reconciliation skipped during polling for tx {tx.id}: {err}")
                    tx.updated_at = now
                    db.commit()
        elif tx.provider == "xendit" and tx.provider_payment_id:
            from app.services.xendit_service import get_xendit_secret_key
            if get_xendit_secret_key():
                try:
                    reconcile_and_process_payment(db=db, transaction_id=tx.id)
                    db.refresh(tx)
                except Exception as err:
                    logger.warning(f"Authoritative Xendit reconciliation skipped during polling for tx {tx.id}: {err}")

    effective_plan, sub = get_effective_plan(db, current_user.id)

    return {
        "id": tx.id,
        "external_id": tx.external_id,
        "provider": tx.provider,
        "provider_mode": tx.provider_mode,
        "status": tx.status,
        "plan": tx.plan,
        "amount": tx.amount,
        "currency": tx.currency,
        "payment_method": tx.payment_method,
        "payment_channel": tx.payment_channel,
        "checkout_url": tx.checkout_url,
        "va_number": getattr(tx, "va_number", None),
        "qr_string": tx.qr_string,
        "paid_at": tx.paid_at.isoformat() if tx.paid_at else None,
        "is_pro_active": effective_plan == PRO_PLAN_ID,
        "current_period_end": sub.current_period_end.isoformat() if (sub and sub.current_period_end) else None,
    }
