from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class EmailDelivery(Base):
    __tablename__ = "email_deliveries"

    __table_args__ = (
        UniqueConstraint(
            "notification_id",
            "recipient_email",
            name="uq_email_deliveries_notification_recipient",
        ),
        Index("idx_email_deliveries_outbox", "status", "next_attempt_at"),
        Index("idx_email_deliveries_user_history", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    notification_id: Mapped[int | None] = mapped_column(
        ForeignKey("notifications.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    recipient_email: Mapped[str] = mapped_column(
        String(255),
        index=True,
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(
        String(32),
        default="resend",
        nullable=False,
    )

    event_type: Mapped[str] = mapped_column(
        String(64),
        index=True,
        nullable=False,
    )

    idempotency_key: Mapped[str | None] = mapped_column(
        String(128),
        unique=True,
        index=True,
        nullable=True,
    )

    subject: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    body_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    body_html: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        index=True,
        nullable=False,
    )  # pending, processing, sent, retry, failed, skipped

    attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )

    provider_message_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    last_error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    last_error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )

    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )

    user = relationship("User")
    notification = relationship("Notification")
