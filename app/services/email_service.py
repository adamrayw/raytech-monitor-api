import email.utils
import html
import json
import logging
import os
import random
from typing import Any
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.email_delivery import EmailDelivery
from app.models.monitor import Monitor
from app.models.notification import Notification
from app.models.notification_preference import NotificationPreference
from app.models.subscription import UserSubscription
from app.models.user import User

logger = logging.getLogger("raytech.email")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_frontend_url() -> str:
    return os.environ.get("FRONTEND_URL", "https://monitor.raytech.cloud").rstrip("/")


def is_email_enabled() -> bool:
    return os.environ.get("EMAIL_ENABLED", "false").lower() in ("true", "1", "yes")


def is_test_mode() -> bool:
    return os.environ.get("EMAIL_TEST_MODE", "true").lower() in ("true", "1", "yes")


def get_test_recipient() -> str:
    return os.environ.get("EMAIL_TEST_RECIPIENT", "").strip()


def get_resend_api_key() -> str:
    return os.environ.get("RESEND_API_KEY", "").strip()


def get_email_from() -> str:
    return os.environ.get(
        "EMAIL_FROM",
        "Raytech Monitor <onboarding@resend.dev>",
    ).strip()


def get_billing_email_from() -> str:
    """Returns the sender address for transactional billing confirmation emails.

    Prefers BILLING_EMAIL_FROM if configured.
    Safely handles both plain email addresses and display-name formats without
    ever producing invalid nested brackets (e.g. 'Raytech Billing <Raytech Monitor <addr>>').
    """
    configured = os.environ.get("BILLING_EMAIL_FROM", "").strip()
    if configured:
        disp, addr = email.utils.parseaddr(configured)
        if addr and not disp and "<" not in configured:
            return f"Raytech Billing <{addr}>"
        return configured

    base_from = get_email_from()
    display_name, email_address = email.utils.parseaddr(base_from)
    pure_address = email_address.strip() if email_address else base_from.strip()

    if not pure_address:
        pure_address = "onboarding@resend.dev"

    return f"Raytech Billing <{pure_address}>"


def get_email_reply_to() -> str:
    return os.environ.get("EMAIL_REPLY_TO", "").strip()


def get_resend_api_base_url() -> str:
    return os.environ.get("RESEND_API_BASE_URL", "https://api.resend.com").rstrip("/")


def get_max_attempts() -> int:
    try:
        return int(os.environ.get("EMAIL_MAX_ATTEMPTS", "5"))
    except ValueError:
        return 5


def get_batch_size() -> int:
    try:
        return int(os.environ.get("EMAIL_BATCH_SIZE", "20"))
    except ValueError:
        return 20


def get_daily_send_limit() -> int:
    try:
        return int(os.environ.get("EMAIL_DAILY_SEND_LIMIT", "80"))
    except ValueError:
        return 80


def get_per_user_daily_limit() -> int:
    try:
        return int(os.environ.get("EMAIL_PER_USER_DAILY_LIMIT", "20"))
    except ValueError:
        return 20


def is_event_enabled_for_user(
    pref: NotificationPreference,
    event_type: str,
) -> bool:
    if not pref.email_enabled:
        return False

    if event_type == "incident_opened":
        return pref.http_down_enabled
    elif event_type == "incident_resolved":
        return pref.http_recovered_enabled
    elif event_type == "heartbeat_missed":
        return pref.heartbeat_missed_enabled
    elif event_type == "heartbeat_recovered":
        return pref.heartbeat_recovered_enabled
    elif event_type.startswith("ssl_expiring"):
        return pref.ssl_expiring_enabled
    elif event_type == "ssl_expired":
        return pref.ssl_expired_enabled
    elif event_type == "ssl_invalid":
        return pref.ssl_invalid_enabled
    elif event_type in ("ssl_renewed", "ssl_recovered"):
        return pref.ssl_recovered_enabled
    elif event_type == "test_email":
        return True

    return False


def build_email_subject(
    event_type: str,
    monitor_name: str,
) -> str:
    if event_type == "incident_opened":
        return f"[Raytech Monitor] DOWN — {monitor_name}"
    elif event_type == "incident_resolved":
        return f"[Raytech Monitor] RECOVERED — {monitor_name}"
    elif event_type == "heartbeat_missed":
        return f"[Raytech Monitor] HEARTBEAT MISSED — {monitor_name}"
    elif event_type == "heartbeat_recovered":
        return f"[Raytech Monitor] HEARTBEAT RECOVERED — {monitor_name}"
    elif event_type.startswith("ssl_expiring"):
        return f"[Raytech Monitor] SSL EXPIRING — {monitor_name}"
    elif event_type == "ssl_expired":
        return f"[Raytech Monitor] SSL EXPIRED — {monitor_name}"
    elif event_type == "ssl_invalid":
        return f"[Raytech Monitor] SSL INVALID — {monitor_name}"
    elif event_type in ("ssl_renewed", "ssl_recovered"):
        return f"[Raytech Monitor] SSL RENEWED — {monitor_name}"
    elif event_type == "test_email":
        return f"[Raytech Monitor] TEST NOTIFICATION — {monitor_name}"
    return f"[Raytech Monitor] ALERT — {monitor_name}"


def render_email_content(
    event_type: str,
    monitor_name: str,
    status_label: str,
    cause_or_message: str,
    occurred_at: datetime,
    monitor_id: int | None = None,
) -> tuple[str, str]:
    """Generates both plain-text and HTML email content with dark/neutral Raytech aesthetic."""
    frontend_url = get_frontend_url()
    monitor_link = (
        f"{frontend_url}/monitors/{monitor_id}"
        if monitor_id is not None
        else frontend_url
    )
    settings_link = f"{frontend_url}/settings"

    occurred_str = occurred_at.strftime("%d %b %Y, %H:%M UTC")

    # Plain-text version
    text_content = f"""Raytech Monitor
========================================
EVENT: {status_label.upper()}
Monitor: {monitor_name}
Status: {status_label}
Occurred: {occurred_str}
Details: {cause_or_message}
----------------------------------------
View Monitor: {monitor_link}

Automated alert from Raytech Monitor.
Manage notification preferences: {settings_link}
"""

    # Escape HTML to prevent injection
    safe_monitor_name = html.escape(monitor_name)
    safe_status_label = html.escape(status_label)
    safe_cause = html.escape(cause_or_message)
    safe_occurred = html.escape(occurred_str)

    # Color accents based on severity
    is_down = "down" in event_type.lower() or "missed" in event_type.lower() or "invalid" in event_type.lower() or "expired" in event_type.lower()
    is_recovered = "resolved" in event_type.lower() or "recovered" in event_type.lower() or "renewed" in event_type.lower()
    badge_color = "#ef4444" if is_down else ("#22c55e" if is_recovered else "#eab308")
    badge_bg = "rgba(239, 68, 68, 0.12)" if is_down else ("rgba(34, 197, 94, 0.12)" if is_recovered else "rgba(234, 179, 8, 0.12)")

    # Minimal, clean dark HTML compatible with Gmail & Outlook
    html_content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>[Raytech Monitor] {safe_status_label} — {safe_monitor_name}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #0c0d0e; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #e6edf3; -webkit-font-smoothing: antialiased;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color: #0c0d0e; padding: 32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width: 540px; background-color: #15171a; border: 1px solid #262930; border-radius: 4px; overflow: hidden; text-align: left;">
          <!-- Header -->
          <tr>
            <td style="padding: 24px 28px; border-bottom: 1px solid #22262b;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <td>
                    <span style="font-size: 11px; font-weight: 700; letter-spacing: 0.12em; color: #8b949e; text-transform: uppercase;">RAYTECH MONITOR</span>
                    <div style="font-size: 19px; font-weight: 600; color: #f0f6fc; margin-top: 4px;">{safe_status_label}</div>
                  </td>
                  <td align="right" valign="middle">
                    <span style="display: inline-block; padding: 4px 10px; font-size: 11px; font-weight: 600; letter-spacing: 0.05em; color: {badge_color}; background-color: {badge_bg}; border: 1px solid {badge_color}; border-radius: 3px; text-transform: uppercase;">
                      {safe_status_label}
                    </span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Incident Details -->
          <tr>
            <td style="padding: 24px 28px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="font-size: 13px; line-height: 1.6;">
                <tr>
                  <td style="padding-bottom: 12px; color: #8b949e; width: 90px;" valign="top">Monitor</td>
                  <td style="padding-bottom: 12px; color: #f0f6fc; font-weight: 600;" valign="top">{safe_monitor_name}</td>
                </tr>
                <tr>
                  <td style="padding-bottom: 12px; color: #8b949e;" valign="top">Occurred</td>
                  <td style="padding-bottom: 12px; color: #c9d1d9;" valign="top">{safe_occurred}</td>
                </tr>
                <tr>
                  <td style="padding-bottom: 16px; color: #8b949e;" valign="top">Cause</td>
                  <td style="padding-bottom: 16px; color: #e6edf3; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; background-color: #0c0d0e; padding: 8px 12px; border-radius: 3px; border: 1px solid #22262b;" valign="top">
                    {safe_cause}
                  </td>
                </tr>
              </table>

              <!-- CTA Button -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top: 16px;">
                <tr>
                  <td>
                    <a href="{monitor_link}" target="_blank" style="display: inline-block; background-color: #21262d; color: #f0f6fc; font-size: 13px; font-weight: 500; text-decoration: none; padding: 10px 18px; border-radius: 3px; border: 1px solid #363b42;">
                      View Monitor &rarr;
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding: 16px 28px; background-color: #0f1113; border-top: 1px solid #22262b; font-size: 11px; color: #8b949e; line-height: 1.5;">
              Automated alert from Raytech Monitor.<br>
              <a href="{settings_link}" target="_blank" style="color: #58a6ff; text-decoration: none;">Manage notification preferences</a>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    return text_content, html_content


def render_payment_confirmation_email(
    customer_name: str | None,
    recipient_email: str,
    transaction_reference: str,
    amount_idr: int,
    paid_at: datetime,
    duration_days: int,
    expiration_date: datetime | None,
    payment_channel: str | None = None,
) -> tuple[str, str]:
    """Generates both plain-text and responsive HTML for payment confirmation receipts."""
    frontend_url = get_frontend_url()
    billing_link = f"{frontend_url}/settings/billing"

    paid_at_str = paid_at.strftime("%d %b %Y, %H:%M UTC")
    expiry_str = expiration_date.strftime("%d %b %Y, %H:%M UTC") if expiration_date else f"{duration_days} days from payment"
    channel_str = payment_channel or "Xendit Sandbox Checkout"
    greeting_name = customer_name.strip() if customer_name and customer_name.strip() else recipient_email

    # Plain-text alternative
    text_content = f"""Raytech Monitor — Payment Confirmation
============================================================
Hello {greeting_name},

Your payment has been successfully processed and confirmed.
Your Raytech Monitor Pro subscription is now active.

PAYMENT SUMMARY
------------------------------------------------------------
Product:             Raytech Monitor Pro
Plan Duration:       {duration_days} Days
Amount:              IDR {amount_idr:,}
Payment Status:      Paid
Payment Date:        {paid_at_str}
Payment Method:      {channel_str}
Order Reference:     {transaction_reference}
Valid Until:         {expiry_str}

View Billing & Plan:
{billing_link}

------------------------------------------------------------
Note: This subscription is a one-time {duration_days}-day access pass and does not automatically renew. No recurring charges will be made to your account.

This email serves as a payment confirmation receipt for your records and is not an official tax invoice.
"""

    # Escape HTML variables to prevent injection
    safe_greeting = html.escape(greeting_name)
    safe_reference = html.escape(transaction_reference)
    safe_paid_at = html.escape(paid_at_str)
    safe_expiry = html.escape(expiry_str)
    safe_channel = html.escape(channel_str)
    safe_amount = f"IDR {amount_idr:,}"
    safe_billing_link = html.escape(billing_link)

    # Minimal, high-contrast, brand-aligned transactional HTML email
    html_content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Your Raytech Monitor Pro payment is confirmed</title>
</head>
<body style="margin: 0; padding: 0; background-color: #0c0d0e; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #e6edf3; -webkit-font-smoothing: antialiased;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color: #0c0d0e; padding: 32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width: 560px; background-color: #15171a; border: 1px solid #262930; border-radius: 6px; overflow: hidden; text-align: left;">
          <!-- Brand Header -->
          <tr>
            <td style="padding: 24px 28px; border-bottom: 1px solid #22262b; background-color: #0f1113;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <td>
                    <span style="font-size: 11px; font-weight: 700; letter-spacing: 0.12em; color: #06b6d4; text-transform: uppercase;">RAYTECH MONITOR BILLING</span>
                    <div style="font-size: 20px; font-weight: 600; color: #f0f6fc; margin-top: 4px;">Payment Confirmed</div>
                  </td>
                  <td align="right" valign="middle">
                    <span style="display: inline-block; padding: 4px 10px; font-size: 11px; font-weight: 600; letter-spacing: 0.05em; color: #22c55e; background-color: rgba(34, 197, 94, 0.12); border: 1px solid #22c55e; border-radius: 3px; text-transform: uppercase;">
                      PAID
                    </span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Message Body -->
          <tr>
            <td style="padding: 28px;">
              <p style="margin: 0 0 16px; font-size: 14px; color: #c9d1d9; line-height: 1.5;">
                Hello <strong style="color: #f0f6fc;">{safe_greeting}</strong>,
              </p>
              <p style="margin: 0 0 20px; font-size: 13px; color: #8b949e; line-height: 1.6;">
                Your payment for <strong style="color: #f0f6fc;">Raytech Monitor Pro</strong> has been successfully processed and verified. Pro features including 1-minute check intervals, 20 HTTP monitors, 20 heartbeats, and SSL alerting are now active.
              </p>

              <!-- Payment Details Summary Table -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="font-size: 13px; line-height: 1.6; border: 1px solid #22262b; border-radius: 4px; background-color: #0c0d0e; margin-bottom: 24px;">
                <tr>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #8b949e; width: 140px;">Product</td>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #f0f6fc; font-weight: 600;">Raytech Monitor Pro</td>
                </tr>
                <tr>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #8b949e;">Amount</td>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #f0f6fc; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-weight: 600;">{safe_amount}</td>
                </tr>
                <tr>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #8b949e;">Order Reference</td>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #06b6d4; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;">{safe_reference}</td>
                </tr>
                <tr>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #8b949e;">Payment Date</td>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #c9d1d9;">{safe_paid_at}</td>
                </tr>
                <tr>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #8b949e;">Payment Channel</td>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #c9d1d9; text-transform: uppercase;">{safe_channel}</td>
                </tr>
                <tr>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #8b949e;">Duration</td>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #c9d1d9;">{duration_days} Days Access</td>
                </tr>
                <tr>
                  <td style="padding: 12px 16px; color: #8b949e;">Valid Until</td>
                  <td style="padding: 12px 16px; color: #22c55e; font-weight: 500;">{safe_expiry}</td>
                </tr>
              </table>

              <!-- Call to Action -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-bottom: 24px;">
                <tr>
                  <td>
                    <a href="{safe_billing_link}" target="_blank" style="display: inline-block; background-color: #06b6d4; color: #081014; font-size: 13px; font-weight: 600; text-decoration: none; padding: 11px 20px; border-radius: 4px;">
                      View Billing &amp; Plan &rarr;
                    </a>
                  </td>
                </tr>
              </table>

              <!-- Notice on renewal -->
              <div style="padding: 12px 16px; background-color: #121519; border: 1px solid #22262b; border-radius: 4px; font-size: 11px; color: #8b949e; line-height: 1.5;">
                <strong style="color: #c9d1d9;">Non-Recurring Access:</strong> Your Pro subscription is a one-time pass and does not automatically debit your card or bank account. You can manually extend your plan at any time from your Billing settings.
              </div>
            </td>
          </tr>

          <!-- Footer Disclaimer -->
          <tr>
            <td style="padding: 16px 28px; background-color: #0f1113; border-top: 1px solid #22262b; font-size: 11px; color: #6e7681; line-height: 1.5;">
              This email serves as a payment confirmation receipt for your records and is not an official tax invoice.<br>
              Raytech Cloud Services &bull; <a href="{safe_billing_link}" target="_blank" style="color: #06b6d4; text-decoration: none;">Account Billing Settings</a>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    return text_content, html_content


WIB = timezone(timedelta(hours=7))


def format_wib_datetime(dt: datetime | None) -> str:
    """Formats a datetime in Asia/Jakarta timezone (WIB, UTC+7)."""
    if not dt:
        return "N/A"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    wib_dt = dt.astimezone(WIB)
    return wib_dt.strftime("%d %b %Y, %H:%M WIB")


def render_subscription_reminder_email(
    customer_name: str | None,
    recipient_email: str,
    expiration_date: datetime,
    threshold_days: int,
) -> tuple[str, str]:
    """Renders HTML and plain-text reminder email for an upcoming Pro subscription expiration."""
    safe_name = html.escape(customer_name.strip()) if customer_name and customer_name.strip() else None
    safe_greeting = safe_name or "Customer"
    safe_email = html.escape(recipient_email)
    safe_expiry = format_wib_datetime(expiration_date)
    frontend_url = get_frontend_url()
    safe_billing_link = html.escape(f"{frontend_url}/settings/billing")

    if threshold_days == 1:
        time_heading = "Final Reminder: Pro Expires Tomorrow"
        time_badge = "24 HOURS REMAINING"
        badge_bg = "#451a1a"
        badge_border = "#f43f5e"
        badge_color = "#fda4af"
        urgency_msg = "Your 30-day Pro subscription expires in 24 hours. Extend today to preserve continuous 1-minute monitoring, SSL checks, and active alerting."
    elif threshold_days == 3:
        time_heading = "Action Required: 3 Days Left on Pro"
        time_badge = "3 DAYS REMAINING"
        badge_bg = "#3b2609"
        badge_border = "#f59e0b"
        badge_color = "#fcd34d"
        urgency_msg = "Your 30-day Pro subscription expires in 3 days. Extend your access at your convenience to maintain high-frequency monitoring."
    else:
        time_heading = "Subscription Expiration Notice"
        time_badge = "7 DAYS REMAINING"
        badge_bg = "#162e3d"
        badge_border = "#06b6d4"
        badge_color = "#67e8f9"
        urgency_msg = "Your 30-day Pro subscription will expire in 7 days. Your plan does not automatically renew, so you may extend anytime from your billing settings."

    text_content = f"""RAYTECH / MONITOR — {time_heading.upper()}

Hello {safe_greeting},

{urgency_msg}

Subscription Details:
- Current Plan: Raytech Monitor Pro
- Expiration Date: {safe_expiry}
- Remaining Time: {time_badge}
- Auto-Renewal: Non-recurring (Manual renewal)

Extend Pro (+30 Days for IDR 49,000):
{safe_billing_link}

Note:
All your monitors, heartbeats, and historical data will remain safely preserved in your account even if your plan expires.

Raytech Cloud Services — https://monitor.raytech.cloud
"""

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{time_heading}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #050608; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #f0f6fc;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color: #050608; padding: 32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" style="max-width: 580px; background-color: #0d0f12; border: 1px solid #22262b; border-radius: 6px; overflow: hidden;" cellspacing="0" cellpadding="0" border="0">
          <!-- Header -->
          <tr>
            <td style="padding: 24px 28px; border-bottom: 1px solid #1a1e23; background-color: #090a0d;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <td>
                    <span style="font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 11px; letter-spacing: 0.15em; color: #8b949e; text-transform: uppercase;">RAYTECH / MONITOR</span>
                    <h1 style="margin: 6px 0 0; font-size: 18px; font-weight: 600; color: #f0f6fc; letter-spacing: -0.02em;">{time_heading}</h1>
                  </td>
                  <td align="right" valign="middle">
                    <span style="display: inline-block; padding: 4px 10px; border-radius: 3px; font-family: ui-monospace, monospace; font-size: 10px; font-weight: 600; letter-spacing: 0.05em; background-color: {badge_bg}; border: 1px solid {badge_border}; color: {badge_color};">
                      {time_badge}
                    </span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Message Body -->
          <tr>
            <td style="padding: 28px;">
              <p style="margin: 0 0 16px; font-size: 14px; color: #c9d1d9; line-height: 1.5;">
                Hello <strong style="color: #f0f6fc;">{safe_greeting}</strong>,
              </p>
              <p style="margin: 0 0 20px; font-size: 13px; color: #8b949e; line-height: 1.6;">
                {urgency_msg}
              </p>

              <!-- Summary Table -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="font-size: 13px; line-height: 1.6; border: 1px solid #22262b; border-radius: 4px; background-color: #0c0d0e; margin-bottom: 24px;">
                <tr>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #8b949e; width: 140px;">Current Plan</td>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #f0f6fc; font-weight: 600;">Raytech Monitor Pro</td>
                </tr>
                <tr>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #8b949e;">Expiration Date</td>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #f0f6fc; font-family: ui-monospace, monospace;">{safe_expiry}</td>
                </tr>
                <tr>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #8b949e;">Renewal Type</td>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #c9d1d9;">Manual Pass (No Auto-Debit)</td>
                </tr>
                <tr>
                  <td style="padding: 12px 16px; color: #8b949e;">Resource Safety</td>
                  <td style="padding: 12px 16px; color: #22c55e;">100% Preserved in Account</td>
                </tr>
              </table>

              <!-- Call to Action -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-bottom: 24px;">
                <tr>
                  <td>
                    <a href="{safe_billing_link}" target="_blank" style="display: inline-block; background-color: #06b6d4; color: #081014; font-size: 13px; font-weight: 600; text-decoration: none; padding: 11px 20px; border-radius: 4px;">
                      Extend Pro (+30 Days) &rarr;
                    </a>
                  </td>
                </tr>
              </table>

              <div style="padding: 12px 16px; background-color: #121519; border: 1px solid #22262b; border-radius: 4px; font-size: 11px; color: #8b949e; line-height: 1.5;">
                <strong style="color: #c9d1d9;">Non-Recurring Pass:</strong> Raytech Monitor Pro does not charge your card automatically. If you choose not to extend, your monitors remain saved and excess resources are simply paused under Free limits.
              </div>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding: 16px 28px; background-color: #0f1113; border-top: 1px solid #22262b; font-size: 11px; color: #6e7681; line-height: 1.5;">
              Raytech Cloud Services &bull; <a href="{safe_billing_link}" target="_blank" style="color: #06b6d4; text-decoration: none;">Account Billing Settings</a>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    return text_content, html_content


def render_subscription_expired_email(
    customer_name: str | None,
    recipient_email: str,
    expired_at: datetime,
) -> tuple[str, str]:
    """Renders HTML and plain-text notification for an expired Pro subscription."""
    safe_name = html.escape(customer_name.strip()) if customer_name and customer_name.strip() else None
    safe_greeting = safe_name or "Customer"
    safe_email = html.escape(recipient_email)
    safe_expired = format_wib_datetime(expired_at)
    frontend_url = get_frontend_url()
    safe_billing_link = html.escape(f"{frontend_url}/settings/billing")

    text_content = f"""RAYTECH / MONITOR — SUBSCRIPTION EXPIRED

Hello {safe_greeting},

Your Raytech Monitor Pro subscription period has ended and your account has transitioned to the Free plan.

Summary:
- Previous Plan: Raytech Monitor Pro
- Current Plan: Free Tier
- Expired On: {safe_expired}
- Status: Free Access Active

What happens next?
- All your monitors, heartbeats, and incident history remain safely saved.
- Resources exceeding Free limits (up to 3 HTTP monitors, 1 Heartbeat, 1 Status Page) are currently restricted from automated checks.
- You can reactivate Pro at any time to immediately unrestrict all monitors.
- No automatic charges have occurred.

Reactivate Pro (+30 Days for IDR 49,000):
{safe_billing_link}

Raytech Cloud Services — https://monitor.raytech.cloud
"""

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Pro Subscription Expired</title>
</head>
<body style="margin: 0; padding: 0; background-color: #050608; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #f0f6fc;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color: #050608; padding: 32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" style="max-width: 580px; background-color: #0d0f12; border: 1px solid #22262b; border-radius: 6px; overflow: hidden;" cellspacing="0" cellpadding="0" border="0">
          <!-- Header -->
          <tr>
            <td style="padding: 24px 28px; border-bottom: 1px solid #1a1e23; background-color: #090a0d;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <td>
                    <span style="font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 11px; letter-spacing: 0.15em; color: #8b949e; text-transform: uppercase;">RAYTECH / MONITOR</span>
                    <h1 style="margin: 6px 0 0; font-size: 18px; font-weight: 600; color: #f0f6fc; letter-spacing: -0.02em;">Pro Subscription Expired</h1>
                  </td>
                  <td align="right" valign="middle">
                    <span style="display: inline-block; padding: 4px 10px; border-radius: 3px; font-family: ui-monospace, monospace; font-size: 10px; font-weight: 600; letter-spacing: 0.05em; background-color: #2b1f14; border: 1px solid #f59e0b; color: #fbbf24;">
                      FREE ACTIVE
                    </span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Message Body -->
          <tr>
            <td style="padding: 28px;">
              <p style="margin: 0 0 16px; font-size: 14px; color: #c9d1d9; line-height: 1.5;">
                Hello <strong style="color: #f0f6fc;">{safe_greeting}</strong>,
              </p>
              <p style="margin: 0 0 20px; font-size: 13px; color: #8b949e; line-height: 1.6;">
                Your 30-day Pro subscription has concluded and your account has transitioned to the Free tier. All your monitors, heartbeats, status pages, and incident logs remain <strong style="color: #f0f6fc;">100% saved</strong>.
              </p>

              <!-- Summary Table -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="font-size: 13px; line-height: 1.6; border: 1px solid #22262b; border-radius: 4px; background-color: #0c0d0e; margin-bottom: 24px;">
                <tr>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #8b949e; width: 140px;">Current Plan</td>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #f0f6fc; font-weight: 600;">Free Tier (Active)</td>
                </tr>
                <tr>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #8b949e;">Expired On</td>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #f0f6fc; font-family: ui-monospace, monospace;">{safe_expired}</td>
                </tr>
                <tr>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #8b949e;">Resource Status</td>
                  <td style="padding: 12px 16px; border-bottom: 1px solid #1a1e23; color: #22c55e;">Preserved (3 HTTP monitors eligible)</td>
                </tr>
                <tr>
                  <td style="padding: 12px 16px; color: #8b949e;">Automatic Charges</td>
                  <td style="padding: 12px 16px; color: #8b949e;">None (No card charged)</td>
                </tr>
              </table>

              <!-- Call to Action -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-bottom: 24px;">
                <tr>
                  <td>
                    <a href="{safe_billing_link}" target="_blank" style="display: inline-block; background-color: #06b6d4; color: #081014; font-size: 13px; font-weight: 600; text-decoration: none; padding: 11px 20px; border-radius: 4px;">
                      Reactivate Pro (+30 Days) &rarr;
                    </a>
                  </td>
                </tr>
              </table>

              <div style="padding: 12px 16px; background-color: #121519; border: 1px solid #22262b; border-radius: 4px; font-size: 11px; color: #8b949e; line-height: 1.5;">
                <strong style="color: #c9d1d9;">Plan Restrictions:</strong> Resources exceeding Free limits (over 3 HTTP monitors, 1 Heartbeat, or 1 Status page) are paused from automated scheduling. You can reactivate Pro at any time to instantly resume full monitoring.
              </div>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding: 16px 28px; background-color: #0f1113; border-top: 1px solid #22262b; font-size: 11px; color: #6e7681; line-height: 1.5;">
              Raytech Cloud Services &bull; <a href="{safe_billing_link}" target="_blank" style="color: #06b6d4; text-decoration: none;">Account Billing Settings</a>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    return text_content, html_content


def enqueue_email_for_notification(
    db: Session,
    notification: Notification,
) -> EmailDelivery | None:
    """Enqueues an email delivery record for a notification, obeying user preferences and idempotency.

    Does NOT block or call Resend directly.
    """
    if not is_email_enabled():
        return None

    # Load user
    user = db.get(User, notification.user_id)
    if user is None or not user.email:
        return None

    # Load preferences
    pref = db.scalar(
        select(NotificationPreference).where(
            NotificationPreference.user_id == user.id
        )
    )
    if pref is None or not pref.email_enabled:
        return None

    from app.services.subscription_service import is_user_email_entitled
    if not is_user_email_entitled(db, user.id):
        return None

    if not is_event_enabled_for_user(pref, notification.event_type):
        return None

    # Check for existing delivery to prevent duplicates
    existing = db.scalar(
        select(EmailDelivery).where(
            EmailDelivery.notification_id == notification.id,
            EmailDelivery.recipient_email == user.email,
        )
    )
    if existing is not None:
        return existing

    # Find monitor name if monitor_id is attached
    monitor_name = "Monitor"
    if notification.monitor_id:
        mon = db.get(Monitor, notification.monitor_id)
        if mon:
            monitor_name = mon.name

    subject = build_email_subject(notification.event_type, monitor_name)

    # Status badge label
    if notification.event_type == "incident_opened":
        status_label = "DOWN"
    elif notification.event_type == "incident_resolved":
        status_label = "RECOVERED"
    elif notification.event_type == "heartbeat_missed":
        status_label = "HEARTBEAT MISSED"
    elif notification.event_type == "heartbeat_recovered":
        status_label = "HEARTBEAT RECOVERED"
    elif notification.event_type.startswith("ssl_expiring"):
        status_label = "SSL EXPIRING"
    elif notification.event_type == "ssl_expired":
        status_label = "SSL EXPIRED"
    elif notification.event_type == "ssl_invalid":
        status_label = "SSL INVALID"
    elif notification.event_type in ("ssl_renewed", "ssl_recovered"):
        status_label = "SSL RENEWED"
    else:
        status_label = "ALERT"

    text_body, html_body = render_email_content(
        event_type=notification.event_type,
        monitor_name=monitor_name,
        status_label=status_label,
        cause_or_message=notification.message,
        occurred_at=notification.created_at or utcnow(),
        monitor_id=notification.monitor_id,
    )

    delivery = EmailDelivery(
        notification_id=notification.id,
        user_id=user.id,
        recipient_email=user.email,
        provider="resend",
        event_type=notification.event_type,
        subject=subject,
        body_text=text_body,
        body_html=html_body,
        status="pending",
        attempt_count=0,
        next_attempt_at=utcnow(),
    )
    db.add(delivery)
    db.flush()

    return delivery


def enqueue_test_email(
    db: Session,
    user: User,
) -> EmailDelivery:
    """Enqueues a test email for the authenticated user."""
    subject = "[Raytech Monitor] TEST NOTIFICATION — System Test"
    text_body, html_body = render_email_content(
        event_type="test_email",
        monitor_name="Raytech Monitor Test",
        status_label="SYSTEM TEST",
        cause_or_message="This is a test notification confirming your email alerting configuration is functional.",
        occurred_at=utcnow(),
        monitor_id=None,
    )

    delivery = EmailDelivery(
        notification_id=None,
        user_id=user.id,
        recipient_email=user.email,
        provider="resend",
        event_type="test_email",
        subject=subject,
        body_text=text_body,
        body_html=html_body,
        status="pending",
        attempt_count=0,
        next_attempt_at=utcnow(),
    )
    db.add(delivery)
    db.flush()
    return delivery


def enqueue_payment_confirmation_email(
    db: Session,
    user: User,
    transaction: Any,
    subscription: Any,
) -> EmailDelivery | None:
    """Enqueues an outbox record for payment confirmation receipt.

    Guarantees exactly-once enqueueing per transaction using idempotency key:
    'payment_confirmed:{transaction.id}'.
    Does not require monitoring email alerts to be enabled (transactional billing communication).
    Flushes changes to the session without committing, ensuring caller controls transaction atomicity.
    """
    if not user.email:
        logger.warning(f"Cannot enqueue payment confirmation: user {user.id} has no email address.")
        return None

    idempotency_key = f"payment_confirmed:{transaction.id}"

    # Check for existing delivery with this idempotency key
    existing = db.scalar(
        select(EmailDelivery).where(EmailDelivery.idempotency_key == idempotency_key)
    )
    if existing is not None:
        logger.info(f"Payment confirmation email already enqueued for tx {transaction.id} ({idempotency_key})")
        return existing

    subject = "Your Raytech Monitor Pro payment is confirmed"
    text_body, html_body = render_payment_confirmation_email(
        customer_name=user.name,
        recipient_email=user.email,
        transaction_reference=transaction.external_id,
        amount_idr=transaction.amount,
        paid_at=transaction.paid_at or utcnow(),
        duration_days=30,
        expiration_date=getattr(subscription, "current_period_end", None),
        payment_channel=transaction.payment_channel or transaction.payment_method,
    )

    delivery = EmailDelivery(
        notification_id=None,
        user_id=user.id,
        recipient_email=user.email,
        provider="resend",
        event_type="payment_confirmed",
        idempotency_key=idempotency_key,
        subject=subject,
        body_text=text_body,
        body_html=html_body,
        status="pending",
        attempt_count=0,
        next_attempt_at=utcnow(),
    )
    db.add(delivery)
    db.flush()
    logger.info(f"Enqueued payment confirmation email for user {user.id}, tx {transaction.id} ({idempotency_key})")
    return delivery


def enqueue_subscription_reminder_email(
    db: Session,
    user: User,
    subscription: Any,
    threshold_days: int,
) -> EmailDelivery | None:
    """Enqueues a subscription expiration reminder outbox record.

    Guarantees exactly-once queueing per subscription expiration period using idempotency key:
    'sub_reminder:{subscription.id}:{subscription.current_period_end_timestamp}:{threshold_days}d'.
    """
    if not user.email:
        logger.warning(f"Cannot enqueue expiration reminder: user {user.id} has no email address.")
        return None

    if not subscription.current_period_end:
        return None

    end_ts = subscription.current_period_end.strftime("%Y%m%d%H%M")
    idempotency_key = f"sub_reminder:{subscription.id}:{end_ts}:{threshold_days}d"

    # Check for existing delivery with this idempotency key
    existing = db.scalar(
        select(EmailDelivery).where(EmailDelivery.idempotency_key == idempotency_key)
    )
    if existing is not None:
        logger.info(f"Expiration reminder ({threshold_days}d) already enqueued for sub {subscription.id} ({idempotency_key})")
        return None

    if threshold_days == 1:
        subject = "[Raytech Monitor] Final reminder: Pro subscription expires tomorrow"
    elif threshold_days == 3:
        subject = "[Raytech Monitor] Action required: 3 days left on your Pro plan"
    else:
        subject = "[Raytech Monitor] Your Pro subscription expires in 7 days"

    text_body, html_body = render_subscription_reminder_email(
        customer_name=user.name,
        recipient_email=user.email,
        expiration_date=subscription.current_period_end,
        threshold_days=threshold_days,
    )

    delivery = EmailDelivery(
        notification_id=None,
        user_id=user.id,
        recipient_email=user.email,
        provider="resend",
        event_type=f"sub_reminder_{threshold_days}d",
        idempotency_key=idempotency_key,
        subject=subject,
        body_text=text_body,
        body_html=html_body,
        status="pending",
        attempt_count=0,
        next_attempt_at=utcnow(),
    )
    db.add(delivery)
    db.flush()
    logger.info(f"Enqueued {threshold_days}d expiration reminder for user {user.id}, sub {subscription.id} ({idempotency_key})")
    return delivery


def enqueue_subscription_expired_email(
    db: Session,
    user: User,
    subscription: Any,
) -> EmailDelivery | None:
    """Enqueues a subscription expired confirmation outbox record.

    Guarantees exactly-once queueing per expired subscription period using idempotency key:
    'sub_expired:{subscription.id}:{subscription.current_period_end_timestamp}'.
    """
    if not user.email:
        logger.warning(f"Cannot enqueue expired confirmation: user {user.id} has no email address.")
        return None

    end_ts = subscription.current_period_end.strftime("%Y%m%d%H%M") if subscription.current_period_end else "none"
    idempotency_key = f"sub_expired:{subscription.id}:{end_ts}"

    existing = db.scalar(
        select(EmailDelivery).where(EmailDelivery.idempotency_key == idempotency_key)
    )
    if existing is not None:
        logger.info(f"Expiration confirmation already enqueued for sub {subscription.id} ({idempotency_key})")
        return None

    subject = "Your Raytech Monitor Pro subscription has expired"
    text_body, html_body = render_subscription_expired_email(
        customer_name=user.name,
        recipient_email=user.email,
        expired_at=subscription.current_period_end or utcnow(),
    )

    delivery = EmailDelivery(
        notification_id=None,
        user_id=user.id,
        recipient_email=user.email,
        provider="resend",
        event_type="sub_expired",
        idempotency_key=idempotency_key,
        subject=subject,
        body_text=text_body,
        body_html=html_body,
        status="pending",
        attempt_count=0,
        next_attempt_at=utcnow(),
    )
    db.add(delivery)
    db.flush()
    logger.info(f"Enqueued Pro expiration confirmation email for user {user.id}, sub {subscription.id} ({idempotency_key})")
    return delivery


def count_recent_sent_emails(
    db: Session,
    user_id: int | None = None,
    since: datetime | None = None,
) -> int:
    """Count sent emails in the last 24h for rate limit checks."""
    if since is None:
        since = utcnow() - timedelta(days=1)

    stmt = select(func.count(EmailDelivery.id)).where(
        EmailDelivery.status == "sent",
        EmailDelivery.created_at >= since,
    )
    if user_id is not None:
        stmt = stmt.where(EmailDelivery.user_id == user_id)

    return db.scalar(stmt) or 0


def get_provider_idempotency_key(delivery: EmailDelivery) -> str:
    """Derives a stable, provider-side idempotency key for Resend API requests.

    Requirements:
    1. One stable key per logical email event, independent of retry attempts.
    2. Retries of the same email (e.g. after ambiguous network timeout) must send the
       exact same idempotency key to prevent duplicate email sends by Resend.
    3. Distinct transactions produce distinct keys.
    4. Complies with Resend header formats (alphanumeric, dashes, underscores).

    Note on Provider Guarantees:
    Provider-side HTTP idempotency retention is typically time-limited (e.g. 24-48 hours
    on Resend) and cannot mathematically guarantee exactly-once delivery indefinitely.
    Database-level unique constraints ('idx_email_deliveries_idempotency_key') and the
    transactional outbox ensure true exactly-once enqueueing within the system.
    """
    if getattr(delivery, "idempotency_key", None):
        slug = (
            delivery.idempotency_key.replace(":", "-")
            .replace("_", "-")
            .replace("/", "-")
            .strip()
        )
        return f"raytech-{slug}"
    return f"raytech-delivery-{delivery.id}"


def send_resend_email_sync(
    delivery: EmailDelivery,
    api_key: str,
    base_url: str,
    from_address: str,
    reply_to: str | None = None,
    target_recipient: str | None = None,
) -> tuple[bool, int, str | None, str | None]:
    """Sends an email via Resend REST API synchronously.

    Returns: (is_success, status_code, provider_message_id, error_message)
    """
    to_email = target_recipient or delivery.recipient_email
    url = f"{base_url}/emails"

    idemp_key = get_provider_idempotency_key(delivery)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Idempotency-Key": idemp_key,
    }

    payload = {
        "from": from_address,
        "to": [to_email],
        "subject": delivery.subject,
        "text": delivery.body_text or "",
        "html": delivery.body_html or "",
    }
    if reply_to:
        payload["reply_to"] = reply_to

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, headers=headers, json=payload)

        status_code = resp.status_code
        if 200 <= status_code < 300:
            data = resp.json()
            message_id = data.get("id")
            return True, status_code, message_id, None
        else:
            try:
                err_data = resp.json()
                err_msg = err_data.get("message") or resp.text
            except Exception:
                err_msg = resp.text[:500]
            return False, status_code, None, err_msg
    except httpx.TimeoutException:
        return False, 408, None, "Request timed out connecting to Resend API"
    except Exception as e:
        return False, 503, None, f"Network/HTTP error: {str(e)[:500]}"


def process_pending_email_deliveries(db: Session | None = None) -> dict:
    """Outbox worker processor for pending and retry email deliveries.

    Safe, bounded, rate-limited, and isolated from monitoring state machines.
    """
    if not is_email_enabled():
        return {"processed": 0, "sent": 0, "failed": 0, "skipped": 0, "reason": "email_disabled"}

    api_key = get_resend_api_key()
    if not api_key:
        logger.warning("Email is enabled but RESEND_API_KEY is not configured.")
        return {"processed": 0, "sent": 0, "failed": 0, "skipped": 0, "reason": "missing_api_key"}

    close_session = False
    if db is None:
        db = SessionLocal()
        close_session = True

    try:
        now = utcnow()

        # 1. Recover stale processing rows (stuck > 5 minutes)
        stale_threshold = now - timedelta(minutes=5)
        stale_rows = db.scalars(
            select(EmailDelivery).where(
                EmailDelivery.status == "processing",
                EmailDelivery.updated_at < stale_threshold,
            )
        ).all()
        for stale in stale_rows:
            stale.status = "retry"
            stale.updated_at = now
        if stale_rows:
            db.commit()

        # 2. Select pending deliveries up to batch size
        batch_size = get_batch_size()
        query = (
            select(EmailDelivery)
            .where(
                EmailDelivery.status.in_(["pending", "retry"]),
                EmailDelivery.next_attempt_at <= now,
            )
            .order_by(EmailDelivery.id.asc())
            .limit(batch_size)
        )

        # Use row-level locking with skip_locked if supported (PostgreSQL)
        try:
            query = query.with_for_update(skip_locked=True)
            deliveries = db.scalars(query).all()
        except Exception:
            # Fallback for SQLite in tests
            db.rollback()
            query = (
                select(EmailDelivery)
                .where(
                    EmailDelivery.status.in_(["pending", "retry"]),
                    EmailDelivery.next_attempt_at <= now,
                )
                .order_by(EmailDelivery.id.asc())
                .limit(batch_size)
            )
            deliveries = db.scalars(query).all()

        if not deliveries:
            return {"processed": 0, "sent": 0, "failed": 0, "skipped": 0}

        # Mark as processing
        for d in deliveries:
            d.status = "processing"
            d.updated_at = now
        db.commit()

        # Rate limits
        daily_limit = get_daily_send_limit()
        per_user_limit = get_per_user_daily_limit()
        max_attempts = get_max_attempts()
        test_mode = is_test_mode()
        test_recipient = get_test_recipient()
        from_address = get_email_from()
        reply_to = get_email_reply_to()
        base_url = get_resend_api_base_url()

        sent_count = 0
        failed_count = 0
        skipped_count = 0

        for delivery in deliveries:
            # Refresh row state
            db.refresh(delivery)

            # Dispatch-time validity check for subscription lifecycle reminders:
            # If the user extended/renewed, expired, or downgraded to Free since queueing,
            # do not send an obsolete or misleading reminder.
            if delivery.event_type.startswith("sub_reminder_"):
                sub = db.scalar(
                    select(UserSubscription).where(UserSubscription.user_id == delivery.user_id)
                )
                is_stale = False
                stale_reason = ""
                if sub is None:
                    is_stale = True
                    stale_reason = "Subscription record no longer exists."
                elif sub.plan != "pro" or sub.status != "active":
                    is_stale = True
                    stale_reason = f"User is no longer an active Pro subscriber (plan={sub.plan}, status={sub.status})."
                elif sub.current_period_end is None:
                    is_stale = True
                    stale_reason = "Subscription current_period_end is None."
                else:
                    end_tz = (
                        sub.current_period_end
                        if sub.current_period_end.tzinfo
                        else sub.current_period_end.replace(tzinfo=timezone.utc)
                    )
                    now_utc = utcnow()
                    if end_tz <= now_utc:
                        is_stale = True
                        stale_reason = "Subscription has already expired."
                    else:
                        # Idempotency key format: sub_reminder:{sub_id}:{end_ts}:{threshold}d
                        expected_end_ts = sub.current_period_end.strftime("%Y%m%d%H%M")
                        if delivery.idempotency_key and f":{expected_end_ts}:" not in delivery.idempotency_key:
                            is_stale = True
                            stale_reason = "Subscription was extended or renewed after reminder was queued."

                if is_stale:
                    delivery.status = "skipped"
                    delivery.last_error_code = "STALE_REMINDER"
                    delivery.last_error_message = f"Skipped at dispatch: {stale_reason}"
                    delivery.updated_at = utcnow()
                    db.commit()
                    skipped_count += 1
                    logger.info(
                        "Skipped dispatch of stale reminder (id=%s, user=%s): %s",
                        delivery.id,
                        delivery.user_id,
                        stale_reason,
                    )
                    continue

            # Check global limit
            recent_global_sent = count_recent_sent_emails(db)
            if recent_global_sent >= daily_limit:
                delivery.status = "skipped"
                delivery.last_error_code = "QUOTA_EXCEEDED"
                delivery.last_error_message = f"Global daily sending limit reached ({daily_limit})"
                delivery.updated_at = utcnow()
                db.commit()
                skipped_count += 1
                continue

            # Check per-user limit
            recent_user_sent = count_recent_sent_emails(db, user_id=delivery.user_id)
            if recent_user_sent >= per_user_limit:
                delivery.status = "skipped"
                delivery.last_error_code = "USER_QUOTA_EXCEEDED"
                delivery.last_error_message = f"User daily sending limit reached ({per_user_limit})"
                delivery.updated_at = utcnow()
                db.commit()
                skipped_count += 1
                continue

            # Determine actual recipient
            actual_recipient = delivery.recipient_email
            if test_mode:
                if not test_recipient:
                    delivery.status = "failed"
                    delivery.last_error_code = "CONFIG_ERROR"
                    delivery.last_error_message = "EMAIL_TEST_MODE is enabled but EMAIL_TEST_RECIPIENT is not configured"
                    delivery.updated_at = utcnow()
                    db.commit()
                    failed_count += 1
                    continue
                actual_recipient = test_recipient
                logger.info(
                    "Test mode active: routing email for %s to test recipient %s",
                    delivery.recipient_email,
                    test_recipient,
                )

            # Perform send with appropriate sender identity
            sender_address = (
                get_billing_email_from()
                if delivery.event_type == "payment_confirmed"
                or delivery.event_type.startswith("sub_")
                else from_address
            )

            is_success, status_code, message_id, err_msg = send_resend_email_sync(
                delivery=delivery,
                api_key=api_key,
                base_url=base_url,
                from_address=sender_address,
                reply_to=reply_to,
                target_recipient=actual_recipient,
            )

            if is_success:
                delivery.status = "sent"
                delivery.sent_at = utcnow()
                delivery.provider_message_id = message_id
                delivery.last_error_code = None
                delivery.last_error_message = None
                delivery.updated_at = utcnow()
                sent_count += 1
            else:
                delivery.attempt_count += 1
                delivery.last_error_code = str(status_code)
                delivery.last_error_message = (err_msg or "")[:1000]

                # Check if retryable (429 or 5xx or network errors)
                is_retryable = status_code in (429, 408, 500, 502, 503, 504)
                if is_retryable and delivery.attempt_count < max_attempts:
                    delivery.status = "retry"
                    # Exponential backoff with jitter: 2^attempt * 10s + jitter
                    jitter = random.uniform(1.0, 5.0)
                    backoff_seconds = (2 ** delivery.attempt_count) * 10 + jitter
                    delivery.next_attempt_at = utcnow() + timedelta(seconds=backoff_seconds)
                else:
                    delivery.status = "failed"
                    failed_count += 1

                delivery.updated_at = utcnow()

            db.commit()

        return {
            "processed": len(deliveries),
            "sent": sent_count,
            "failed": failed_count,
            "skipped": skipped_count,
        }

    finally:
        if close_session:
            db.close()
