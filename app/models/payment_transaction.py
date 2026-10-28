from datetime import datetime, timezone
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("user_subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )

    provider: Mapped[str] = mapped_column(
        String(32),
        default="xendit",
        index=True,
        nullable=False,
    )

    provider_mode: Mapped[str] = mapped_column(
        String(32),
        default="sandbox",
        index=True,
        nullable=False,
    )

    provider_payment_id: Mapped[str | None] = mapped_column(
        String(128),
        index=True,
        nullable=True,
    )

    provider_session_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    external_id: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        index=True,
        nullable=False,
    )

    plan: Mapped[str] = mapped_column(
        String(32),
        default="pro",
        nullable=False,
    )

    amount: Mapped[int] = mapped_column(
        Integer,
        default=49000,
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(8),
        default="IDR",
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(32),
        default="PENDING",
        index=True,
        nullable=False,
    )

    payment_method: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    payment_channel: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    checkout_url: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
    )

    qr_string: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        index=True,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )

    user = relationship(
        "User",
        back_populates="payment_transactions",
    )

    subscription = relationship(
        "UserSubscription",
        foreign_keys=[subscription_id],
    )
