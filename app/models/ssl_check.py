from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class SSLCheck(Base):
    __tablename__ = "ssl_checks"

    __table_args__ = (
        Index("ix_ssl_checks_monitor_id_checked_at", "monitor_id", "checked_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        index=True,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    issuer: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    subject: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    valid_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    fingerprint_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    serial_number: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    error_type: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    monitor = relationship(
        "Monitor",
        back_populates="ssl_checks",
    )
