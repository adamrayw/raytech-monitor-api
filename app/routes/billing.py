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
from app.services.duitku_service import get_duitku_api_key
from app.services.payment_service import reconcile_and_process_payment
from app.services.qrisgate_service import (
    PRO_PLAN_ID,
    PRO_SUBSCRIPTION_AMOUNT_IDR,
    PRO_SUBSCRIPTION_DURATION_DAYS,
    QRISGateApiError,
    QRISGateConfigurationError,
    QRISGateInsufficientCreditError,
    QRISGateRateLimitError,
    QRISGateValidationError,
    create_qrisgate_transaction,
    generate_qrisgate_order_id,
    get_frontend_url,
    get_qrisgate_api_key,
    get_qrisgate_payment_methods,
)
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
    """Returns the list of available payment methods for QRISGate gateway."""
    api_rate_limiter.check_or_raise(
        f"billing_methods:{current_user.id}",
        max_requests=60,
        window_seconds=60.0,
        error_detail="Too many payment method requests. Please wait before refreshing.",
    )
    enforced_amount = PRO_SUBSCRIPTION_AMOUNT_IDR
    methods = get_qrisgate_payment_methods()
    categories = [{"key": "qr", "label": "QR Payment"}]

    return {
        "payment_methods": methods,
        "categories": categories,
        "provider": "qrisgate",
        "provider_mode": "live",
        "base_amount": enforced_amount,
        "currency": "IDR",
    }


class CheckoutRequest(BaseModel):
    plan: str = Field(default=PRO_PLAN_ID, description="Target plan ID ('pro')")
    provider: str = Field(default="qrisgate", description="Payment provider adapter ('qrisgate')")
    payment_method: str | None = Field(default="QRIS", description="Payment method ('QRIS' or 'BANK_TRANSFER')")
    bank_code: str | None = Field(default=None, description="Bank code if BANK_TRANSFER (e.g. 'BCA')")
    customer_name: str | None = Field(default=None, description="Customer name")
    return_url: str | None = Field(default=None, description="URL to return to after payment")
    items: list[dict[str, Any]] | None = Field(default=None, description="Order items")


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
    """Creates a local pending payment transaction and requests a checkout session from QRISGate.

    QRISGate is the sole active payment provider.
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

    requested_provider = (payload.provider or "qrisgate").strip().lower()
    if requested_provider != "qrisgate":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "UNSUPPORTED_PROVIDER",
                "message": "QRISGate adalah gateway pembayaran aktif.",
            },
        )

    requested_method = (payload.payment_method or "QRIS").strip().upper()

    # 1. Idempotency protection:
    # If a pending transaction exists created within last 15 minutes for QRISGate with valid checkout_url, reuse it.
    fifteen_mins_ago = utcnow() - timedelta(minutes=15)
    existing_pending = db.scalar(
        select(PaymentTransaction)
        .where(
            PaymentTransaction.user_id == current_user.id,
            PaymentTransaction.status == "PENDING",
            PaymentTransaction.provider == "qrisgate",
            PaymentTransaction.created_at >= fifteen_mins_ago,
            PaymentTransaction.checkout_url.isnot(None),
        )
        .order_by(PaymentTransaction.created_at.desc())
        .limit(1)
    )

    if existing_pending and existing_pending.checkout_url:
        logger.info(f"Reusing active pending checkout tx {existing_pending.id} for user {current_user.id}")
        data_obj = {
            "checkout_url": existing_pending.checkout_url,
            "total_amount": existing_pending.total_amount or existing_pending.amount,
            "unique_code": existing_pending.unique_code or 0,
            "qr_string": existing_pending.qr_string,
            "expires_at": existing_pending.expires_at.isoformat() if existing_pending.expires_at else None,
            "external_order_id": existing_pending.external_id,
            "id": existing_pending.provider_payment_id or existing_pending.id,
        }
        return {
            "data": data_obj,
            "transaction_id": existing_pending.id,
            "external_id": existing_pending.external_id,
            "provider": existing_pending.provider,
            "provider_mode": existing_pending.provider_mode,
            "payment_method": existing_pending.payment_method,
            "payment_channel": existing_pending.payment_channel,
            "checkout_url": existing_pending.checkout_url,
            "qr_string": existing_pending.qr_string,
            "amount": existing_pending.amount,
            "total_amount": existing_pending.total_amount or existing_pending.amount,
            "unique_code": existing_pending.unique_code or 0,
            "currency": existing_pending.currency,
            "status": existing_pending.status,
            "expires_at": existing_pending.expires_at.isoformat() if existing_pending.expires_at else None,
        }

    # 2. Generate unique external order reference
    external_id = generate_qrisgate_order_id(current_user.id)

    # 3. Create local pending transaction record (authoritative price: IDR 49,000)
    tx = PaymentTransaction(
        user_id=current_user.id,
        plan=PRO_PLAN_ID,
        amount=PRO_SUBSCRIPTION_AMOUNT_IDR,
        currency="IDR",
        status="PENDING",
        provider="qrisgate",
        provider_mode="live",
        revenue_classification="REAL",
        payment_method=requested_method,
        payment_channel="qrisgate",
        external_id=external_id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    # 4. Initiate provider checkout via QRISGate API
    try:
        effective_return_url = f"{get_frontend_url()}/order/{tx.external_id}"
        qrg_resp = create_qrisgate_transaction(
            external_order_id=tx.external_id,
            amount=tx.amount,
            customer_name=payload.customer_name or current_user.name or "Raytech Subscriber",
            return_url=effective_return_url,
            payment_method=requested_method,
            bank_code=payload.bank_code,
        )
    except QRISGateInsufficientCreditError as exc:
        tx.status = "FAILED"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"code": "MERCHANT_CREDIT_EXHAUSTED", "message": str(exc)},
        )
    except QRISGateValidationError as exc:
        tx.status = "FAILED"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "GATEWAY_VALIDATION_ERROR", "message": str(exc)},
        )
    except QRISGateRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "GATEWAY_RATE_LIMIT", "message": str(exc)},
        )
    except QRISGateConfigurationError as exc:
        tx.status = "FAILED"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "GATEWAY_CONFIG_ERROR", "message": str(exc)},
        )
    except QRISGateApiError as exc:
        tx.status = "FAILED"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "GATEWAY_API_ERROR", "message": str(exc)},
        )

    # 5. Persist provider metadata
    tx.provider_payment_id = qrg_resp.get("id")
    tx.checkout_url = qrg_resp.get("checkout_url")
    tx.qr_string = qrg_resp.get("qr_string")
    tx.total_amount = qrg_resp.get("total_amount")
    tx.unique_code = qrg_resp.get("unique_code")

    raw_exp = qrg_resp.get("expires_at")
    parsed_exp = parse_iso_datetime(raw_exp) if raw_exp else None
    tx.expires_at = parsed_exp or (utcnow() + timedelta(minutes=15))
    tx.updated_at = utcnow()
    db.commit()
    db.refresh(tx)

    data_obj = {
        "checkout_url": tx.checkout_url,
        "total_amount": tx.total_amount or tx.amount,
        "unique_code": tx.unique_code or 0,
        "qr_string": tx.qr_string,
        "expires_at": tx.expires_at.isoformat() if tx.expires_at else None,
        "external_order_id": tx.external_id,
        "id": tx.provider_payment_id or tx.id,
    }

    return {
        "data": data_obj,
        "transaction_id": tx.id,
        "external_id": tx.external_id,
        "provider": tx.provider,
        "provider_mode": tx.provider_mode,
        "payment_method": tx.payment_method,
        "payment_channel": tx.payment_channel,
        "checkout_url": tx.checkout_url,
        "qr_string": tx.qr_string,
        "amount": tx.amount,
        "total_amount": tx.total_amount or tx.amount,
        "unique_code": tx.unique_code or 0,
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
            "total_amount": tx.total_amount or tx.amount,
            "unique_code": tx.unique_code or 0,
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
        "total_amount": tx.total_amount or tx.amount,
        "unique_code": tx.unique_code or 0,
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
    if tx.status == "PENDING":
        if tx.provider == "qrisgate" and bool(get_qrisgate_api_key()):
            tx_updated = ensure_utc(tx.updated_at)
            seconds_since_update = (now - tx_updated).total_seconds() if tx_updated else 999.0
            if sync or seconds_since_update >= 2.0:
                try:
                    reconcile_and_process_payment(db=db, transaction_id=tx.id)
                    db.refresh(tx)
                except Exception as err:
                    logger.warning(f"Authoritative QRISGate reconciliation skipped during polling for tx {tx.id}: {err}")
                    tx.updated_at = now
                    db.commit()
        elif tx.provider == "duitku" and bool(get_duitku_api_key()):
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
        "total_amount": tx.total_amount or tx.amount,
        "unique_code": tx.unique_code or 0,
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


@router.get("/order/{order_id}", summary="Get order details by external order ID or transaction ID")
def get_order_by_id(
    order_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Retrieves an authenticated user's order status and reconciles it when pending."""
    api_rate_limiter.check_or_raise(
        f"billing_order:{current_user.id}",
        max_requests=60,
        window_seconds=60.0,
        error_detail="Too many order status requests. Please wait before polling again.",
    )
    tx = None
    if order_id.isdigit():
        tx = db.scalar(
            select(PaymentTransaction).where(
                PaymentTransaction.id == int(order_id),
                PaymentTransaction.user_id == current_user.id,
            )
        )
    if not tx:
        tx = db.scalar(
            select(PaymentTransaction).where(
                PaymentTransaction.external_id == order_id,
                PaymentTransaction.user_id == current_user.id,
            )
        )
    if not tx:
        tx = db.scalar(
            select(PaymentTransaction).where(
                PaymentTransaction.provider_payment_id == order_id,
                PaymentTransaction.user_id == current_user.id,
            )
        )

    if not tx:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found.",
        )

    # If pending, try to sync authoritatively with QRISGate
    if tx.status == "PENDING" and tx.provider == "qrisgate" and bool(get_qrisgate_api_key()):
        try:
            reconcile_and_process_payment(db=db, transaction_id=tx.id)
            db.refresh(tx)
        except Exception as err:
            logger.warning(f"Error syncing order {order_id} with QRISGate: {err}")

    effective_plan, _ = get_effective_plan(db, tx.user_id)

    data_obj = {
        "id": tx.provider_payment_id or str(tx.id),
        "transaction_id": tx.id,
        "external_order_id": tx.external_id,
        "status": tx.status,
        "amount": tx.amount,
        "total_amount": tx.total_amount or tx.amount,
        "unique_code": tx.unique_code or 0,
        "currency": tx.currency,
        "plan": tx.plan,
        "checkout_url": tx.checkout_url,
        "qr_string": tx.qr_string,
        "paid_at": tx.paid_at.isoformat() if tx.paid_at else None,
        "expires_at": tx.expires_at.isoformat() if tx.expires_at else None,
        "is_pro_active": effective_plan == PRO_PLAN_ID,
    }

    return {
        "data": data_obj,
        "id": tx.id,
        "external_id": tx.external_id,
        "external_order_id": tx.external_id,
        "status": tx.status,
        "amount": tx.amount,
        "total_amount": tx.total_amount or tx.amount,
        "unique_code": tx.unique_code or 0,
        "currency": tx.currency,
        "plan": tx.plan,
        "checkout_url": tx.checkout_url,
        "qr_string": tx.qr_string,
        "paid_at": tx.paid_at.isoformat() if tx.paid_at else None,
        "expires_at": tx.expires_at.isoformat() if tx.expires_at else None,
        "is_pro_active": effective_plan == PRO_PLAN_ID,
    }
