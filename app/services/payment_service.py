import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.payment_transaction import PaymentTransaction
from app.models.user import User
from app.services.subscription_service import activate_pro_from_payment, utcnow
from app.services.xendit_service import (
    PRO_SUBSCRIPTION_AMOUNT_IDR,
    PRO_SUBSCRIPTION_DURATION_DAYS,
    XenditApiError,
    XenditConfigurationError,
    get_xendit_invoice,
)

logger = logging.getLogger("raytech.payment_service")

# State machine defining valid transitions
# Terminal states: PAID cannot transition to anything else.
# EXPIRED/FAILED/CANCELED can only transition to PAID if authoritative provider confirms settlement.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"PAID", "EXPIRED", "FAILED", "CANCELED"},
    "EXPIRED": {"PAID"},
    "FAILED": {"PAID"},
    "CANCELED": {"PAID"},
    "PAID": set(),
}


def parse_iso_datetime(dt_str: Any) -> datetime | None:
    if not dt_str or not isinstance(dt_str, str):
        return None
    try:
        clean = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(clean)
    except Exception:
        return None


def reconcile_and_process_payment(
    db: Session,
    transaction_id: int,
    expected_external_id: str | None = None,
    expected_invoice_id: str | None = None,
    webhook_payload: dict[str, Any] | None = None,
) -> tuple[str, PaymentTransaction]:
    """Authoritative reconciliation and atomic state transition for a payment transaction.

    Acquires a pessimistic database row lock (`with_for_update()`) to prevent race
    conditions between concurrent webhook deliveries and user polling.

    Authoritatively queries Xendit's authenticated API to verify payment proof,
    preventing any spoofing or forged webhook payloads. Updates transaction status
    and activates Pro entitlements in a single atomic commit.
    """
    # 1. Pessimistic lock on the transaction row
    tx = db.scalar(
        select(PaymentTransaction)
        .where(PaymentTransaction.id == transaction_id)
        .with_for_update()
    )
    if not tx:
        logger.warning(f"reconcile_and_process_payment called for unknown tx {transaction_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Transaction not found"},
        )

    # 2. Strict Idempotency: if already settled as PAID, do not extend twice
    if tx.status == "PAID":
        logger.info(f"Transaction {tx.id} ({tx.external_id}) is already PAID. Duplicate ignored.")
        return "already_paid", tx

    # 3. Identity verification against payload / expectations
    if expected_external_id and tx.external_id != expected_external_id:
        logger.error(f"External ID mismatch on tx {tx.id}: expected {tx.external_id}, received {expected_external_id}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "External ID mismatch"},
        )

    if (
        expected_invoice_id
        and tx.provider_payment_id
        and tx.provider_payment_id != expected_invoice_id
    ):
        logger.error(f"Invoice ID mismatch on tx {tx.id}: stored {tx.provider_payment_id}, received {expected_invoice_id}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "Invoice ID mismatch"},
        )

    # 4. Determine target provider invoice ID
    target_invoice_id = tx.provider_payment_id or expected_invoice_id
    if not target_invoice_id:
        logger.error(f"No invoice ID associated with tx {tx.id} for authoritative verification")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Missing invoice ID for payment verification"},
        )

    # 5. Authoritatively retrieve invoice from Xendit's authenticated API
    # Propagates XenditApiError (502/504) if Xendit is unreachable, causing webhook to fail retryably
    invoice = get_xendit_invoice(target_invoice_id)

    # 6. Authoritative Invoice Identity & Data Integrity Verification
    authoritative_id = str(invoice.get("id", "")).strip()
    if authoritative_id != target_invoice_id:
        logger.error(f"Authoritative ID mismatch: expected {target_invoice_id}, got {authoritative_id}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "Authoritative invoice ID mismatch"},
        )

    authoritative_external_id = str(invoice.get("external_id", "")).strip()
    if authoritative_external_id != tx.external_id:
        logger.error(f"Authoritative external_id mismatch: expected {tx.external_id}, got {authoritative_external_id}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "Authoritative external_id mismatch"},
        )

    authoritative_currency = str(invoice.get("currency", "")).strip().upper()
    if not authoritative_currency or authoritative_currency != "IDR":
        logger.error(f"Authoritative currency mismatch on tx {tx.id}: expected IDR, got '{authoritative_currency}'")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "Currency must be IDR"},
        )

    try:
        authoritative_amount = int(invoice.get("amount", 0))
    except (ValueError, TypeError):
        authoritative_amount = 0

    if authoritative_amount != PRO_SUBSCRIPTION_AMOUNT_IDR:
        logger.error(f"Authoritative amount mismatch on tx {tx.id}: expected {PRO_SUBSCRIPTION_AMOUNT_IDR}, got {authoritative_amount}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": f"Amount must be IDR {PRO_SUBSCRIPTION_AMOUNT_IDR}"},
        )

    authoritative_status = str(invoice.get("status", "")).strip().upper()

    # 7. State Machine Execution & Atomic Transaction Commit
    if authoritative_status in ("PAID", "SETTLED"):
        # Verify valid transition
        allowed = VALID_TRANSITIONS.get(tx.status, set())
        if "PAID" not in allowed:
            logger.warning(f"Disallowed state transition from {tx.status} to PAID for tx {tx.id}")
            return "invalid_transition", tx

        now = utcnow()
        tx.status = "PAID"
        tx.provider_payment_id = authoritative_id
        tx.payment_method = invoice.get("payment_method") or (webhook_payload.get("payment_method") if webhook_payload else None)
        tx.payment_channel = invoice.get("payment_channel") or (webhook_payload.get("payment_channel") if webhook_payload else None)
        tx.paid_at = parse_iso_datetime(invoice.get("paid_at")) or now
        tx.updated_at = now

        # Update subscription atomically without committing inside helper
        sub = activate_pro_from_payment(
            db=db,
            user_id=tx.user_id,
            transaction_id=tx.id,
            duration_days=PRO_SUBSCRIPTION_DURATION_DAYS,
            commit=False,
        )

        # Enqueue payment confirmation email in the same atomic database transaction
        user = db.get(User, tx.user_id)
        if user:
            from app.services.email_service import enqueue_payment_confirmation_email
            enqueue_payment_confirmation_email(
                db=db,
                user=user,
                transaction=tx,
                subscription=sub,
            )

        # Single atomic database commit for payment_transactions, user_subscriptions, and email_deliveries
        db.commit()
        db.refresh(tx)
        logger.info(f"Authoritative settlement successful: user {tx.user_id} Pro activated via tx {tx.id}")
        return "activated", tx

    elif authoritative_status == "EXPIRED":
        if tx.status == "PENDING":
            tx.status = "EXPIRED"
            tx.updated_at = utcnow()
            db.commit()
            db.refresh(tx)
        return "expired", tx

    elif authoritative_status in ("FAILED", "CANCELED"):
        if tx.status == "PENDING":
            tx.status = "FAILED"
            tx.updated_at = utcnow()
            db.commit()
            db.refresh(tx)
        return "failed", tx

    elif authoritative_status == "PENDING":
        # If incoming webhook payload forged PAID but authoritative status is PENDING, reject
        if webhook_payload and str(webhook_payload.get("status", "")).upper() in ("PAID", "SETTLED"):
            logger.warning(f"Forged PAID webhook for tx {tx.id}: authoritative invoice is still PENDING")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "Authoritative invoice status is still PENDING"},
            )
        return "pending", tx

    return "unhandled", tx
