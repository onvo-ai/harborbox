from __future__ import annotations

import asyncio
import base64
import logging
import os
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
from opensandbox.exceptions import (
    SandboxApiException,
    SandboxException,
    SandboxReadyTimeoutException,
    SandboxTimeoutException,
)
from opensandbox.models.execd import Execution, ExecutionHandlers, RunCommandOpts
from opensandbox.models.filesystem import DirectoryListEntry

from harborbox.errors import (
    SandboxFileNotFoundError,
    SandboxMemoryExceededError,
    SandboxStartTimeoutError,
    SandboxUnavailableError,
)
from harborbox.runtime_protocol import StartedSandbox, WarmPoolReservation
from harborbox.schemas import (
    ExecutionError,
    ExecutionResult,
    FileEntry,
    FileListResponse,
    FileReadResponse,
    FileUploadResponse,
    FileWriteRequest,
    LogOutput,
    RuntimeCommandRequest,
    RuntimeExecutionResult,
    RuntimeProcessRequest,
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
# execd's error code for a path that does not exist (`model.ErrorCodeFileNotFound`).
FILE_NOT_FOUND_CODE = "FILE_NOT_FOUND"
logger = logging.getLogger(__name__)

# Tokens checked (case-insensitively) against a dead sandbox's live
# `SandboxStatus.reason`/`.message` -- see `_detect_memory_exceeded`. Kept
# narrow and OOM-specific on purpose: a bare "137" would also match an
# unrelated process's own `exit(137)`, so only the qualified forms are
# included.
_OOM_SIGNAL_TOKENS = (
    "oom",
    "out of memory",
    "out-of-memory",
    "exit code 137",
    "code: 137",
    # OpenSandbox's own wording for a container, not a process, that exited on
    # SIGKILL. Under gVisor that is what an OOM looks like: the kernel's OOM
    # killer takes the whole sandbox, and the container exits 137.
    "exited with code 137",
)

# Sandbox states that mean a delete has already happened or is under way.
# OpenSandbox 1.1 drops a deleted sandbox at once, so a lookup is a 404. From
# 1.2 a deleted sandbox reports `Stopping` until its cleanup finishes, then
# `Terminated` (DEV-2614). Either one means "already killed": `kill` must not
# fail on it or delete it a second time.
_GONE_STATES = frozenset({"stopping", "terminated"})

# Sandbox states after which nothing inside can still be producing output.
_DEAD_STATES = _GONE_STATES | {"failed"}


@dataclass(frozen=True)
class _CommandSpec:
    """What to run and how; bundled to keep `_run_command`'s signature small.

    `command` is shell text, or an argv list execd runs with no shell at all.
    """

    command: str | list[str]
    timeout_seconds: int
    max_output_bytes: int
    environment: dict[str, str]
    cwd: str | None


class _LineShape:
    r"""Keep `logs.stdout`/`logs.stderr` in the shape our callers parse.

    execd 1.1 changed its output events (upstream 380322ba): each line now
    arrives with its own `\n`, `\r` or `\r\n`, as a separate event per
    terminator byte. Until 1.1 a line arrived with its terminator stripped, an
    empty line arrived as a lone `"\n"`, and the `\n` of a `\r\n` was
    dropped. Toolspoke joins these chunks with `"\n"`, so passing the new
    shape through would double every newline it reads -- its CLI
    introspection stops at the first blank line. Harborbox's API is a
    contract with the apps that call it, so this turns the new events back
    into the old ones, one stream at a time.
    """

    def __init__(self) -> None:
        self._last_was_cr = False

    def feed(self, text: str) -> str | None:
        """Return the chunk the old execd would have sent, or None for none."""
        if not text or text[-1] not in "\r\n":
            # An unterminated tail, flushed when the command ends.
            self._last_was_cr = False
            return text
        body, terminator = text[:-1], text[-1]
        swallowed = not body and terminator == "\n" and self._last_was_cr
        self._last_was_cr = terminator == "\r"
        if swallowed:
            return None
        return body or "\n"


class _BoundedOutput:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.truncated = False
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self.results: list[ExecutionResult] = []
        self.error: ExecutionError | None = None
        self._stdout_lines = _LineShape()
        self._stderr_lines = _LineShape()

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
        line = self._stdout_lines.feed(str(message.text))
        if line is not None:
            self._append(self.stdout, line)

    async def on_stderr(self, message: OutputMessage) -> None:
        line = self._stderr_lines.feed(str(message.text))
        if line is not None:
            self._append(self.stderr, line)

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

    def response(self, execution: Execution) -> RuntimeExecutionResult:
        error = self.error
        if error is None and execution.error is not None:
            error = ExecutionError(
                name=execution.error.name,
                value=execution.error.value,
                traceback=list(execution.error.traceback),
            )
        return RuntimeExecutionResult(
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
        # The same connection with one number changed. `request_timeout` bounds
        # each HTTP call, and the create POST is a call that legitimately takes
        # tens of seconds -- so on the start path it has to be the start's own
        # budget, not the short bound every other call wants. Sharing
        # `self._transport` means this is a second config over one connection
        # pool, not a second pool.
        self._start_connection = ConnectionConfig(
            api_key=settings.opensandbox_api_key.get_secret_value(),
            domain=settings.opensandbox_domain,
            protocol=settings.opensandbox_protocol,
            request_timeout=timedelta(seconds=settings.sandbox_start_timeout_seconds),
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
                    seconds=self.settings.sandbox_start_timeout_seconds
                ),
                "metadata": self._runtime_metadata(sandbox),
                "resource": {
                    "cpu": str(sandbox.cpu),
                    "memory": f"{sandbox.memory_mb}Mi",
                },
                "connection_config": self._start_connection,
            }
            handle = None
            if not snapshot_id:
                handle = await self._warm_pools.acquire(
                    template=sandbox.metadata_.get("template"),
                    memory_mb=sandbox.memory_mb,
                    cpu=sandbox.cpu,
                    conformance_version=sandbox.conformance_version,
                )
            if snapshot_id:
                handle = await OpenSandbox.create(snapshot_id=snapshot_id, **kwargs)
            elif handle is None:
                template = sandbox.metadata_.get("template")
                handle = await OpenSandbox.create(
                    self.settings.image_spec_for_template(
                        template, sandbox.conformance_version
                    ),
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
            await self._raise_start_error(exc, sandbox)

    async def wait_until_ready(self, sandbox: Sandbox) -> None:
        """Ready means the container answers, which is now all there is to wait for.

        This used to be a careful distinction: readiness deliberately did not
        wait for the Jupyter kernel, because doing so cost every sandbox its
        boot and spawn for a capability the main caller never touched. With the
        kernel and the endpoint it served both gone, there is no second thing
        to become ready -- a sandbox that answers can run commands.
        """
        await self._get_handle(sandbox, check_ready=True)

    async def execute_command(
        self, sandbox: Sandbox, request: RuntimeCommandRequest
    ) -> RuntimeExecutionResult:
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
        self, sandbox: Sandbox, request: RuntimeProcessRequest
    ) -> RuntimeExecutionResult:
        # execd 1.1+ runs an argv list natively, resolving the executable on
        # PATH with no shell in between. Its argv mode has no stdin, though,
        # so a process that needs one still goes through a shell pipe.
        command: str | list[str] = [request.executable, *request.args]
        if request.stdin is not None:
            encoded = base64.b64encode(request.stdin.encode("utf-8")).decode("ascii")
            argv = shlex.join([request.executable, *request.args])
            command = f"printf %s {shlex.quote(encoded)} | base64 -d | {argv}"
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
    ) -> RuntimeExecutionResult:
        """Run one command, and never wait on a sandbox that is gone.

        The SDK reads a command's output stream with no read timeout and
        relies on execd to close it. Under plain Docker an OOM kills only the
        command, execd closes the stream, and that surfaces at once. Under
        gVisor an OOM kills the whole sandbox, network stack included, so the
        stream is never closed and the read never returns -- measured on the
        gVisor trial as an execution still `running` 78s into a 30s timeout.
        So the run races a watcher that polls the sandbox's state, and both
        race a deadline of the command's own timeout plus a grace period.
        """
        handle = await self._get_handle(sandbox, check_ready=True)
        output = _BoundedOutput(spec.max_output_bytes)
        run = asyncio.ensure_future(
            handle.commands.run(
                spec.command,
                opts=RunCommandOpts(
                    working_directory=spec.cwd,
                    timeout=timedelta(seconds=spec.timeout_seconds),
                    envs=spec.environment,
                ),
                handlers=output.handlers(),
            )
        )
        watch = asyncio.ensure_future(self._wait_until_dead(sandbox))
        deadline = spec.timeout_seconds + self.settings.execution_deadline_grace_seconds
        try:
            await asyncio.wait(
                {run, watch}, timeout=deadline, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (run, watch):
                task.cancel()
            await asyncio.gather(run, watch, return_exceptions=True)
        if run.done() and not run.cancelled():
            try:
                return output.response(run.result())
            except SandboxException as exc:
                await self._raise_runtime_error(exc, sandbox)
        if await self._detect_memory_exceeded(sandbox):
            raise SandboxMemoryExceededError(sandbox.memory_mb)
        if watch.done() and not watch.cancelled():
            message = "the sandbox died while the command was running"
        else:
            message = f"the sandbox did not answer within {deadline:g}s"
        raise SandboxUnavailableError(message)

    async def _wait_until_dead(self, sandbox: Sandbox) -> None:
        """Return once OpenSandbox reports `sandbox` in a dead state.

        A failed or slow lookup is not evidence of death, so it is skipped
        rather than returned on: only a state OpenSandbox actually reported
        ends the wait.
        """
        container_id = sandbox.container_id
        if not container_id:
            await asyncio.Event().wait()
            return
        while True:
            await asyncio.sleep(self.settings.execution_liveness_poll_seconds)
            try:
                async with asyncio.timeout(self.settings.oom_diagnostic_timeout_seconds):
                    info = await (await self._get_manager()).get_sandbox_info(
                        container_id
                    )
            except Exception:  # noqa: BLE001, S112 - a missed poll is not a death
                continue
            if str(info.status.state).lower() in _DEAD_STATES:
                return

    async def read_file(self, sandbox: Sandbox, path: str) -> FileReadResponse:
        handle = await self._get_handle(sandbox, check_ready=True)
        try:
            content = await handle.files.read_bytes(path)
        except SandboxException as exc:
            await self._raise_file_error(exc, sandbox, path)
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
            await self._raise_runtime_error(exc, sandbox)
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
                await self._raise_runtime_error(exc, sandbox)
        return FileUploadResponse(path=path, size=size)

    async def list_files(self, sandbox: Sandbox, path: str) -> FileListResponse:
        handle = await self._get_handle(sandbox, check_ready=True)
        try:
            entries = await handle.files.list_directory(
                DirectoryListEntry(path=path, depth=1)
            )
        except SandboxException as exc:
            await self._raise_file_error(exc, sandbox, path)
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
            await self._raise_file_error(exc, sandbox, path)

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
            await self._raise_runtime_error(exc, sandbox)
        finally:
            await handle.close()
            self._sandboxes.pop(sandbox.id, None)

    async def resume(self, sandbox: Sandbox) -> StartedSandbox:
        if not sandbox.container_id:
            return await self.start_sandbox(sandbox)
        try:
            handle = await OpenSandbox.resume(
                sandbox.container_id,
                connection_config=self._start_connection,
                resume_timeout=timedelta(
                    seconds=self.settings.sandbox_start_timeout_seconds
                ),
            )
            self._sandboxes[sandbox.id] = handle
            return StartedSandbox(id=handle.id, name=handle.id)
        except SandboxException as exc:
            await self._raise_start_error(exc, sandbox)

    async def kill(self, sandbox: Sandbox) -> None:
        handle = self._sandboxes.pop(sandbox.id, None)
        if handle is None and sandbox.container_id:
            try:
                handle = await OpenSandbox.connect(
                    sandbox.container_id,
                    connection_config=self._connection,
                    skip_health_check=True,
                )
            except SandboxException as exc:
                await self._raise_unless_gone(exc, sandbox)
        if handle is not None:
            try:
                await handle.kill()
            except SandboxException as exc:
                await self._raise_unless_gone(exc, sandbox)
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
            await self._raise_runtime_error(exc, sandbox)
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
            await self._raise_runtime_error(exc, sandbox)
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

    async def _detect_memory_exceeded(self, sandbox: Sandbox) -> bool:
        """Best-effort live check for whether `sandbox` died of OOM.

        This is the only OOM signal this backend actually has. Unlike
        `DockerRuntime`, which inspects `State.OOMKilled`/exit code 137
        straight from the container runtime after every failure, OpenSandbox
        exposes no such thing to the client: its whole exception taxonomy
        (`SandboxError.{INTERNAL_UNKNOWN_ERROR,READY_TIMEOUT,UNHEALTHY,...}`)
        carries no memory-limit code, and the exception message that used to
        be substring-matched here can never contain "oom" for any of them --
        that was dead code. The one place OpenSandbox *does* expose something
        after the fact is `SandboxStatus.reason`/`.message` on a live
        `get_sandbox_info` call -- a short machine-readable reason code and a
        human-readable message the control plane sets for the sandbox's
        current state.

        This queries that live status and looks for an OOM-shaped reason,
        regardless of `state` -- it used to return `False` outright whenever
        `state == "running"` on the theory that a live container could not
        have died. CI evidence from a real OOM disproved that: the Linux OOM
        killer targets the memory-hungry *process* inside the container's
        cgroup -- the command's own interpreter -- not necessarily the
        container's own PID 1 (execd), so that process dies, the container
        and its `execd` keep running, and `get_sandbox_info` reports `running`
        throughout. That call is answering "is the container up," which is a
        different question from "did something inside it just get killed."
        Whatever `reason`/`message` do or do not say is checked either way
        now; the old state check was filtering out exactly the case this
        exists to catch.

        Even so, this remains a real, unresolved limitation, not merely a
        pending config gap: OpenSandbox's client-visible surface has no
        process-level signal at all, only this container-level one. If a
        given deployment's control plane never populates `reason`/`message`
        with anything OOM-shaped for a process that died inside an
        otherwise-healthy container -- which is the common case observed in
        CI, where the failure surfaces as a severed stream
        (`SandboxException` from a mid-response disconnect) with no
        exploitable status signal at all -- this cannot fire, and the caller
        falls back to whatever generic error the failed call already raised.
        Widening `_OOM_SIGNAL_TOKENS` to match on transport-error text (e.g.
        "peer closed connection", "incomplete chunked read") was considered
        and rejected: that symptom is real and suggestive, but not
        exclusive to OOM -- an ordinary network blip mid-stream would raise
        the identical exception, and there is no way to tell them apart from
        here. Matching on it would trade one guess (the original dead
        substring match on exception text) for another, and mislabel real
        transport faults as memory errors. This is the best available signal
        short of either a process-level exit code (which OpenSandbox does
        not expose to the client today) or a verified read of its
        diagnostics `get_logs`/`get_events` API for an OOM-killer log line,
        which this pass could not confirm without a live stack to test it
        against.

        This is a diagnostic on the error path of every `_raise_runtime_error`
        call site, reached by a caller who has already been waiting. Without an
        explicit short bound it would inherit
        `ConnectionConfig.request_timeout`
        (`opensandbox_ready_timeout_seconds`, 30s by default), turning a
        30s script timeout into up to 60s while still holding the
        execution's CPU reservation -- worst exactly when the control plane
        is degraded enough to be slow to answer this in the first place.
        `oom_diagnostic_timeout_seconds` (3s default) bounds it instead, and
        any failure here -- including that bound itself firing as a
        `TimeoutError`, not a `SandboxException` -- falls back to `False`
        rather than ever stalling or escaping as its own, unrelated error.
        """
        if not sandbox.container_id:
            return False
        try:
            async with asyncio.timeout(self.settings.oom_diagnostic_timeout_seconds):
                info = await (await self._get_manager()).get_sandbox_info(
                    sandbox.container_id
                )
        except Exception:  # noqa: BLE001 - a diagnostic must never fail loud
            # A failed (or too-slow) diagnostic lookup says nothing about why
            # the original call failed; do not let it mask that error with a
            # worse one, or stall it further.
            return False
        # No `state` check: a container-level "running" does not mean nothing
        # inside it died -- see the docstring above.
        haystack = f"{info.status.reason or ''} {info.status.message or ''}".lower()
        return any(token in haystack for token in _OOM_SIGNAL_TOKENS)

    async def _raise_file_error(
        self, exc: SandboxException, sandbox: Sandbox, path: str
    ) -> NoReturn:
        """`_raise_runtime_error`, except that a missing path is the caller's.

        Matched on execd's own `FILE_NOT_FOUND` code, not on HTTP 404: through
        OpenSandbox's server proxy, a sandbox that no longer exists is a 404
        too, and that one really is the sandbox being gone.
        """
        if exc.error.code == FILE_NOT_FOUND_CODE:
            raise SandboxFileNotFoundError(path) from exc
        await self._raise_runtime_error(exc, sandbox)

    async def _raise_unless_gone(
        self, exc: SandboxException, sandbox: Sandbox
    ) -> None:
        """Return if a failed kill step failed because the sandbox is gone.

        A 404 means gone, as before. Any other failure is checked against the
        sandbox's live state: on OpenSandbox 1.2 a deleted sandbox reports
        `Stopping` and then `Terminated` instead of a 404, and connecting to
        or deleting it again may fail. Either state counts as gone. Otherwise
        the failure is raised as it was before: API errors through
        `_raise_runtime_error`, anything else unchanged.

        On 1.1 the state check only runs after a failure, so a kill that works
        makes no extra call.
        """
        if (
            isinstance(exc, SandboxApiException)
            and exc.status_code == HTTPStatus.NOT_FOUND
        ):
            return
        if await self._is_gone(sandbox):
            logger.info(
                "Sandbox %s is already stopping or gone; treating it as killed",
                sandbox.id,
            )
            return
        if isinstance(exc, SandboxApiException):
            await self._raise_runtime_error(exc, sandbox)
        raise exc

    async def _is_gone(self, sandbox: Sandbox) -> bool:
        """Whether OpenSandbox reports `sandbox` deleted or being deleted.

        Best effort and bounded: a failed or slow lookup is not evidence, so it
        answers False and the caller keeps its original error.
        """
        if not sandbox.container_id:
            return False
        try:
            async with asyncio.timeout(self.settings.oom_diagnostic_timeout_seconds):
                info = await (await self._get_manager()).get_sandbox_info(
                    sandbox.container_id
                )
        except SandboxApiException as exc:
            return exc.status_code == HTTPStatus.NOT_FOUND
        except Exception:  # noqa: BLE001 - a failed lookup is not evidence
            return False
        return str(info.status.state).lower() in _GONE_STATES

    async def _raise_runtime_error(
        self, exc: SandboxException, sandbox: Sandbox
    ) -> NoReturn:
        if await self._detect_memory_exceeded(sandbox):
            raise SandboxMemoryExceededError(sandbox.memory_mb) from exc
        raise SandboxUnavailableError(str(exc)) from exc

    @staticmethod
    def _is_start_timeout(exc: SandboxException) -> bool:
        """Whether a failed start ran out of time rather than went wrong.

        `SandboxReadyTimeoutException` is the readiness poll giving up.
        `SandboxTimeoutException` (SDK 1.1+) is the create POST itself
        exceeding `ConnectionConfig.request_timeout` -- a read, write or pool
        timeout. Both are upstream's own names, so they are checked first.

        The SDK does not name the third: a connect timeout becomes
        `SandboxConnectionException`, the same class as a refused connection.
        Only the cause chain still says it was a timeout, so that is what this
        walks -- matching on the message text instead would be the same dead
        substring match `_detect_memory_exceeded`'s docstring already rejects.

        A timeout is safe to call retryable because the SDK cleans up after
        itself: `Sandbox.create` kills the half-built sandbox before raising,
        so a retry starts a new container rather than orphaning the old one.

        Narrow on purpose. Anything else -- a refused create, an unpullable
        image, an unhealthy control plane -- is a dead sandbox, and telling a
        caller to retry that would spin them against an error that never
        clears.
        """
        if isinstance(exc, SandboxReadyTimeoutException | SandboxTimeoutException):
            return True
        seen: set[int] = set()
        cause: BaseException | None = exc
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            if isinstance(cause, httpx.TimeoutException):
                return True
            cause = cause.__cause__
        return False

    async def _raise_start_error(
        self, exc: SandboxException, sandbox: Sandbox
    ) -> NoReturn:
        """`_raise_runtime_error` for the start path, where slow is not dead.

        Every runtime failure used to become `SandboxUnavailableError`, and
        both `_ensure_running` and the API's `ensure_ready` read that as "this
        sandbox is gone" and wrote `failed`. On the start path that turned a
        cold start slower than its own budget into a permanently dead sandbox
        -- the DEV-2032 bug, and one Onvo Lite hit in production on its
        create-upload-transform path.

        `SandboxStartTimeoutError` is the existing signal for "not known to be
        broken, just not up yet" (DEV-1996 introduced it for the caller-side
        deadline). Raising it here means a timed-out start leaves a row a retry
        can still bring up.
        """
        if self._is_start_timeout(exc):
            raise SandboxStartTimeoutError(str(exc)) from exc
        await self._raise_runtime_error(exc, sandbox)
