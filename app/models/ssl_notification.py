from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class SSLNotificationLog(Base):
    """
    Tracks sent milestone warnings and status alerts for certificate lifecycles.
    Ensures idempotent milestone alerting without duplicate notifications per worker cycle.
    """
    __tablename__ = "ssl_notification_logs"

    __table_args__ = (
        UniqueConstraint(
            "monitor_id",
            "cert_identifier",
            "milestone",
            name="uq_ssl_notification_dedup",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    cert_identifier: Mapped[str] = mapped_column(
        String(128),
        index=True,
        nullable=False,
    )

    milestone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    notified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )

    notification_id: Mapped[int | None] = mapped_column(
        ForeignKey("notifications.id", ondelete="SET NULL"),
        nullable=True,
    )

    monitor = relationship(
        "Monitor",
        back_populates="ssl_notifications",
    )
