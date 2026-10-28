import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.services.subscription_service import (
    PLAN_CONFIG,
    get_entitlements,
    get_or_create_subscription,
    get_subscription_usage,
    switch_user_plan_dev,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/subscription", tags=["subscription"])


class SwitchPlanRequest(BaseModel):
    plan: str = Field(description="Plan to switch to: 'free' or 'pro'")
    reason: str = Field(default="dev_testing", description="Reason for plan change")


def is_dev_switching_allowed() -> bool:
    """Checks whether development plan switching is permitted.

    Strictly disabled in production and defaults to closed.
    """
    env = os.environ.get("ENVIRONMENT", "").strip().lower()
    if env in ("production", "prod"):
        return False
    is_test_env = bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("TESTING", "").lower() in ("true", "1")
    )
    default_flag = "true" if is_test_env else "false"
    allow_flag = os.environ.get("ALLOW_DEV_PLAN_SWITCH", default_flag).lower()
    return allow_flag in ("1", "true", "yes")


@router.get("", summary="Get current user subscription details")
def get_user_subscription(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Returns effective plan, subscription status, entitlements, and billing summary."""
    from sqlalchemy import func, select
    from app.models.payment_transaction import PaymentTransaction

    sub = get_or_create_subscription(db, current_user.id)
    entitlements = get_entitlements(db, current_user.id)
    plan_id = entitlements["id"]

    is_pro = plan_id == "pro"

    # Query real paid transactions
    tx_count = (
        db.scalar(
            select(func.count(PaymentTransaction.id)).where(
                PaymentTransaction.user_id == current_user.id,
                PaymentTransaction.status == "PAID",
            )
        )
        or 0
    )

    latest_paid_tx = db.scalar(
        select(PaymentTransaction)
        .where(
            PaymentTransaction.user_id == current_user.id,
            PaymentTransaction.status == "PAID",
        )
        .order_by(PaymentTransaction.paid_at.desc())
        .limit(1)
    )

    if is_pro:
        if latest_paid_tx:
            if latest_paid_tx.provider == "duitku":
                mode_str = " (Sandbox)" if latest_paid_tx.provider_mode == "sandbox" else ""
                method_name = latest_paid_tx.payment_method or "QRIS"
                payment_method = f"Duitku {method_name}{mode_str}"
            elif latest_paid_tx.provider == "xendit":
                channel_info = f" ({latest_paid_tx.payment_channel})" if latest_paid_tx.payment_channel else ""
                payment_method = f"Xendit (Historical){channel_info}"
            else:
                channel_info = f" ({latest_paid_tx.payment_channel})" if latest_paid_tx.payment_channel else ""
                payment_method = f"{latest_paid_tx.provider}{channel_info}"
        elif getattr(sub, "legacy_override", False):
            payment_method = "Legacy Grandfathered"
        else:
            payment_method = "Not configured"
        billing_cycle = "30-day Prepaid"
    else:
        payment_method = "Not required"
        billing_cycle = "N/A"

    from datetime import datetime, timezone
    from app.services.email_service import format_wib_datetime

    now = datetime.now(timezone.utc)
    remaining_seconds = 0
    if is_pro and sub.current_period_end:
        end_tz = sub.current_period_end if sub.current_period_end.tzinfo else sub.current_period_end.replace(tzinfo=timezone.utc)
        remaining_seconds = max(0, int((end_tz - now).total_seconds()))

    end_wib = format_wib_datetime(sub.current_period_end) if sub.current_period_end else None
    is_expired_pro = (sub.status == "expired") or (not is_pro and tx_count > 0)

    from app.services.duitku_service import is_user_allowed_sandbox_checkout

    sandbox_checkout_allowed = is_user_allowed_sandbox_checkout(current_user)

    billing_summary = {
        "plan_id": plan_id,
        "plan_name": entitlements["name"],
        "status": sub.status,
        "status_display": "Pro Active" if is_pro else ("Plan Expired" if is_expired_pro else "Free Active"),
        "payment_method": payment_method,
        "billing_cycle": billing_cycle,
        "renewal_date": sub.current_period_end.isoformat() if (is_pro and sub.current_period_end) else None,
        "invoices_count": tx_count,
        "auto_renew": False,
        "checkout_allowed": sandbox_checkout_allowed,
        "checkout_mode": "sandbox",
    }

    return {
        "plan": plan_id,
        "name": entitlements["name"],
        "status": sub.status,
        "price_display": entitlements["price_display"],
        "billing_period": entitlements["billing_period"],
        "description": entitlements["description"],
        "started_at": sub.started_at.isoformat() if sub.started_at else None,
        "current_period_start": sub.current_period_start.isoformat() if sub.current_period_start else None,
        "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
        "current_period_end_wib": end_wib,
        "remaining_seconds": remaining_seconds,
        "is_expired_pro": is_expired_pro,
        "auto_renew": False,
        "cancel_at_period_end": sub.cancel_at_period_end,
        "legacy_override": sub.legacy_override,
        "upgrade_available": not is_pro,
        "sandbox_checkout_allowed": sandbox_checkout_allowed,
        "payment_method": payment_method,
        "billing_cycle": billing_cycle,
        "renewal_date": sub.current_period_end.isoformat() if (is_pro and sub.current_period_end) else None,
        "invoices_count": tx_count,
        "billing_summary": billing_summary,
        "entitlements": {
            "max_http_monitors": entitlements["max_http_monitors"],
            "min_http_interval_seconds": entitlements["min_http_interval_seconds"],
            "min_http_interval_display": entitlements["min_http_interval_display"],
            "max_heartbeat_monitors": entitlements["max_heartbeat_monitors"],
            "max_status_pages": entitlements["max_status_pages"],
            "ssl_monitoring": entitlements["ssl_monitoring"],
            "email_alerts": entitlements["email_alerts"],
            "in_app_notifications": entitlements["in_app_notifications"],
            "incident_history_days": entitlements["incident_history_days"],
        },
    }


@router.get("/usage", summary="Get current usage vs plan quota")
def get_user_subscription_usage(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Returns real monitor counts, heartbeat count, status page count, limits, and over_limit flags."""
    return get_subscription_usage(db, current_user.id)


@router.get("/plans", summary="Get available subscription plans")
def get_available_plans() -> dict[str, Any]:
    """Returns plan definitions for public or authenticated presentation."""
    return {
        "plans": [
            PLAN_CONFIG["free"],
            PLAN_CONFIG["pro"],
        ]
    }


@router.post("/admin/switch-plan", summary="Development-only plan switcher")
def switch_plan_development(
    payload: SwitchPlanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Allows development/testing plan switching. Strictly disabled in production."""
    if not is_dev_switching_allowed():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Development plan switching is disabled in production.",
        )

    updated_sub = switch_user_plan_dev(
        db,
        user_id=current_user.id,
        new_plan=payload.plan,
        reason=payload.reason,
    )

    return {
        "message": f"Successfully switched user plan to '{updated_sub.plan}'.",
        "plan": updated_sub.plan,
        "status": updated_sub.status,
        "user_id": current_user.id,
    }
