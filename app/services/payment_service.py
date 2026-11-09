import hmac
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.payment_transaction import PaymentTransaction
from app.models.user import User
from app.services.duitku_service import (
    DuitkuApiError,
    DuitkuConfigurationError,
    get_duitku_transaction_status,
    interpret_duitku_status,
)
from app.services.qrisgate_service import (
    QRISGateApiError,
    QRISGateConfigurationError,
    get_qrisgate_api_key,
    get_qrisgate_merchant_id,
    get_qrisgate_transaction,
)
from app.services.subscription_service import activate_pro_from_payment, utcnow
from app.services.product_event_service import record_product_event
from app.services.xendit_service import (
    PRO_SUBSCRIPTION_AMOUNT_IDR,
    PRO_SUBSCRIPTION_DURATION_DAYS,
    XenditApiError,
    XenditConfigurationError,
    get_xendit_invoice,
)

logger = logging.getLogger("raytech.payment_service")

# Only pending payments may transition. Terminal states never reopen.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"PAID", "EXPIRED", "FAILED"},
    "EXPIRED": set(),
    "FAILED": set(),
    "CANCELED": set(),
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


def qrisgate_external_order_matches(authoritative: str, expected: str) -> bool:
    if authoritative == expected:
        return True

    suffix_length = 4
    if len(authoritative) <= suffix_length or len(expected) < suffix_length:
        return False

    masked_prefix = authoritative[:-suffix_length]
    suffix = authoritative[-suffix_length:]
    return (
        len(masked_prefix) >= suffix_length
        and set(masked_prefix) == {"•"}
        and hmac.compare_digest(suffix, expected[-suffix_length:])
    )


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

    Authoritatively queries the respective provider's (Duitku or Xendit) authenticated API
    to verify payment proof, preventing any spoofing or forged webhook payloads.
    Updates transaction status and activates Pro entitlements in a single atomic commit.
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

    # --- QRISGATE RECONCILIATION PATH ---
    if tx.provider == "qrisgate":
        if not get_qrisgate_api_key():
            logger.error("QRISGate reconciliation unavailable: API key is not configured")
            raise QRISGateConfigurationError("QRISGate API key is not configured")

        target_id = tx.provider_payment_id or tx.external_id
        try:
            status_data = get_qrisgate_transaction(target_id)
        except (QRISGateApiError, QRISGateConfigurationError):
            logger.error("Authoritative QRISGate lookup failed for tx %s", tx.id)
            raise
        except Exception as exc:
            logger.error("Unexpected QRISGate lookup failure for tx %s: %s", tx.id, type(exc).__name__)
            raise QRISGateApiError("Authoritative QRISGate verification failed") from exc

        authoritative_id = str(status_data.get("id") or "").strip()
        authoritative_external_id = str(status_data.get("external_order_id") or "").strip()
        if not tx.provider_payment_id or authoritative_id != tx.provider_payment_id:
            logger.error("QRISGate provider reference mismatch for tx %s", tx.id)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "Authoritative provider reference mismatch"},
            )
        if not qrisgate_external_order_matches(authoritative_external_id, tx.external_id):
            logger.error("QRISGate external order mismatch for tx %s", tx.id)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "Authoritative external order mismatch"},
            )
        if expected_invoice_id and authoritative_id != expected_invoice_id:
            logger.error("QRISGate callback provider reference mismatch for tx %s", tx.id)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "Callback provider reference mismatch"},
            )

        expected_merchant_id = get_qrisgate_merchant_id()
        authoritative_merchant_id = str(status_data.get("merchant_id") or "").strip()
        if expected_merchant_id and authoritative_merchant_id != expected_merchant_id:
            logger.error("QRISGate merchant mismatch for tx %s", tx.id)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "Authoritative merchant mismatch"},
            )

        currency = str(status_data.get("currency") or tx.currency).strip().upper()
        if currency != "IDR" or tx.currency.upper() != "IDR":
            logger.error("QRISGate currency mismatch for tx %s", tx.id)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "Currency must be IDR"},
            )

        authoritative_amount = status_data.get("amount")
        authoritative_total = status_data.get("total_amount")
        expected_total = tx.total_amount if tx.total_amount is not None else tx.amount
        try:
            amount_matches = authoritative_amount is None or int(authoritative_amount) == tx.amount
            total_matches = authoritative_total is None or int(authoritative_total) == expected_total
        except (TypeError, ValueError):
            amount_matches = False
            total_matches = False
        has_authoritative_amount = authoritative_amount is not None or authoritative_total is not None
        if not has_authoritative_amount or not amount_matches or not total_matches:
            logger.error("QRISGate amount mismatch for tx %s", tx.id)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "Authoritative payment amount mismatch"},
            )

        normalized_status = status_data.get("status")
        paid_at_time = parse_iso_datetime(status_data.get("paid_at"))

        if normalized_status == "PAID":
            if "PAID" not in VALID_TRANSITIONS.get(tx.status, set()):
                logger.warning("Rejected QRISGate terminal-state transition for tx %s from %s", tx.id, tx.status)
                return "invalid_transition", tx

            now = utcnow()
            tx.status = "PAID"
            tx.paid_at = paid_at_time or now
            tx.updated_at = now

            sub = activate_pro_from_payment(
                db=db,
                user_id=tx.user_id,
                transaction_id=tx.id,
                duration_days=PRO_SUBSCRIPTION_DURATION_DAYS,
                commit=False,
                provider="qrisgate",
            )
            tx.subscription_id = sub.id

            user = db.get(User, tx.user_id)
            if user:
                from app.services.email_service import enqueue_payment_confirmation_email
                enqueue_payment_confirmation_email(
                    db=db,
                    user=user,
                    transaction=tx,
                    subscription=sub,
                )

            record_product_event(
                db,
                event_name="payment_succeeded",
                user_id=tx.user_id,
                source="payment",
                properties={
                    "transaction_id": tx.id,
                    "provider": tx.provider,
                    "amount": tx.amount,
                    "currency": tx.currency,
                },
                idempotency_key=f"payment_succeeded:{tx.id}",
            )
            db.commit()
            db.refresh(tx)
            logger.info("QRISGate payment settlement successful for user %s via tx %s", tx.user_id, tx.id)
            return "activated", tx

        if normalized_status in ("FAILED", "EXPIRED"):
            if tx.status == "PENDING":
                tx.status = normalized_status
                tx.updated_at = utcnow()
                analytics_event = "payment_failed" if normalized_status == "FAILED" else "payment_expired"
                properties: dict[str, Any] = {
                    "transaction_id": tx.id,
                    "provider": tx.provider,
                }
                if normalized_status == "FAILED":
                    properties["reason_code"] = "provider_failed"
                record_product_event(
                    db,
                    event_name=analytics_event,
                    user_id=tx.user_id,
                    source="payment",
                    properties=properties,
                    idempotency_key=f"{analytics_event}:{tx.id}",
                )
                db.commit()
                db.refresh(tx)
            return "failed" if normalized_status == "FAILED" else "expired", tx

        tx.updated_at = utcnow()
        db.commit()
        db.refresh(tx)
        return "pending", tx

    # --- DUITKU RECONCILIATION PATH ---
    if tx.provider == "duitku":
        target_order_id = tx.external_id
        status_data = get_duitku_transaction_status(target_order_id)

        authoritative_order_id = str(status_data.get("merchantOrderId", "")).strip()
        if authoritative_order_id != tx.external_id:
            logger.error(f"Duitku authoritative order ID mismatch: expected {tx.external_id}, got {authoritative_order_id}")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "Authoritative merchantOrderId mismatch"},
            )

        try:
            authoritative_amount = int(float(status_data.get("amount", 0)))
        except (ValueError, TypeError):
            authoritative_amount = 0

        if authoritative_amount != PRO_SUBSCRIPTION_AMOUNT_IDR:
            logger.error(f"Duitku authoritative amount mismatch on tx {tx.id}: expected {PRO_SUBSCRIPTION_AMOUNT_IDR}, got {authoritative_amount}")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": f"Amount must be IDR {PRO_SUBSCRIPTION_AMOUNT_IDR}"},
            )

        normalized_status, raw_status_code = interpret_duitku_status(status_data)

        if normalized_status == "PAID":
            allowed = VALID_TRANSITIONS.get(tx.status, set())
            if "PAID" not in allowed:
                logger.warning(f"Disallowed state transition from {tx.status} to PAID for tx {tx.id}")
                return "invalid_transition", tx

            now = utcnow()
            tx.status = "PAID"
            tx.provider_payment_id = status_data.get("reference") or tx.provider_payment_id
            tx.payment_method = (webhook_payload.get("paymentCode") if webhook_payload else None) or tx.payment_method or "QRIS"
            tx.payment_channel = "duitku"
            tx.paid_at = now
            tx.updated_at = now

            sub = activate_pro_from_payment(
                db=db,
                user_id=tx.user_id,
                transaction_id=tx.id,
                duration_days=PRO_SUBSCRIPTION_DURATION_DAYS,
                commit=False,
            )

            user = db.get(User, tx.user_id)
            if user:
                from app.services.email_service import enqueue_payment_confirmation_email
                enqueue_payment_confirmation_email(
                    db=db,
                    user=user,
                    transaction=tx,
                    subscription=sub,
                )

            db.commit()
            db.refresh(tx)
            logger.info(f"Duitku authoritative settlement successful: user {tx.user_id} Pro activated via tx {tx.id}")
            return "activated", tx

        elif normalized_status in ("FAILED", "EXPIRED"):
            if tx.status == "PENDING":
                tx.status = normalized_status
                tx.updated_at = utcnow()
                db.commit()
                db.refresh(tx)
            return "failed" if normalized_status == "FAILED" else "expired", tx

        else:  # PENDING
            if webhook_payload and (
                str(webhook_payload.get("resultCode", "")) == "00"
                or str(webhook_payload.get("status", "")).upper() in ("PAID", "SETTLED")
            ):
                logger.warning(f"Forged PAID webhook for Duitku tx {tx.id}: authoritative status is still PENDING")
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"error": "Authoritative invoice status is still PENDING"},
                )
            tx.updated_at = utcnow()
            db.commit()
            db.refresh(tx)
            return "pending", tx

    # --- XENDIT RECONCILIATION PATH ---
    # 4. Determine target provider invoice ID
    target_invoice_id = tx.provider_payment_id or expected_invoice_id
    if not target_invoice_id:
        logger.error(f"No invoice ID associated with tx {tx.id} for authoritative verification")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Missing invoice ID for payment verification"},
        )

    # 5. Authoritatively retrieve invoice from Xendit's authenticated API
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

        sub = activate_pro_from_payment(
            db=db,
            user_id=tx.user_id,
            transaction_id=tx.id,
            duration_days=PRO_SUBSCRIPTION_DURATION_DAYS,
            commit=False,
            provider="xendit",
        )

        user = db.get(User, tx.user_id)
        if user:
            from app.services.email_service import enqueue_payment_confirmation_email
            enqueue_payment_confirmation_email(
                db=db,
                user=user,
                transaction=tx,
                subscription=sub,
            )

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
        if webhook_payload and str(webhook_payload.get("status", "")).upper() in ("PAID", "SETTLED"):
            logger.warning(f"Forged PAID webhook for tx {tx.id}: authoritative invoice is still PENDING")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "Authoritative invoice status is still PENDING"},
            )
        return "pending", tx

    return "unhandled", tx

