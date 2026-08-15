from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from harborbox.models import Sandbox
    from harborbox.schemas import (
        AgentCommandRequest,
        AgentExecutionRequest,
        AgentExecutionResponse,
        AgentProcessRequest,
        FileListResponse,
        FileReadResponse,
        FileUploadResponse,
        FileWriteRequest,
    )


@dataclass(frozen=True)
class StartedSandbox:
    id: str
    name: str


@dataclass(frozen=True)
class WarmPoolReservation:
    memory_mb: int = 0
    cpu: float = 0.0
    sandboxes: int = 0


class SandboxRuntime(Protocol):
    """Runtime-neutral lifecycle and execution boundary.

    The shape follows the OpenSandbox split between lifecycle operations and
    in-sandbox execution. Docker is the current provider; a remote OpenSandbox
    or Kubernetes provider can implement this protocol without changing the
    scheduler, admission controller, API, or Relaydeck.
    """

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def warm_pool_reservation(self) -> WarmPoolReservation: ...

    async def total_memory_mb(self) -> int: ...

    async def available_memory_mb(self) -> int: ...

    async def start_sandbox(self, sandbox: Sandbox) -> StartedSandbox: ...

    async def wait_until_ready(self, sandbox: Sandbox) -> None: ...

    async def execute_code(
        self, sandbox: Sandbox, request: AgentExecutionRequest
    ) -> AgentExecutionResponse: ...

    async def execute_command(
        self, sandbox: Sandbox, request: AgentCommandRequest
    ) -> AgentExecutionResponse: ...

    async def execute_process(
        self, sandbox: Sandbox, request: AgentProcessRequest
    ) -> AgentExecutionResponse: ...

    async def read_file(self, sandbox: Sandbox, path: str) -> FileReadResponse: ...

    async def write_file(
        self, sandbox: Sandbox, request: FileWriteRequest
    ) -> FileReadResponse: ...

    async def write_file_stream(
        self,
        sandbox: Sandbox,
        path: str,
        content: AsyncIterator[bytes],
    ) -> FileUploadResponse: ...

    async def list_files(self, sandbox: Sandbox, path: str) -> FileListResponse: ...

    async def remove_file(self, sandbox: Sandbox, path: str) -> None: ...

    async def pause(self, sandbox: Sandbox, *, memory: bool) -> None: ...

    async def resume(self, sandbox: Sandbox) -> StartedSandbox: ...

    async def kill(self, sandbox: Sandbox) -> None: ...

    async def container_status(self, sandbox: Sandbox) -> str | None: ...
