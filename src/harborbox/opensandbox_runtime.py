from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import shlex
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import httpx
from opensandbox import Sandbox as OpenSandbox
from opensandbox import SandboxManager
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxApiException, SandboxException
from opensandbox.models.execd import Execution, ExecutionHandlers, RunCommandOpts
from opensandbox.models.filesystem import DirectoryListEntry

from harborbox.runtime import SandboxMemoryExceededError, SandboxUnavailableError
from harborbox.runtime_protocol import StartedSandbox, WarmPoolReservation
from harborbox.schemas import (
    AgentCommandRequest,
    AgentExecutionRequest,
    AgentExecutionResponse,
    AgentProcessRequest,
    ExecutionError,
    ExecutionResult,
    FileEntry,
    FileListResponse,
    FileReadResponse,
    FileUploadResponse,
    FileWriteRequest,
    LogOutput,
)
from harborbox.warm_pool import OpenSandboxWarmPools

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from opensandbox.models.execd import ExecutionError as SdkExecutionError
    from opensandbox.models.execd import ExecutionResult as SdkExecutionResult
    from opensandbox.models.execd import OutputMessage

    from harborbox.config import Settings
    from harborbox.models import Sandbox

SNAPSHOT_METADATA_KEY = "harborbox.runtime.snapshot_id"
logger = logging.getLogger(__name__)

# Absolute, not `python`: opensandbox runs commands through its own bootstrap
# and the venv is only on PATH for the image's entrypoint.
SANDBOX_PYTHON = "/opt/venv/bin/python"
CODE_RUNNER_PATH = "/opt/coderun.py"
FORKRUN_PATH = "/opt/forkrun.py"


def _split_result_trailer(
    chunks: list[str], sentinel: str
) -> tuple[list[str], ExecutionResult | None, ExecutionError | None]:
    """Pull `coderun.py`'s trailer off the end of stdout.

    Returns the caller-visible stdout with the trailer removed, plus whatever
    the trailer carried. A missing or unparsable trailer is not an error: output
    is bounded (`_BoundedOutput`), so a large enough body can push the trailer
    past the limit, and losing the final-expression echo is a better outcome
    than failing an execution whose code ran fine.
    """
    joined = "".join(chunks)
    marker = "\n" + sentinel
    index = joined.rfind(marker)
    if index == -1:
        return chunks, None, None

    payload, _, _ = joined[index + len(marker) :].partition("\n")
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return chunks, None, None

    remaining = [joined[:index]] if joined[:index] else []
    error = decoded.get("error")
    return (
        remaining,
        ExecutionResult(text=decoded["text"]) if decoded.get("text") is not None else None,
        ExecutionError(
            name=str(error["name"]),
            value=str(error["value"]),
            traceback=[str(line) for line in error.get("traceback", [])],
        )
        if error
        else None,
    )


@dataclass(frozen=True)
class _CommandSpec:
    """What to run and how; bundled to keep `_run_command`'s signature small."""

    command: str
    timeout_seconds: int
    max_output_bytes: int
    environment: dict[str, str]
    cwd: str | None


class _BoundedOutput:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.truncated = False
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self.results: list[ExecutionResult] = []
        self.error: ExecutionError | None = None

    def _append(self, target: list[str], text: str) -> None:
        encoded = text.encode("utf-8")
        remaining = max(0, self.limit - self.used)
        if remaining == 0:
            self.truncated = True
            return
        chunk = encoded[:remaining]
        target.append(chunk.decode("utf-8", errors="replace"))
        self.used += len(chunk)
        if len(chunk) < len(encoded):
            self.truncated = True

    async def on_stdout(self, message: OutputMessage) -> None:
        self._append(self.stdout, str(message.text))

    async def on_stderr(self, message: OutputMessage) -> None:
        self._append(self.stderr, str(message.text))

    async def on_result(self, result: SdkExecutionResult) -> None:
        text = result.text
        if text is not None:
            encoded = str(text).encode("utf-8")
            remaining = max(0, self.limit - self.used)
            text = encoded[:remaining].decode("utf-8", errors="replace")
            self.used += min(len(encoded), remaining)
            if len(encoded) > remaining:
                self.truncated = True
        extra = dict(getattr(result, "extra_properties", {}) or {})
        self.results.append(ExecutionResult(text=text, data=extra))

    async def on_error(self, error: SdkExecutionError) -> None:
        self.error = ExecutionError(
            name=str(error.name),
            value=str(error.value),
            traceback=[str(line) for line in error.traceback],
        )

    def handlers(self) -> ExecutionHandlers:
        return ExecutionHandlers(
            on_stdout=self.on_stdout,
            on_stderr=self.on_stderr,
            on_result=self.on_result,
            on_error=self.on_error,
            skip_accumulation=True,
        )

    def response(self, execution: Execution) -> AgentExecutionResponse:
        error = self.error
        if error is None and execution.error is not None:
            error = ExecutionError(
                name=execution.error.name,
                value=execution.error.value,
                traceback=list(execution.error.traceback),
            )
        return AgentExecutionResponse(
            logs=LogOutput(
                stdout=self.stdout,
                stderr=self.stderr,
                truncated=self.truncated,
            ),
            results=self.results,
            error=error,
            exit_code=execution.exit_code,
        )


class OpenSandboxRuntime:
    """OpenSandbox-backed runtime for Harborbox admission and orchestration.

    Harborbox owns durable jobs, admission, quotas, and desired state. This
    adapter deliberately delegates sandbox lifecycle, isolation, execution,
    files, networking, and snapshots to an upstream OpenSandbox server.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._transport = httpx.AsyncHTTPTransport(
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=30,
            )
        )
        self._connection = ConnectionConfig(
            api_key=settings.opensandbox_api_key.get_secret_value(),
            domain=settings.opensandbox_domain,
            protocol=settings.opensandbox_protocol,
            request_timeout=timedelta(
                seconds=settings.opensandbox_ready_timeout_seconds
            ),
            use_server_proxy=settings.opensandbox_use_server_proxy,
            disable_metrics=True,
            transport=self._transport,
        )
        self._sandboxes: dict[str, OpenSandbox] = {}
        self._manager: SandboxManager | None = None
        self._manager_lock = asyncio.Lock()
        self._warm_pools = OpenSandboxWarmPools(settings, self._connection)

    async def start(self) -> None:
        await self._warm_pools.start()

    def warm_pool_reservation(self) -> WarmPoolReservation:
        return self._warm_pools.reservation()

    async def close(self) -> None:
        await self._warm_pools.close()
        handles = list(self._sandboxes.values())
        self._sandboxes.clear()
        await asyncio.gather(
            *(handle.close() for handle in handles), return_exceptions=True
        )
        if self._manager is not None:
            await self._manager.close()
        await self._transport.aclose()

    async def total_memory_mb(self) -> int:
        if self.settings.total_memory_mb is not None:
            return self.settings.total_memory_mb
        total = await asyncio.to_thread(self._read_meminfo_mb, "MemTotal")
        if total > 0:
            return total
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size) // (1024 * 1024)

    async def available_memory_mb(self) -> int:
        available = await asyncio.to_thread(self._read_meminfo_mb, "MemAvailable")
        return available if available > 0 else await self.total_memory_mb()

    @staticmethod
    def _read_meminfo_mb(key: str) -> int:
        try:
            with Path("/proc/meminfo").open(encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith(f"{key}:"):
                        return int(line.split()[1]) // 1024
        except (OSError, ValueError):
            pass
        return 0

    async def start_sandbox(self, sandbox: Sandbox) -> StartedSandbox:
        snapshot_id = sandbox.metadata_.get(SNAPSHOT_METADATA_KEY)
        try:
            kwargs: dict[str, Any] = {
                "timeout": None,
                "ready_timeout": timedelta(
                    seconds=self.settings.opensandbox_ready_timeout_seconds
                ),
                "metadata": self._runtime_metadata(sandbox),
                "resource": {
                    "cpu": str(sandbox.cpu),
                    "memory": f"{sandbox.memory_mb}Mi",
                },
                "connection_config": self._connection,
            }
            handle = None
            if not snapshot_id:
                handle = await self._warm_pools.acquire(
                    template=sandbox.metadata_.get("template"),
                    memory_mb=sandbox.memory_mb,
                    cpu=sandbox.cpu,
                )
            if snapshot_id:
                handle = await OpenSandbox.create(snapshot_id=snapshot_id, **kwargs)
            elif handle is None:
                template = sandbox.metadata_.get("template")
                handle = await OpenSandbox.create(
                    self.settings.image_for_template(template),
                    entrypoint=self.settings.entrypoint_for_template(template),
                    **kwargs,
                )
            else:
                metadata = dict(sandbox.metadata_)
                metadata["harborbox.runtime.warm_pool"] = "true"
                sandbox.metadata_ = metadata
            self._sandboxes[sandbox.id] = handle
            return StartedSandbox(id=handle.id, name=handle.id)
        except SandboxException as exc:
            self._raise_runtime_error(exc, sandbox)

    async def wait_until_ready(self, sandbox: Sandbox) -> None:
        """Ready means the container answers, which is now all there is to wait for.

        This used to be a careful distinction: readiness deliberately did not
        wait for the Jupyter kernel, because doing so cost every sandbox its
        boot and spawn for a capability the main caller never touched, and
        `execute_code` paid it instead. With the kernel gone there is no second
        thing to become ready -- a sandbox that answers can run Python.
        """
        await self._get_handle(sandbox, check_ready=True)

    async def execute_code(
        self, sandbox: Sandbox, request: AgentExecutionRequest
    ) -> AgentExecutionResponse:
        """Run Python as an ordinary command, via `coderun.py`.

        There is no kernel behind this any more. execd runs bash directly and
        only ever proxied Python to a Jupyter server, so serving this endpoint
        meant starting one in every sandbox: ~3 s of boot and ~197 MB resident
        before a line of user code ran, on templates whose main caller reaches
        `/commands` instead. `coderun.py` reproduces the one thing the kernel
        gave that a script does not -- the final-expression echo -- and
        `forkrun.py` keeps the pandas import off the per-call path.

        The old per-call-environment special case is gone with the kernel that
        motivated it. Every execution is now a fresh forked child, so a secret
        cannot linger in a persistent interpreter; there is no persistent
        interpreter left to linger in.
        """
        sentinel = f"__harborbox_result_{secrets.token_hex(16)}__"
        code_path = f"/tmp/harborbox-code-{secrets.token_hex(8)}.py"  # noqa: S108
        encoded = base64.b64encode(request.code.encode("utf-8")).decode("ascii")
        quoted_path = shlex.quote(code_path)
        # forkrun is only in the images that carry pandas; relaydeck has no use
        # for a preloading daemon and does not ship one, so fall back to running
        # the runner directly rather than assuming it is there.
        command = (
            f"printf %s {shlex.quote(encoded)} | base64 -d > {quoted_path} && "
            f"{{ if [ -f {FORKRUN_PATH} ]; then "
            f"{SANDBOX_PYTHON} {FORKRUN_PATH} {CODE_RUNNER_PATH}; else "
            f"{SANDBOX_PYTHON} {CODE_RUNNER_PATH}; fi; }}; "
            f"rc=$?; rm -f {quoted_path}; exit $rc"
        )
        response = await self._run_command(
            sandbox,
            _CommandSpec(
                command=command,
                timeout_seconds=request.timeout_seconds,
                max_output_bytes=request.max_output_bytes,
                environment={
                    **request.env,
                    "HARBORBOX_CODE_PATH": code_path,
                    "HARBORBOX_RESULT_SENTINEL": sentinel,
                },
                cwd="/workspace",
            ),
        )
        stdout, result, error = _split_result_trailer(response.logs.stdout, sentinel)
        return AgentExecutionResponse(
            logs=LogOutput(
                stdout=stdout,
                stderr=response.logs.stderr,
                truncated=response.logs.truncated,
            ),
            results=[result] if result is not None else [],
            error=error or response.error,
            exit_code=response.exit_code,
        )

    async def execute_command(
        self, sandbox: Sandbox, request: AgentCommandRequest
    ) -> AgentExecutionResponse:
        return await self._run_command(
            sandbox,
            _CommandSpec(
                command=request.command,
                timeout_seconds=request.timeout_seconds,
                max_output_bytes=request.max_output_bytes,
                environment=request.env,
                cwd=request.cwd,
            ),
        )

    async def execute_process(
        self, sandbox: Sandbox, request: AgentProcessRequest
    ) -> AgentExecutionResponse:
        command = shlex.join([request.executable, *request.args])
        if request.stdin is not None:
            encoded = base64.b64encode(request.stdin.encode("utf-8")).decode("ascii")
            command = f"printf %s {shlex.quote(encoded)} | base64 -d | {command}"
        return await self._run_command(
            sandbox,
            _CommandSpec(
                command=command,
                timeout_seconds=request.timeout_seconds,
                max_output_bytes=request.max_output_bytes,
                environment=request.env,
                cwd=request.cwd,
            ),
        )

    async def _run_command(
        self, sandbox: Sandbox, spec: _CommandSpec
    ) -> AgentExecutionResponse:
        handle = await self._get_handle(sandbox, check_ready=True)
        output = _BoundedOutput(spec.max_output_bytes)
        try:
            execution = await handle.commands.run(
                spec.command,
                opts=RunCommandOpts(
                    working_directory=spec.cwd,
                    timeout=timedelta(seconds=spec.timeout_seconds),
                    envs=spec.environment,
                ),
                handlers=output.handlers(),
            )
            return output.response(execution)
        except SandboxException as exc:
            self._raise_runtime_error(exc, sandbox)

    async def read_file(self, sandbox: Sandbox, path: str) -> FileReadResponse:
        handle = await self._get_handle(sandbox, check_ready=True)
        try:
            content = await handle.files.read_bytes(path)
        except SandboxException as exc:
            self._raise_runtime_error(exc, sandbox)
        try:
            return FileReadResponse(
                path=path, content=content.decode("utf-8"), encoding="utf-8"
            )
        except UnicodeDecodeError:
            return FileReadResponse(
                path=path,
                content=base64.b64encode(content).decode("ascii"),
                encoding="base64",
            )

    async def write_file(
        self, sandbox: Sandbox, request: FileWriteRequest
    ) -> FileReadResponse:
        content = (
            request.content.encode("utf-8")
            if request.encoding == "utf-8"
            else base64.b64decode(request.content, validate=True)
        )
        handle = await self._get_handle(sandbox, check_ready=True)
        try:
            await handle.files.write_file(request.path, content)
        except SandboxException as exc:
            self._raise_runtime_error(exc, sandbox)
        return FileReadResponse(
            path=request.path,
            content=request.content,
            encoding=request.encoding,
        )

    async def write_file_stream(
        self,
        sandbox: Sandbox,
        path: str,
        content: AsyncIterator[bytes],
    ) -> FileUploadResponse:
        handle = await self._get_handle(sandbox, check_ready=True)
        size = 0
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as upload:
            async for chunk in content:
                size += len(chunk)
                if size > self.settings.max_upload_bytes:
                    message = "file upload exceeds configured limit"
                    raise SandboxUnavailableError(message)
                upload.write(chunk)
            upload.seek(0)
            try:
                await handle.files.write_file(path, upload)
            except SandboxException as exc:
                self._raise_runtime_error(exc, sandbox)
        return FileUploadResponse(path=path, size=size)

    async def list_files(self, sandbox: Sandbox, path: str) -> FileListResponse:
        handle = await self._get_handle(sandbox, check_ready=True)
        try:
            entries = await handle.files.list_directory(
                DirectoryListEntry(path=path, depth=1)
            )
        except SandboxException as exc:
            self._raise_runtime_error(exc, sandbox)
        return FileListResponse(
            path=path,
            entries=[
                FileEntry(
                    name=entry.path.rstrip("/").rsplit("/", 1)[-1],
                    path=entry.path,
                    type=(
                        "directory"
                        if (entry.entry_type or "").lower() in {"dir", "directory"}
                        else "file"
                    ),
                    size=entry.size,
                )
                for entry in entries
            ],
        )

    async def remove_file(self, sandbox: Sandbox, path: str) -> None:
        handle = await self._get_handle(sandbox, check_ready=True)
        try:
            info = (await handle.files.get_file_info([path])).get(path)
            if info and (info.entry_type or "").lower() in {"dir", "directory"}:
                await handle.files.delete_directories([path])
            else:
                await handle.files.delete_files([path])
        except SandboxException as exc:
            self._raise_runtime_error(exc, sandbox)

    async def pause(self, sandbox: Sandbox, *, memory: bool) -> None:
        if not sandbox.container_id:
            return
        handle = await self._get_handle(sandbox, check_ready=False)
        try:
            if memory:
                await handle.pause()
            else:
                previous_snapshot_id = sandbox.metadata_.get(SNAPSHOT_METADATA_KEY)
                snapshot = await handle.create_snapshot(
                    name=f"harborbox-{sandbox.id}"
                )
                await self._wait_snapshot_ready(snapshot.id)
                await handle.kill()
                if (
                    previous_snapshot_id
                    and previous_snapshot_id != snapshot.id
                ):
                    try:
                        await self._delete_snapshot(previous_snapshot_id)
                    except SandboxUnavailableError:
                        logger.warning(
                            "Could not delete replaced snapshot %s for sandbox %s",
                            previous_snapshot_id,
                            sandbox.id,
                        )
                metadata = dict(sandbox.metadata_)
                metadata[SNAPSHOT_METADATA_KEY] = snapshot.id
                sandbox.metadata_ = metadata
        except SandboxException as exc:
            self._raise_runtime_error(exc, sandbox)
        finally:
            await handle.close()
            self._sandboxes.pop(sandbox.id, None)

    async def resume(self, sandbox: Sandbox) -> StartedSandbox:
        if not sandbox.container_id:
            return await self.start_sandbox(sandbox)
        try:
            handle = await OpenSandbox.resume(
                sandbox.container_id,
                connection_config=self._connection,
                resume_timeout=timedelta(
                    seconds=self.settings.opensandbox_ready_timeout_seconds
                ),
            )
            self._sandboxes[sandbox.id] = handle
            return StartedSandbox(id=handle.id, name=handle.id)
        except SandboxException as exc:
            self._raise_runtime_error(exc, sandbox)

    async def kill(self, sandbox: Sandbox) -> None:
        handle = self._sandboxes.pop(sandbox.id, None)
        if handle is None and sandbox.container_id:
            try:
                handle = await OpenSandbox.connect(
                    sandbox.container_id,
                    connection_config=self._connection,
                    skip_health_check=True,
                )
            except SandboxApiException as exc:
                if exc.status_code != HTTPStatus.NOT_FOUND:
                    self._raise_runtime_error(exc, sandbox)
        if handle is not None:
            try:
                await handle.kill()
            except SandboxApiException as exc:
                if exc.status_code != HTTPStatus.NOT_FOUND:
                    self._raise_runtime_error(exc, sandbox)
            finally:
                await handle.close()

        snapshot_id = sandbox.metadata_.get(SNAPSHOT_METADATA_KEY)
        if snapshot_id:
            await self._delete_snapshot(snapshot_id, ignore_missing=True)
            metadata = dict(sandbox.metadata_)
            metadata.pop(SNAPSHOT_METADATA_KEY, None)
            sandbox.metadata_ = metadata

    async def container_status(self, sandbox: Sandbox) -> str | None:
        if not sandbox.container_id:
            return None
        try:
            info = await (await self._get_manager()).get_sandbox_info(
                sandbox.container_id
            )
        except SandboxApiException as exc:
            if exc.status_code == HTTPStatus.NOT_FOUND:
                return None
            self._raise_runtime_error(exc, sandbox)
        state = info.status.state.lower()
        return {
            "running": "running",
            "pending": "created",
            "pausing": "paused",
            "paused": "paused",
            "resuming": "created",
            "stopping": "exited",
            "terminated": "exited",
            "failed": "dead",
        }.get(state, state)

    async def _get_handle(
        self, sandbox: Sandbox, *, check_ready: bool
    ) -> OpenSandbox:
        cached = self._sandboxes.get(sandbox.id)
        if cached is not None:
            return cached
        if not sandbox.container_id:
            message = "sandbox has no OpenSandbox runtime id"
            raise SandboxUnavailableError(message)
        try:
            handle = await OpenSandbox.connect(
                sandbox.container_id,
                connection_config=self._connection,
                connect_timeout=timedelta(
                    seconds=self.settings.opensandbox_ready_timeout_seconds
                ),
                skip_health_check=not check_ready,
            )
        except SandboxException as exc:
            self._raise_runtime_error(exc, sandbox)
        self._sandboxes[sandbox.id] = handle
        return handle

    async def _get_manager(self) -> SandboxManager:
        if self._manager is not None:
            return self._manager
        async with self._manager_lock:
            if self._manager is None:
                self._manager = await SandboxManager.create(self._connection)
        return self._manager

    async def _wait_snapshot_ready(self, snapshot_id: str) -> None:
        deadline = (
            asyncio.get_running_loop().time()
            + self.settings.opensandbox_snapshot_timeout_seconds
        )
        manager = await self._get_manager()
        while asyncio.get_running_loop().time() < deadline:
            snapshot = await manager.get_snapshot(snapshot_id)
            state = snapshot.status.state.lower()
            if state == "ready":
                return
            if state == "failed":
                raise SandboxUnavailableError(
                    snapshot.status.message or "OpenSandbox snapshot failed"
                )
            await asyncio.sleep(0.1)
        message = "OpenSandbox snapshot did not become ready"
        raise SandboxUnavailableError(message)

    async def _delete_snapshot(
        self, snapshot_id: str, *, ignore_missing: bool = False
    ) -> None:
        try:
            await (await self._get_manager()).delete_snapshot(snapshot_id)
        except SandboxApiException as exc:
            if not (ignore_missing and exc.status_code == HTTPStatus.NOT_FOUND):
                raise SandboxUnavailableError(str(exc)) from exc

    @staticmethod
    def _runtime_metadata(sandbox: Sandbox) -> dict[str, str]:
        metadata = {
            key: value
            for key, value in sandbox.metadata_.items()
            if key != SNAPSHOT_METADATA_KEY
        }
        metadata["harborbox.sandbox_id"] = sandbox.id
        return metadata

    @staticmethod
    def _raise_runtime_error(exc: SandboxException, sandbox: Sandbox) -> NoReturn:
        message = str(exc)
        lowered = message.lower()
        if "oom" in lowered or "out of memory" in lowered or "exit code 137" in lowered:
            raise SandboxMemoryExceededError(sandbox.memory_mb) from exc
        raise SandboxUnavailableError(message) from exc
