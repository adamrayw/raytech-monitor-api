import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.payment_transaction import PaymentTransaction
from app.models.webhook_event import ProcessedWebhookEvent
from app.services.duitku_service import (
    DuitkuApiError,
    DuitkuConfigurationError,
    verify_duitku_callback_signature,
)
from app.services.payment_service import reconcile_and_process_payment
from app.services.qrisgate_service import (
    QRISGateApiError,
    QRISGateConfigurationError,
    verify_qrisgate_timestamp,
    verify_qrisgate_webhook_signature,
)
from app.services.xendit_service import (
    XenditApiError,
    XenditConfigurationError,
    verify_webhook_token,
)

logger = logging.getLogger("raytech.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/duitku", summary="Handle incoming Duitku payment callback notifications")
async def duitku_callback(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Callback endpoint invoked by Duitku servers upon transaction payment/status changes.

    Supports both 'application/x-www-form-urlencoded' (default Duitku POST) and JSON bodies.
    Authenticity is enforced via HMAC-SHA256 signature verification matching the official
    Duitku specification.

    Every payment notification is authoritatively verified against Duitku's Check Transaction API
    before state transitions or Pro activations are performed.
    Guarantees strict atomicity and idempotency.
    """
    # 1. Parse payload (form or JSON)
    payload: dict[str, Any] = {}
    content_type = request.headers.get("content-type", "").lower()
    try:
        if "application/json" in content_type:
            payload = await request.json()
        else:
            raw_body = await request.body()
            import urllib.parse
            parsed = urllib.parse.parse_qs(raw_body.decode("utf-8"))
            payload = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
    except Exception as exc:
        logger.error(f"Failed to parse Duitku webhook body: {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Invalid request body"},
        )

    merchant_code = str(payload.get("merchantCode") or payload.get("merchantcode") or "").strip()
    amount = payload.get("amount") or payload.get("paymentAmount")
    merchant_order_id = str(payload.get("merchantOrderId") or "").strip()
    incoming_signature = str(payload.get("signature") or "").strip()
    reference = str(payload.get("reference") or "").strip()

    if not merchant_order_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Missing merchantOrderId in Duitku callback"},
        )

    # 2. Authenticity verification
    if not verify_duitku_callback_signature(
        merchant_code=merchant_code,
        amount=amount,
        merchant_order_id=merchant_order_id,
        incoming_signature=incoming_signature,
    ):
        logger.warning(
            f"Rejected unauthorized Duitku callback: invalid signature for order {merchant_order_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Invalid or missing callback signature"},
        )

    # 3. Fast lookup to find transaction ID
    query = select(PaymentTransaction.id).where(
        PaymentTransaction.external_id == merchant_order_id
    )
    tx_id = db.scalar(query)

    if not tx_id and reference:
        tx_id = db.scalar(
            select(PaymentTransaction.id).where(
                PaymentTransaction.provider_payment_id == reference
            )
        )

    if not tx_id:
        logger.warning(f"Duitku callback received for unknown transaction: order_id={merchant_order_id}, reference={reference}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Transaction not found"},
        )

    # 4. Authoritatively reconcile and process payment state transition
    try:
        outcome, tx = reconcile_and_process_payment(
            db=db,
            transaction_id=tx_id,
            expected_external_id=merchant_order_id,
            webhook_payload=payload,
        )
    except DuitkuApiError as exc:
        logger.error(f"Duitku API error during webhook reconciliation for tx {tx_id}: {exc}")
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": str(exc)},
        )
    except DuitkuConfigurationError as exc:
        logger.error(f"Configuration error during Duitku reconciliation: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Payment gateway configuration error"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Unexpected error during Duitku webhook processing for tx {tx_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Internal error processing payment"},
        )

    logger.info(f"Duitku callback handled successfully for tx {tx.id} with outcome: {outcome}")
    # Respond with HTTP 200 OK plain text 'SUCCESS' per Duitku specification
    return Response(content="SUCCESS", media_type="text/plain", status_code=status.HTTP_200_OK)


@router.post("/xendit", summary="Handle incoming Xendit webhook notifications")
async def xendit_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_callback_token: str | None = Header(None, alias="x-callback-token"),
) -> dict[str, Any]:
    """Unauthenticated webhook endpoint invoked by Xendit servers.

    Authenticity is enforced via constant-time verification of 'x-callback-token'.
    Every payment notification is authoritatively cross-verified with Xendit's API
    before state transitions or Pro activations are performed.
    Guarantees strict atomicity and idempotency.
    """
    # 1. Authenticity verification
    if not verify_webhook_token(x_callback_token):
        logger.warning("Rejected unauthorized Xendit webhook: invalid or missing x-callback-token")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "Invalid or missing webhook verification token"},
        )

    # 2. Parse request payload
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Invalid JSON body"},
        )

    external_id = payload.get("external_id")
    invoice_id = payload.get("id")

    if not external_id and not invoice_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Missing external_id or id in webhook payload"},
        )

    # 3. Fast lookup to find transaction ID
    query = select(PaymentTransaction.id)
    if external_id:
        query = query.where(PaymentTransaction.external_id == str(external_id))
    else:
        query = query.where(PaymentTransaction.provider_payment_id == str(invoice_id))

    tx_id = db.scalar(query)
    if not tx_id:
        logger.warning(f"Webhook received for unknown transaction: external_id={external_id}, invoice_id={invoice_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "Transaction not found"},
        )

    # 4. Authoritatively reconcile and process payment state transition
    try:
        outcome, tx = reconcile_and_process_payment(
            db=db,
            transaction_id=tx_id,
            expected_external_id=str(external_id) if external_id else None,
            expected_invoice_id=str(invoice_id) if invoice_id else None,
            webhook_payload=payload,
        )
    except XenditApiError as exc:
        logger.error(f"Xendit API error during webhook reconciliation for tx {tx_id}: {exc}")
        # Return retryable gateway status (502/504) so provider re-attempts delivery
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": str(exc)},
        )
    except XenditConfigurationError as exc:
        logger.error(f"Configuration error during webhook reconciliation: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Payment gateway configuration error"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Unexpected error during webhook processing for tx {tx_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Internal error processing payment"},
        )

    if outcome == "already_paid":
        return {"status": "already_processed", "transaction_id": tx.id}
    elif outcome == "activated":
        return {"status": "processed", "activated": True, "transaction_id": tx.id}
    elif outcome == "expired":
        return {"status": "expired", "transaction_id": tx.id}
    elif outcome == "failed":
        return {"status": "failed", "transaction_id": tx.id}
    else:
        return {"status": outcome, "transaction_id": tx.id}


@router.post("/qrisgate", summary="Handle QRISGate webhook payment notifications")
async def qrisgate_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_qrisgate_signature: str | None = Header(None, alias="x-qrisgate-signature"),
    x_qrisgate_timestamp: str | None = Header(None, alias="x-qrisgate-timestamp"),
    x_qrisgate_delivery_id: str | None = Header(None, alias="x-qrisgate-delivery-id"),
) -> Response:
    """Public webhook receiver endpoint for QRISGate payment callbacks.

    Security & Reliability:
    1. Reads raw bytes before parsing JSON to guarantee exact byte fidelity for signature.
    2. Enforces timing-safe HMAC-SHA256 signature verification matching QRISGate spec.
    3. Rejects 401 if signature is invalid or x-qrisgate-timestamp > 5 minutes (anti-replay).
    4. Enforces idempotency via event_id; duplicates reply 200 without reprocessing.
    5. On payment.paid: sets status PAID, marks paid_at, and executes fulfillment (Pro activation, email).
    6. Returns 200 quickly (<5s).
    """
    # 1. Read RAW body
    raw_body = await request.body()

    # 2. Reject stale requests before authenticating timestamp + raw body.
    if not x_qrisgate_timestamp or not verify_qrisgate_timestamp(x_qrisgate_timestamp, max_age_seconds=300):
        logger.warning("Rejected QRISGate webhook: timestamp expired, missing, or invalid")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Timestamp outside acceptable 5-minute window or invalid"},
        )

    # 3. QRISGate signs the exact timestamp header, a dot, and the raw body.
    if not x_qrisgate_signature or not verify_qrisgate_webhook_signature(
        raw_body,
        x_qrisgate_signature,
        x_qrisgate_timestamp,
    ):
        logger.warning("Rejected QRISGate webhook: invalid or missing signature")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Invalid or missing signature"},
        )

    # 4. Parse JSON
    try:
        payload: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        logger.error(f"Failed to parse QRISGate webhook body as JSON: {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Invalid JSON body"},
        )

    event_id = str(x_qrisgate_delivery_id or payload.get("event_id") or "").strip()
    if len(event_id) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Invalid delivery ID"},
        )
    event_type = str(payload.get("event") or payload.get("type") or "").strip().lower()
    order_data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    # Idempotency check: if event_id already processed, reply 200 immediately
    if event_id:
        existing_event = db.scalar(
            select(ProcessedWebhookEvent).where(ProcessedWebhookEvent.event_id == event_id)
        )
        if existing_event:
            logger.info(f"QRISGate webhook event {event_id} has already been processed. Returning 200.")
            return JSONResponse(
                content={"status": "already_processed", "event_id": event_id},
                status_code=status.HTTP_200_OK,
            )

    # 5. Process payment.paid event
    if event_type == "payment.paid":
        external_order_id = str(
            order_data.get("external_order_id") or payload.get("external_order_id") or ""
        ).strip()
        transaction_id_qrg = str(
            order_data.get("id") or order_data.get("transaction_id") or payload.get("transaction_id") or ""
        ).strip()

        tx = None
        if external_order_id:
            tx = db.scalar(
                select(PaymentTransaction).where(
                    PaymentTransaction.external_id == external_order_id
                )
            )
        if not tx and transaction_id_qrg:
            tx = db.scalar(
                select(PaymentTransaction).where(
                    PaymentTransaction.provider_payment_id == transaction_id_qrg
                )
            )

        if not tx:
            logger.warning(
                "QRISGate webhook received for unknown order: external_order_id=%s, provider_id=%s",
                external_order_id,
                transaction_id_qrg,
            )
            return JSONResponse(
                content={"status": "order_not_found"},
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if tx.provider != "qrisgate":
            logger.error("QRISGate webhook provider mismatch for tx %s", tx.id)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "Transaction provider mismatch"},
            )

        transaction_db_id = tx.id
        try:
            outcome, updated_tx = reconcile_and_process_payment(
                db=db,
                transaction_id=transaction_db_id,
                expected_external_id=external_order_id or None,
                expected_invoice_id=transaction_id_qrg or None,
                webhook_payload=payload,
            )
            logger.info("QRISGate payment processed for tx %s: outcome=%s", transaction_db_id, outcome)
        except (QRISGateApiError, QRISGateConfigurationError) as exc:
            db.rollback()
            logger.error("QRISGate authoritative verification unavailable for tx %s", transaction_db_id)
            raise HTTPException(
                status_code=getattr(exc, "status_code", status.HTTP_502_BAD_GATEWAY),
                detail={"error": "Payment verification is temporarily unavailable"},
            )
        except HTTPException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error("Payment finalization failed for QRISGate tx %s: %s", transaction_db_id, type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "Internal error processing payment"},
            )

    # Record processed event ID for strict idempotency
    if event_id:
        try:
            db.add(ProcessedWebhookEvent(event_id=event_id, provider="qrisgate"))
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning(f"Could not persist processed event ID {event_id}: {exc}")

    return JSONResponse(
        content={"status": "ok", "event_id": event_id or None},
        status_code=status.HTTP_200_OK,
    )
