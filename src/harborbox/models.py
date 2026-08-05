from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
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
    # Whether this sandbox may reach the network at all. Off unless the caller
    # asks: widget code is handed its data as files and needs none, so the
    # default keeps "internet blocked" true for everything that does not
    # explicitly opt out of it.
    egress: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
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


class SandboxTemplate(Base):
    """A derived, content-hashed template built on top of a static base image.

    Static templates are not rows here: they are configuration, and creating
    rows for them would give the same template two sources of truth.
    """

    __tablename__ = "sandbox_templates"
    __table_args__ = (Index("ix_sandbox_templates_gc", "status", "last_used_at"),)

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    base: Mapped[str] = mapped_column(String(64))
    spec_hash: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    image: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), index=True, default="building")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    memory_mb: Mapped[int] = mapped_column(Integer)
    cpu: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
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


class WarmPoolState(Base):
    __tablename__ = "warm_pool_states"

    pool_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    max_idle: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idle_ttl_seconds: Mapped[float] = mapped_column(Float, default=86_400.0)
    primary_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    primary_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    destroy_state: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    destroy_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    destroy_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WarmPoolIdleSandbox(Base):
    __tablename__ = "warm_pool_idle_sandboxes"
    __table_args__ = (
        Index("ix_warm_pool_idle_fifo", "pool_name", "created_at", "id"),
        Index("ix_warm_pool_idle_expiry", "pool_name", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pool_name: Mapped[str] = mapped_column(
        ForeignKey("warm_pool_states.pool_name", ondelete="CASCADE")
    )
    sandbox_id: Mapped[str] = mapped_column(String(255), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
