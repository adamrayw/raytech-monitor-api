import argparse

from sqlalchemy import select

from app.database import SessionLocal
from app.models.user import User


ALLOWED_COHORTS = {"internal", "beta", "public"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Set the soft-launch cohort for an existing Raytech Monitor user."
    )
    parser.add_argument("--email", required=True, help="Exact existing account email")
    parser.add_argument("--cohort", required=True, choices=sorted(ALLOWED_COHORTS))
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
        if user.launch_cohort == args.cohort:
            print(f"{email} is already in cohort {args.cohort}; no change made")
            return 0
        user.launch_cohort = args.cohort
        db.commit()
        print(f"Set launch cohort for {email} to {args.cohort}")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
