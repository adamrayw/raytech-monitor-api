import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.payment_transaction import PaymentTransaction
from app.services.duitku_service import (
    DuitkuApiError,
    DuitkuConfigurationError,
    verify_duitku_callback_signature,
)
from app.services.payment_service import reconcile_and_process_payment
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
