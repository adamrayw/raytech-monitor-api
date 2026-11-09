from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.models.user_feedback import UserFeedback
from app.services.product_event_service import record_product_event, sanitize_page_path
from app.services.rate_limiter import api_rate_limiter


router = APIRouter(prefix="/feedback", tags=["feedback"])
FeedbackCategory = Literal["bug", "feature_request", "user_experience", "payment", "other"]


class FeedbackRequest(BaseModel):
    category: FeedbackCategory
    message: str = Field(min_length=10, max_length=2000)
    rating: int | None = Field(default=None, ge=1, le=5)
    page_path: str = Field(default="/", max_length=255)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 10:
            raise ValueError("Feedback message must contain at least 10 characters")
        return cleaned


@router.post("", status_code=status.HTTP_201_CREATED)
def submit_feedback(
    payload: FeedbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    api_rate_limiter.check_or_raise(
        f"feedback:{current_user.id}",
        max_requests=5,
        window_seconds=3600.0,
        error_detail="Too many feedback submissions. Please try again later.",
    )
    feedback = UserFeedback(
        user_id=current_user.id,
        category=payload.category,
        message=payload.message,
        rating=payload.rating,
        page_path=sanitize_page_path(payload.page_path),
        status="open",
    )
    db.add(feedback)
    db.flush()
    record_product_event(
        db,
        event_name="feedback_submitted",
        user_id=current_user.id,
        source="api",
        properties={
            "feedback_id": feedback.id,
            "category": feedback.category,
            "rating": feedback.rating,
        },
        idempotency_key=f"feedback_submitted:{feedback.id}",
    )
    db.commit()
    return {
        "id": feedback.id,
        "status": feedback.status,
        "message": "Feedback received",
    }
