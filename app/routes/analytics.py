from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.services.product_event_service import record_daily_client_event
from app.services.rate_limiter import api_rate_limiter


router = APIRouter(prefix="/analytics", tags=["analytics"])


class ClientEventRequest(BaseModel):
    event_name: str = Field(min_length=1, max_length=64)
    properties: dict[str, Any] = Field(default_factory=dict)


@router.post("/events", status_code=status.HTTP_202_ACCEPTED)
def create_client_event(
    payload: ClientEventRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    api_rate_limiter.check_or_raise(
        f"product_event:{current_user.id}",
        max_requests=120,
        window_seconds=60.0,
        error_detail="Too many analytics events.",
    )
    try:
        event = record_daily_client_event(
            db,
            event_name=payload.event_name.strip(),
            user_id=current_user.id,
            properties=payload.properties,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    db.commit()
    return {"accepted": True, "event_id": event.id}
