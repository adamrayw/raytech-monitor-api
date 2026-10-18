from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)

    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(32),
        default="open",
        index=True,
        nullable=False,
    )

    cause: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    start_http_status: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    last_http_status: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        index=True,
        nullable=False,
    )

    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    duration_seconds: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    resolution_reason: Mapped[str | None] = mapped_column(
        String(64),
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

    monitor = relationship(
        "Monitor",
        back_populates="incidents",
    )
