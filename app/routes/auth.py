import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.dependencies.auth import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.oauth import oauth


router = APIRouter(
    prefix="/auth",
    tags=["auth"],
)

FRONTEND_URL = os.environ.get(
    "FRONTEND_URL",
    "https://monitor.raytech.cloud",
)

GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    "https://api.monitor.raytech.cloud/auth/google/callback",
)

from app.services.rate_limiter import api_rate_limiter, get_trusted_client_ip


@router.get("/google/login")
async def google_login(request: Request):
    client_ip = get_trusted_client_ip(request)
    api_rate_limiter.check_or_raise(
        f"auth_login:{client_ip}",
        max_requests=30,
        window_seconds=60.0,
        error_detail="Too many login attempts. Please wait a minute before retrying.",
    )
    return await oauth.google.authorize_redirect(
        request,
        GOOGLE_REDIRECT_URI,
    )


@router.get(
    "/google/callback",
    name="google_callback",
)
async def google_callback(
    request: Request,
    db: Session = Depends(get_db),
):
    token = await oauth.google.authorize_access_token(request)

    userinfo = token.get("userinfo")

    if not userinfo:
        userinfo = await oauth.google.userinfo(token=token)

    google_sub = userinfo.get("sub")
    email = userinfo.get("email")

    if not google_sub or not email:
        raise HTTPException(
            status_code=400,
            detail="Google account information is incomplete",
        )

    user = db.scalar(
        select(User).where(
            User.google_sub == google_sub
        )
    )

    if user is None:
        user = db.scalar(
            select(User).where(
                User.email == email
            )
        )

    if user is None:
        user = User(
            email=email,
            name=userinfo.get("name"),
            picture=userinfo.get("picture"),
            google_sub=google_sub,
            is_active=True,
        )

        db.add(user)

    else:
        user.google_sub = google_sub
        user.name = userinfo.get("name") or user.name
        user.picture = userinfo.get("picture") or user.picture

    db.commit()
    db.refresh(user)

    # Session fixation defense: regenerate/clear prior anonymous session state before setting user id
    request.session.clear()
    request.session["user_id"] = user.id

    return RedirectResponse(
        url=f"{FRONTEND_URL}/app",
        status_code=302,
    )

@router.get("/me")
def me(
    user: User = Depends(get_current_user),
):
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
    }

@router.post("/logout")
async def logout(request: Request):
    request.session.clear()

    return {
        "ok": True,
    }
