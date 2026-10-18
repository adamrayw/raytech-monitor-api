"""CLI helper to switch a user's subscription plan during development and testing.

Usage:
    python -m scripts.switch_user_plan --email user@example.com --plan pro
    python -m scripts.switch_user_plan --email user@example.com --plan free
"""

import argparse
import sys

from sqlalchemy import select

from app.database import SessionLocal
from app.models.user import User
from app.services.subscription_service import PLAN_CONFIG, switch_user_plan_dev


def main():
    parser = argparse.ArgumentParser(description="Switch user subscription plan for development")
    parser.add_argument("--email", required=True, help="User email address")
    parser.add_argument("--plan", required=True, choices=list(PLAN_CONFIG.keys()), help="Target plan ('free' or 'pro')")
    parser.add_argument("--reason", default="cli_admin_tool", help="Reason for plan change")

    args = parser.parse_args()

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == args.email))
        if not user:
            print(f"Error: User with email '{args.email}' not found.", file=sys.stderr)
            sys.exit(1)

        sub = switch_user_plan_dev(db, user_id=user.id, new_plan=args.plan, reason=args.reason)
        print(f"Success: User '{args.email}' (ID: {user.id}) switched to plan '{sub.plan}'.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
