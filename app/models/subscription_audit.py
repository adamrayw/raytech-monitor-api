from datetime import datetime, timezone
from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SubscriptionAuditLog(Base):
    __tablename__ = "subscription_audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("user_subscriptions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    previous_plan: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    new_plan: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    event_type: Mapped[str] = mapped_column(
        String(64),
        index=True,
        nullable=False,
    )

    reason: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    payment_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_transactions.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        index=True,
        nullable=False,
    )

    user = relationship("User")
    subscription = relationship("UserSubscription")
    payment_transaction = relationship("PaymentTransaction")
