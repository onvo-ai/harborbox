from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SandboxStatus = Literal[
    "created",
    "starting",
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


class SandboxCreate(BaseModel):
    memory_mb: int | None = Field(default=None, ge=128)
    cpu: float | None = Field(default=None, gt=0)
    idle_timeout_seconds: int | None = Field(default=None, ge=0)
    metadata: dict[str, str] = Field(default_factory=dict)


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


class PauseRequest(BaseModel):
    memory: bool = True


class CodeExecutionCreate(BaseModel):
    code: str
    timeout_seconds: int | None = Field(default=None, ge=1)
    env: dict[str, str] = Field(default_factory=dict)


class CommandCreate(BaseModel):
    command: str
    timeout_seconds: int | None = Field(default=None, ge=1)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


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
    kind: Literal["code", "command"]
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
    sandbox_budget_mb: int
    reserved_memory_mb: int
    available_reservation_mb: int
    host_available_memory_mb: int
    running_sandboxes: int
    running_executions: int
    queued_executions: int


class AgentExecutionRequest(BaseModel):
    code: str
    timeout_seconds: int
    max_output_bytes: int
    env: dict[str, str] = Field(default_factory=dict)


class AgentCommandRequest(BaseModel):
    command: str
    timeout_seconds: int
    max_output_bytes: int
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


class AgentExecutionResponse(BaseModel):
    logs: LogOutput
    results: list[ExecutionResult] = Field(default_factory=list)
    error: ExecutionError | None = None
    exit_code: int | None = None

    @model_validator(mode="after")
    def error_and_exit_are_consistent(self) -> AgentExecutionResponse:
        if self.error is not None and self.exit_code == 0:
            raise ValueError("an errored execution cannot have exit_code=0")
        return self
