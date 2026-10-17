from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
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


class StatusPage(Base):
    __tablename__ = "status_pages"

    __table_args__ = (
        Index("idx_status_pages_user_id", "user_id"),
        Index("idx_status_pages_slug", "slug"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    slug: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    accent_color: Mapped[str] = mapped_column(
        String(32),
        default="#00f0ff",
        nullable=False,
    )

    logo_url: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
    )

    is_published: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    show_incident_history: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
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

    user = relationship("User")
    page_monitors = relationship(
        "StatusPageMonitor",
        back_populates="status_page",
        cascade="all, delete-orphan",
        order_by="StatusPageMonitor.display_order",
    )


class StatusPageMonitor(Base):
    __tablename__ = "status_page_monitors"

    __table_args__ = (
        UniqueConstraint(
            "status_page_id",
            "monitor_id",
            name="uq_status_page_monitors_page_monitor",
        ),
        Index("idx_status_page_monitors_order", "status_page_id", "display_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    status_page_id: Mapped[int] = mapped_column(
        ForeignKey("status_pages.id", ondelete="CASCADE"),
        nullable=False,
    )

    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"),
        nullable=False,
    )

    display_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    display_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )

    status_page = relationship("StatusPage", back_populates="page_monitors")
    monitor = relationship("Monitor")
