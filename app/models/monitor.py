from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class Monitor(Base):
    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    type: Mapped[str] = mapped_column(
        String(32),
        default="http",
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    url: Mapped[str] = mapped_column(
        String(2048),
        nullable=False,
    )

    interval_seconds: Mapped[int] = mapped_column(
        Integer,
        default=300,
        nullable=False,
    )

    timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        default=10,
        nullable=False,
    )

    expected_status_min: Mapped[int] = mapped_column(
        Integer,
        default=200,
        nullable=False,
    )

    expected_status_max: Mapped[int] = mapped_column(
        Integer,
        default=299,
        nullable=False,
    )

    degraded_threshold_ms: Mapped[int] = mapped_column(
        Integer,
        default=1500,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(32),
        default="unknown",
        index=True,
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        index=True,
        nullable=False,
    )

    consecutive_failures: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    consecutive_successes: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    last_response_ms: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    last_http_status: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    next_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=True,
    )

    ssl_monitoring_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        index=True,
        nullable=False,
    )

    ssl_warning_days: Mapped[int] = mapped_column(
        Integer,
        default=14,
        nullable=False,
    )

    ssl_status: Mapped[str] = mapped_column(
        String(32),
        default="unknown",
        index=True,
        nullable=False,
    )

    ssl_next_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=True,
    )

    ssl_last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    ssl_last_successful_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    ssl_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    ssl_issuer: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    ssl_subject: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    ssl_serial_number: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    ssl_fingerprint_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    ssl_valid_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    ssl_error_type: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    ssl_error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
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
        back_populates="monitors",
    )

    checks = relationship(
        "MonitorCheck",
        back_populates="monitor",
        cascade="all, delete-orphan",
    )

    incidents = relationship(
        "Incident",
        back_populates="monitor",
        cascade="all, delete-orphan",
    )

    ssl_checks = relationship(
        "SSLCheck",
        back_populates="monitor",
        cascade="all, delete-orphan",
    )

    ssl_notifications = relationship(
        "SSLNotificationLog",
        back_populates="monitor",
        cascade="all, delete-orphan",
    )

