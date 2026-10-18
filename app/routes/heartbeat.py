import time
from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.heartbeat_service import process_heartbeat
from app.services.rate_limiter import api_rate_limiter, get_trusted_client_ip

router = APIRouter(prefix="/heartbeat", tags=["heartbeat"])


@router.post("/{token}", status_code=status.HTTP_200_OK)
def receive_heartbeat(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Public heartbeat ingestion endpoint.
    Called by cron jobs, backup tasks, and external workers upon completion.
    No OAuth authentication required; authentication is via the 256-bit secret token.
    """
    client_ip = get_trusted_client_ip(request)
    api_rate_limiter.check_or_raise(
        f"heartbeat:{client_ip}",
        max_requests=120,
        window_seconds=60.0,
        error_detail="Too many heartbeat requests. Please slow down.",
    )

    # Basic length check: 256-bit urlsafe token is 43-44 chars
    if not token or len(token) < 20 or len(token) > 128:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid heartbeat token",
        )

    success, message, monitor = process_heartbeat(db, token, client_ip)
    if not success or monitor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid heartbeat token",
        )

    return {
        "status": "ok",
        "message": message,
    }
