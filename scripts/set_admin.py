import argparse

from sqlalchemy import select

from app.database import SessionLocal
from app.models.user import User


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Grant or revoke Raytech Monitor administrator access for an existing user."
    )
    parser.add_argument("--email", required=True, help="Exact existing account email")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--grant", action="store_true", help="Grant administrator access")
    action.add_argument("--revoke", action="store_true", help="Revoke administrator access")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    email = args.email.strip().lower()
    if not email:
        raise SystemExit("Email must not be empty")

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == email).with_for_update())
        if user is None:
            raise SystemExit(f"No existing user found for {email}")

        requested_value = bool(args.grant)
        if user.is_admin == requested_value:
            state = "already an administrator" if requested_value else "already not an administrator"
            print(f"{email} is {state}; no change made")
            return 0

        user.is_admin = requested_value
        db.commit()
        action = "Granted" if requested_value else "Revoked"
        print(f"{action} administrator access for {email}")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
