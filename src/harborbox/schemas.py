from __future__ import annotations

# Pydantic resolves field annotations at class-construction time, so `datetime`
# must stay a real import here, not TYPE_CHECKING-only.
from datetime import datetime  # noqa: TC003
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SandboxStatus = Literal[
    "created",
    "starting",
    # Starting up, and already idle, for nobody. Neither counts against the CPU
    # budget; both reserve their memory. See _replenish_pool in scheduler.py.
    "pooling",
    "pooled",
    "running",
    "paused_memory",
    "paused_cold",
    "killed",
    "failed",
]
ExecutionStatus = Literal[
    "queued",
    "admitted",
    "starting",
    "running",
    "succeeded",
    "failed",
    "cancelled",
]


TemplateStatus = Literal["building", "ready", "failed"]


class SandboxCreate(BaseModel):
    # Free-form because a template may be a statically configured name or a
    # derived, content-hashed one. Resolution happens against the registry.
    template: str = Field(min_length=1, max_length=128)
    memory_mb: int | None = Field(default=None, ge=128)
    cpu: float | None = Field(default=None, gt=0)
    idle_timeout_seconds: int | None = Field(default=None, ge=0)
    metadata: dict[str, str] = Field(default_factory=dict)
    # Opt-in. A sandbox with egress can reach whatever the egress network can,
    # so it is only appropriate for code that must talk to something - a
    # customer database, a third-party API - and never for code that was handed
    # its data as files.
    egress: bool = False


class SandboxResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: SandboxStatus
    memory_mb: int
    cpu: float
    pids_limit: int
    idle_timeout_seconds: int
    metadata: dict[str, str] = Field(validation_alias="metadata_")
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime


class SandboxUpdate(BaseModel):
    idle_timeout_seconds: int | None = Field(default=None, ge=0)


class TemplateCreate(BaseModel):
    base: str = Field(min_length=1, max_length=64)
    apt: list[str] = Field(default_factory=list, max_length=200)
    npm: list[str] = Field(default_factory=list, max_length=200)
    env: dict[str, str] = Field(default_factory=dict)
    memory_mb: int | None = Field(default=None, ge=128)
    cpu: float | None = Field(default=None, gt=0)


class TemplateResponse(BaseModel):
    name: str
    base: str
    image: str
    spec_hash: str | None = None
    status: TemplateStatus
    version: str
    memory_mb: int
    cpu: float
    warm_pool: int = 0
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TemplateListResponse(BaseModel):
    templates: list[TemplateResponse]


class PauseRequest(BaseModel):
    memory: bool = True


class CommandCreate(BaseModel):
    command: str
    timeout_seconds: int | None = Field(default=None, ge=1)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    wait: bool = False
    wait_timeout_seconds: int | None = Field(default=None, ge=1)


class ProcessCreate(BaseModel):
    executable: str = Field(min_length=1, max_length=1024)
    args: list[str] = Field(default_factory=list, max_length=256)
    stdin: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    wait: bool = False
    wait_timeout_seconds: int | None = Field(default=None, ge=1)


class LogOutput(BaseModel):
    stdout: list[str] = Field(default_factory=list)
    stderr: list[str] = Field(default_factory=list)
    truncated: bool = False


class ExecutionResult(BaseModel):
    text: str | None = None
    json_value: Any | None = Field(
        default=None,
        validation_alias="json",
        serialization_alias="json",
    )
    html: str | None = None
    png: str | None = None
    jpeg: str | None = None
    svg: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ExecutionError(BaseModel):
    name: str
    value: str
    traceback: list[str] = Field(default_factory=list)


class ExecutionResponse(BaseModel):
    id: str
    sandbox_id: str
    # "code" is retained for reading only: `POST /v1/sandboxes/{id}/executions`
    # is gone and nothing produces new code executions, but rows written before
    # its removal still carry the value and must not 500 on read.
    kind: Literal["code", "command", "process"]
    status: ExecutionStatus
    queue_position: int | None = None
    waiting_for: Literal["memory", "cpu", "sandbox", "worker"] | None = None
    logs: LogOutput | None = None
    results: list[ExecutionResult] = Field(default_factory=list)
    error: ExecutionError | None = None
    exit_code: int | None = None
    created_at: datetime
    admitted_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    queued_ms: int | None = None
    startup_ms: int | None = None
    execution_ms: int | None = None


class FileWriteRequest(BaseModel):
    path: str
    content: str
    encoding: Literal["utf-8", "base64"] = "utf-8"


class FileReadResponse(BaseModel):
    path: str
    content: str
    encoding: Literal["utf-8", "base64"]


class FileUploadResponse(BaseModel):
    path: str
    size: int


class FileEntry(BaseModel):
    name: str
    path: str
    type: Literal["file", "directory"]
    size: int | None = None


class FileListResponse(BaseModel):
    path: str
    entries: list[FileEntry]


class HealthResponse(BaseModel):
    status: Literal["ok"]


class CapacityResponse(BaseModel):
    total_memory_mb: int
    reserve_memory_mb: int
    configured_sandbox_budget_mb: int | None
    sandbox_budget_mb: int
    reserved_memory_mb: int
    warm_pool_reserved_memory_mb: int
    warm_pool_reserved_cpu: float
    warm_pool_target_sandboxes: int
    available_reservation_mb: int
    host_available_memory_mb: int
    running_sandboxes: int
    running_executions: int
    queued_executions: int


class RuntimeCommandRequest(BaseModel):
    command: str
    timeout_seconds: int
    max_output_bytes: int
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


class RuntimeProcessRequest(BaseModel):
    executable: str
    args: list[str] = Field(default_factory=list)
    stdin: str | None = None
    timeout_seconds: int
    max_output_bytes: int
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


class RuntimeExecutionResult(BaseModel):
    logs: LogOutput
    results: list[ExecutionResult] = Field(default_factory=list)
    error: ExecutionError | None = None
    exit_code: int | None = None

    @model_validator(mode="after")
    def error_and_exit_are_consistent(self) -> RuntimeExecutionResult:
        if self.error is not None and self.exit_code == 0:
            message = "an errored execution cannot have exit_code=0"
            raise ValueError(message)
        return self
