import os
import secrets

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User


DEV_TOKEN = os.environ.get("MONITOR_DEV_TOKEN", "")
DEV_USER_EMAIL = os.environ.get("MONITOR_DEV_USER_EMAIL", "")


def get_current_user(
    request: Request,
    x_dev_token: str | None = Header(
        default=None,
        alias="X-Dev-Token",
    ),
    db: Session = Depends(get_db),
) -> User:
    user_id = request.session.get("user_id")

    if user_id is not None:
        user = db.get(User, user_id)

        if user is not None and user.is_active:
            return user

    if (
        DEV_TOKEN
        and DEV_USER_EMAIL
        and x_dev_token
        and secrets.compare_digest(
            x_dev_token,
            DEV_TOKEN,
        )
    ):
        user = db.scalar(
            select(User).where(
                User.email == DEV_USER_EMAIL,
                User.is_active.is_(True),
            )
        )

        if user is not None:
            return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )
