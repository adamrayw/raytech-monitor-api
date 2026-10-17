import time
from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.heartbeat_service import process_heartbeat

router = APIRouter(prefix="/heartbeat", tags=["heartbeat"])

# In-memory rate limiting: max 120 pings per minute per IP to prevent brute-forcing
_rate_limits: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_WINDOW = 60.0
MAX_REQUESTS_PER_WINDOW = 120


def check_rate_limit(client_ip: str):
    now = time.time()
    timestamps = _rate_limits[client_ip]
    # Prune old timestamps
    _rate_limits[client_ip] = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_limits[client_ip]) >= MAX_REQUESTS_PER_WINDOW:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many heartbeat requests. Please slow down.",
        )
    _rate_limits[client_ip].append(now)


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
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(client_ip)

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
