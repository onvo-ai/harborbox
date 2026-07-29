from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from harborbox.db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class Sandbox(Base):
    __tablename__ = "sandboxes"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="created")
    container_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    container_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_token: Mapped[str] = mapped_column(String(128))
    memory_mb: Mapped[int] = mapped_column(Integer)
    cpu: Mapped[float] = mapped_column(Float)
    pids_limit: Mapped[int] = mapped_column(Integer)
    idle_timeout_seconds: Mapped[int] = mapped_column(Integer)
    metadata_: Mapped[dict[str, str]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    executions: Mapped[list[Execution]] = relationship(
        back_populates="sandbox", cascade="all, delete-orphan"
    )


class Execution(Base):
    __tablename__ = "executions"
    __table_args__ = (
        Index("ix_executions_queue", "status", "created_at"),
        Index("ix_executions_sandbox_active", "sandbox_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    sandbox_id: Mapped[str] = mapped_column(ForeignKey("sandboxes.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16), default="code")
    status: Mapped[str] = mapped_column(String(32), index=True, default="queued")
    code: Mapped[str | None] = mapped_column(Text, nullable=True)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    cwd: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    admitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sandbox: Mapped[Sandbox] = relationship(back_populates="executions")

