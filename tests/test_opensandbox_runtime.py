from __future__ import annotations

import asyncio
import base64
import logging
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from types import SimpleNamespace
from typing import IO, TYPE_CHECKING, Any, NoReturn

import pytest
from opensandbox.exceptions import (
    SandboxApiException,
    SandboxException,
    SandboxReadyTimeoutException,
)
from opensandbox.models.execd import Execution, ExecutionHandlers, RunCommandOpts
from opensandbox.models.execd import ExecutionError as SdkError

import harborbox.opensandbox_runtime as runtime_module
from harborbox.config import Settings
from harborbox.errors import (
    SandboxMemoryExceededError,
    SandboxStartTimeoutError,
    SandboxUnavailableError,
)
from harborbox.models import Sandbox
from harborbox.opensandbox_runtime import (
    SNAPSHOT_METADATA_KEY,
    OpenSandboxRuntime,
    _BoundedOutput,
)
from harborbox.runtime_protocol import WarmPoolReservation
from harborbox.schemas import FileWriteRequest, RuntimeCommandRequest, RuntimeProcessRequest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from opensandbox.models.filesystem import DirectoryListEntry


# `overrides` can set any Sandbox column, whose types are heterogeneous
# (str, int, float, dict, datetime, ...), so Any is the honest type here.
def sandbox_record(**overrides: Any) -> Sandbox:  # noqa: ANN401
    values: dict[str, Any] = {
        "id": "sbx-test",
        "status": "created",
        "container_id": None,
        "container_name": None,
        "agent_token": "unused-with-opensandbox",
        "memory_mb": 768,
        "cpu": 1.5,
        "pids_limit": 128,
        "idle_timeout_seconds": 60,
        "metadata_": {"template": "base", "team": "test"},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "last_activity_at": datetime.now(UTC),
    }
    values.update(overrides)
    return Sandbox(**values)


class FakeHandle:
    def __init__(self, sandbox_id: str = "osb-runtime-1") -> None:
        self.id = sandbox_id
        self.killed = False
        self.closed = False
        self.paused = False

    async def close(self) -> None:
        self.closed = True

    async def kill(self) -> None:
        self.killed = True

    async def pause(self) -> None:
        self.paused = True

    async def create_snapshot(self, name: str | None = None) -> SimpleNamespace:
        assert name == "harborbox-sbx-test"
        return SimpleNamespace(id="snap-test")


COMMAND_FAILURE_MESSAGE = "command blew up"


class FailingCommandHandle(FakeHandle):
    """A handle whose command run always raises, so `_raise_runtime_error` decides."""

    @property
    def commands(self) -> SimpleNamespace:
        async def run(*_: object, **__: object) -> None:
            raise SandboxException(COMMAND_FAILURE_MESSAGE)

        return SimpleNamespace(run=run)


class FakeManager:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        # What get_sandbox_info() returns, or an exception it raises instead.
        # Tests that care set this before triggering the call.
        self.sandbox_info: SimpleNamespace | None = None
        self.sandbox_info_error: Exception | None = None
        # How long get_sandbox_info() takes before returning/raising --
        # exercises _detect_memory_exceeded's own bound.
        self.sandbox_info_delay: float = 0.0

    async def get_snapshot(self, snapshot_id: str) -> SimpleNamespace:
        assert snapshot_id == "snap-test"
        return SimpleNamespace(
            status=SimpleNamespace(state="Ready", message=None)
        )

    async def delete_snapshot(self, snapshot_id: str) -> None:
        self.deleted.append(snapshot_id)

    async def get_sandbox_info(self, sandbox_id: str) -> SimpleNamespace:  # noqa: ARG002
        if self.sandbox_info_delay:
            await asyncio.sleep(self.sandbox_info_delay)
        if self.sandbox_info_error is not None:
            raise self.sandbox_info_error
        assert self.sandbox_info is not None, "test must set sandbox_info first"
        return self.sandbox_info

    async def close(self) -> None:
        return None


def sandbox_info(
    *, state: str = "terminated", reason: str | None = None, message: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(status=SimpleNamespace(state=state, reason=reason, message=message))


@pytest.mark.asyncio
async def test_start_delegates_image_and_resource_limits_to_opensandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    handle = FakeHandle()

    class FakeOpenSandbox:
        @classmethod
        # Stands in for opensandbox.Sandbox.create and exists to capture
        # whatever the runtime passes it, so it must accept anything.
        async def create(cls, *args: Any, **kwargs: Any) -> FakeHandle:  # noqa: ANN401
            captured["args"] = args
            captured["kwargs"] = kwargs
            return handle

    monkeypatch.setattr(runtime_module, "OpenSandbox", FakeOpenSandbox)
    runtime = OpenSandboxRuntime(Settings())
    sandbox = sandbox_record()

    started = await runtime.start_sandbox(sandbox)

    assert started.id == "osb-runtime-1"
    assert captured["args"] == ("harborbox-sandbox-base:local",)
    assert captured["kwargs"]["resource"] == {
        "cpu": "1.5",
        "memory": "768Mi",
    }
    assert captured["kwargs"]["metadata"]["harborbox.sandbox_id"] == "sbx-test"
    assert captured["kwargs"]["timeout"] is None
    await runtime.close()


@pytest.mark.asyncio
async def test_start_sends_registry_credentials_when_one_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry auth reaches opensandbox per create call, not from its config file.

    There is no server-side credential store to fall back on, so an image on a
    private registry is unpullable unless every create carries the auth.
    """
    captured: dict[str, Any] = {}

    class FakeOpenSandbox:
        @classmethod
        async def create(cls, *args: Any, **_kwargs: Any) -> FakeHandle:  # noqa: ANN401
            captured["args"] = args
            return FakeHandle()

    monkeypatch.setattr(runtime_module, "OpenSandbox", FakeOpenSandbox)
    runtime = OpenSandboxRuntime(
        Settings(
            registry_pull_endpoint="127.0.0.1:5050",
            registry_username="harborbox",
            registry_password="s3cret",  # noqa: S106 - a fixture, not a credential
        )
    )

    await runtime.start_sandbox(sandbox_record())

    spec = captured["args"][0]
    assert spec.image == "127.0.0.1:5050/harborbox-sandbox-base:local"
    assert spec.auth is not None
    assert spec.auth.username == "harborbox"
    assert spec.auth.password == "s3cret"  # noqa: S105 - as above
    await runtime.close()


@pytest.mark.asyncio
async def test_start_sends_a_bare_image_when_no_registry_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeOpenSandbox:
        @classmethod
        async def create(cls, *args: Any, **_kwargs: Any) -> FakeHandle:  # noqa: ANN401
            captured["args"] = args
            return FakeHandle()

    monkeypatch.setattr(runtime_module, "OpenSandbox", FakeOpenSandbox)
    runtime = OpenSandboxRuntime(Settings())

    await runtime.start_sandbox(sandbox_record())

    assert captured["args"] == ("harborbox-sandbox-base:local",)
    await runtime.close()


@pytest.mark.asyncio
async def test_start_claims_matching_warm_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = FakeHandle("osb-warm-1")

    # Stands in for the warm pool's acquire() and exists to assert on
    # whatever kwargs the runtime forwards, so it must accept anything.
    async def acquire(**kwargs: Any) -> FakeHandle:  # noqa: ANN401
        assert kwargs == {
            "template": "relaydeck",
            "memory_mb": 256,
            "cpu": 0.5,
        }
        return handle

    runtime = OpenSandboxRuntime(Settings())
    monkeypatch.setattr(runtime._warm_pools, "acquire", acquire)
    sandbox = sandbox_record(
        memory_mb=256,
        cpu=0.5,
        metadata_={"template": "relaydeck"},
    )

    started = await runtime.start_sandbox(sandbox)

    assert started.id == "osb-warm-1"
    assert sandbox.metadata_["harborbox.runtime.warm_pool"] == "true"
    await runtime.close()


@pytest.mark.asyncio
async def test_cold_pause_snapshots_kills_and_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = FakeHandle()
    restored = FakeHandle("osb-runtime-2")
    manager = FakeManager()
    captured: dict[str, Any] = {}

    class FakeOpenSandbox:
        @classmethod
        # Stands in for opensandbox.Sandbox.create and exists to capture
        # whatever the runtime passes it, so it must accept anything.
        async def create(cls, *args: Any, **kwargs: Any) -> FakeHandle:  # noqa: ANN401
            captured["args"] = args
            captured["kwargs"] = kwargs
            return restored

    monkeypatch.setattr(runtime_module, "OpenSandbox", FakeOpenSandbox)
    runtime = OpenSandboxRuntime(Settings())
    runtime._manager = manager  # type: ignore[assignment]
    runtime._sandboxes["sbx-test"] = original
    sandbox = sandbox_record(
        status="running",
        container_id="osb-runtime-1",
        container_name="osb-runtime-1",
    )

    await runtime.pause(sandbox, memory=False)

    assert original.killed is True
    assert original.closed is True
    assert sandbox.metadata_[SNAPSHOT_METADATA_KEY] == "snap-test"

    sandbox.container_id = None
    sandbox.container_name = None
    started = await runtime.start_sandbox(sandbox)

    assert started.id == "osb-runtime-2"
    assert captured["args"] == ()
    assert captured["kwargs"]["snapshot_id"] == "snap-test"
    assert sandbox.metadata_[SNAPSHOT_METADATA_KEY] == "snap-test"
    assert manager.deleted == []

    sandbox.container_id = started.id
    sandbox.container_name = started.name
    await runtime.kill(sandbox)

    assert restored.killed is True
    assert SNAPSHOT_METADATA_KEY not in sandbox.metadata_
    assert manager.deleted == ["snap-test"]
    await runtime.close()


@pytest.mark.asyncio
async def test_output_collector_enforces_byte_limit() -> None:
    output = _BoundedOutput(5)

    await output.on_stdout(SimpleNamespace(text="abcdef"))
    await output.on_stderr(SimpleNamespace(text="ignored"))

    assert output.stdout == ["abcde"]
    assert output.stderr == []
    assert output.truncated is True


# --- OOM detection (task 21, failure #5) ------------------------------------
#
# OpenSandbox's client-visible exception taxonomy carries no memory-limit
# code (see `opensandbox.exceptions.sandbox.SandboxError`), so the substring
# match this used to do against `str(exc)` could never fire -- confirmed by
# `test_raise_runtime_error_ignores_oom_text_in_the_exception_itself` below.
# The only live signal this backend has is `SandboxStatus.reason`/`.message`
# from `get_sandbox_info`, which `_detect_memory_exceeded` queries.


class TestDetectMemoryExceeded:
    async def _runtime(
        self, manager: FakeManager, settings: Settings | None = None
    ) -> OpenSandboxRuntime:
        runtime = OpenSandboxRuntime(settings or Settings())
        runtime._manager = manager  # type: ignore[assignment]
        return runtime

    @pytest.mark.asyncio
    async def test_no_container_id_is_not_treated_as_oom(self) -> None:
        runtime = await self._runtime(FakeManager())
        sandbox = sandbox_record(container_id=None)

        assert await runtime._detect_memory_exceeded(sandbox) is False

    @pytest.mark.asyncio
    async def test_a_running_sandbox_with_no_oom_signal_is_not_treated_as_oom(
        self,
    ) -> None:
        manager = FakeManager()
        manager.sandbox_info = sandbox_info(state="running", reason="config-updated")
        runtime = await self._runtime(manager)
        sandbox = sandbox_record(container_id="osb-x")

        assert await runtime._detect_memory_exceeded(sandbox) is False

    @pytest.mark.asyncio
    async def test_a_running_sandbox_can_still_be_reported_as_oom(self) -> None:
        """Round-2 regression test.

        This used to be `test_a_still_running_sandbox_is_not_treated_as_oom`
        and asserted the opposite: that `state == "running"` always meant
        "not OOM," even with an OOM-shaped `reason`. CI evidence disproved
        that theory -- the Linux OOM killer targets the memory-hungry kernel
        process inside the container's cgroup, not necessarily the
        container's own PID 1, so the container (and this status call)
        legitimately reports `running` throughout. The state check is gone;
        only the reason/message content decides now.
        """
        manager = FakeManager()
        manager.sandbox_info = sandbox_info(state="running", reason="OOMKilled")
        runtime = await self._runtime(manager)
        sandbox = sandbox_record(container_id="osb-x")

        assert await runtime._detect_memory_exceeded(sandbox) is True

    @pytest.mark.asyncio
    async def test_a_failed_diagnostic_lookup_does_not_claim_oom(self) -> None:
        manager = FakeManager()
        manager.sandbox_info_error = SandboxException("info endpoint unreachable")
        runtime = await self._runtime(manager)
        sandbox = sandbox_record(container_id="osb-x")

        assert await runtime._detect_memory_exceeded(sandbox) is False

    @pytest.mark.asyncio
    async def test_an_unexpected_exception_type_does_not_claim_oom_either(self) -> None:
        """The diagnostic lookup used to only catch `SandboxException`.

        A bare `RuntimeError` (or, as in production, the `TimeoutError` the
        bound below raises) must be swallowed the same way, not escape and
        replace the caller's real error.
        """
        manager = FakeManager()
        manager.sandbox_info_error = RuntimeError("transport blew up")
        runtime = await self._runtime(manager)
        sandbox = sandbox_record(container_id="osb-x")

        assert await runtime._detect_memory_exceeded(sandbox) is False

    @pytest.mark.asyncio
    async def test_a_slow_diagnostic_lookup_is_bounded_and_falls_back(self) -> None:
        """IMPORTANT 2: the lookup must not inherit the 30s connection timeout.

        A control plane slow enough to blow `oom_diagnostic_timeout_seconds`
        must not add that latency on top of every one of the 14 error sites
        this feeds -- it times out on its own short bound and reports "not
        OOM" rather than stalling the caller further.
        """
        manager = FakeManager()
        manager.sandbox_info_delay = 0.1
        manager.sandbox_info = sandbox_info(state="terminated", reason="OOMKilled")
        settings = Settings(oom_diagnostic_timeout_seconds=0.01)
        runtime = await self._runtime(manager, settings)
        sandbox = sandbox_record(container_id="osb-x")

        started = asyncio.get_running_loop().time()
        result = await runtime._detect_memory_exceeded(sandbox)
        elapsed = asyncio.get_running_loop().time() - started

        assert result is False
        assert elapsed < manager.sandbox_info_delay

    @pytest.mark.asyncio
    async def test_a_dead_sandbox_with_no_reason_is_not_treated_as_oom(self) -> None:
        manager = FakeManager()
        manager.sandbox_info = sandbox_info(state="terminated")
        runtime = await self._runtime(manager)
        sandbox = sandbox_record(container_id="osb-x")

        assert await runtime._detect_memory_exceeded(sandbox) is False

    @pytest.mark.parametrize(
        ("reason", "message"),
        [
            ("OOMKilled", None),
            (None, "container was killed: out of memory"),
            ("Killed", "process exited with exit code 137"),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_oom_shaped_reason_or_message_is_detected(
        self, reason: str | None, message: str | None
    ) -> None:
        manager = FakeManager()
        manager.sandbox_info = sandbox_info(state="terminated", reason=reason, message=message)
        runtime = await self._runtime(manager)
        sandbox = sandbox_record(container_id="osb-x")

        assert await runtime._detect_memory_exceeded(sandbox) is True


@pytest.mark.asyncio
async def test_raise_runtime_error_reports_oom_from_live_status() -> None:
    manager = FakeManager()
    manager.sandbox_info = sandbox_info(state="failed", message="sandbox died: OOM")
    runtime = OpenSandboxRuntime(Settings())
    runtime._manager = manager  # type: ignore[assignment]
    sandbox = sandbox_record(container_id="osb-x")

    with pytest.raises(SandboxMemoryExceededError):
        await runtime._raise_runtime_error(SandboxException("upstream call failed"), sandbox)


@pytest.mark.asyncio
async def test_raise_runtime_error_ignores_oom_text_in_the_exception_itself() -> None:
    """The old behaviour: substring-matching `str(exc)` for "oom" is gone.

    Only the live `get_sandbox_info` status is trusted now, not the
    exception's own text -- confirming the previous dead-code match (the SDK
    taxonomy can never produce those substrings, but a test double easily
    can) no longer drives the outcome.
    """
    manager = FakeManager()
    manager.sandbox_info = sandbox_info(state="terminated", reason="ManualStop")
    runtime = OpenSandboxRuntime(Settings())
    runtime._manager = manager  # type: ignore[assignment]
    sandbox = sandbox_record(container_id="osb-x")

    with pytest.raises(SandboxUnavailableError, match="ran out of memory: oom"):
        await runtime._raise_runtime_error(
            SandboxException("ran out of memory: oom"), sandbox
        )


# --- The operations surface: commands, files, lifecycle ----------------------
#
# Everything below drives the adapter through a fake OpenSandbox handle and
# asserts what Harborbox's callers see: the result shapes, the encodings, and
# which Harborbox error an SDK failure turns into. A sandbox with no
# `container_id` makes `_detect_memory_exceeded` answer False without a
# manager, so an SDK failure there is always `SandboxUnavailableError`.


class FakeCommands:
    def __init__(self, execution: Execution | None = None) -> None:
        self.execution = execution or Execution(exit_code=0)
        self.calls: list[tuple[str, RunCommandOpts]] = []
        self.stdout: list[str] = []

    async def run(
        self, command: str, *, opts: RunCommandOpts, handlers: ExecutionHandlers
    ) -> Execution:
        self.calls.append((command, opts))
        assert handlers.on_stdout is not None
        for text in self.stdout:
            await handlers.on_stdout(SimpleNamespace(text=text))  # type: ignore[arg-type]
        return self.execution


class FakeFiles:
    def __init__(self) -> None:
        self.contents: dict[str, bytes] = {}
        self.listing: list[SimpleNamespace] = []
        self.kinds: dict[str, str] = {}
        self.deleted_files: list[str] = []
        self.deleted_directories: list[str] = []
        self.error: SandboxException | None = None

    def _maybe_fail(self) -> None:
        if self.error is not None:
            raise self.error

    async def read_bytes(self, path: str) -> bytes:
        self._maybe_fail()
        return self.contents[path]

    async def write_file(self, path: str, content: bytes | IO[bytes]) -> None:
        self._maybe_fail()
        self.contents[path] = content if isinstance(content, bytes) else content.read()

    async def list_directory(self, entry: DirectoryListEntry) -> list[SimpleNamespace]:
        self._maybe_fail()
        assert entry.depth == 1
        return self.listing

    async def get_file_info(self, paths: list[str]) -> dict[str, SimpleNamespace]:
        self._maybe_fail()
        return {
            path: SimpleNamespace(entry_type=self.kinds[path])
            for path in paths
            if path in self.kinds
        }

    async def delete_files(self, paths: list[str]) -> None:
        self.deleted_files.extend(paths)

    async def delete_directories(self, paths: list[str]) -> None:
        self.deleted_directories.extend(paths)


class OpsHandle(FakeHandle):
    def __init__(self, sandbox_id: str = "osb-runtime-1") -> None:
        super().__init__(sandbox_id)
        self.commands = FakeCommands()
        self.files = FakeFiles()


def ops_runtime(handle: FakeHandle, *, settings: Settings | None = None) -> OpenSandboxRuntime:
    runtime = OpenSandboxRuntime(settings or Settings())
    runtime._sandboxes["sbx-test"] = handle  # type: ignore[assignment]
    return runtime


def sdk_api_error(status: int) -> SandboxApiException:
    return SandboxApiException(f"upstream answered {status}", status_code=status)


class TestBoundedOutput:
    @pytest.mark.asyncio
    async def test_a_result_shares_the_byte_budget_with_the_logs(self) -> None:
        output = _BoundedOutput(6)
        await output.on_stdout(SimpleNamespace(text="abcd"))  # type: ignore[arg-type]

        await output.on_result(
            SimpleNamespace(text="xyz", extra_properties={"image/png": "b64"})  # type: ignore[arg-type]
        )

        assert output.results[0].text == "xy"
        assert output.results[0].data == {"image/png": "b64"}
        assert (output.used, output.truncated) == (output.limit, True)

    @pytest.mark.asyncio
    async def test_a_result_with_no_text_keeps_its_data_and_spends_nothing(self) -> None:
        output = _BoundedOutput(6)

        await output.on_result(SimpleNamespace(text=None, extra_properties=None))  # type: ignore[arg-type]

        assert output.results[0].text is None
        assert output.results[0].data == {}
        assert output.used == 0
        assert output.truncated is False

    @pytest.mark.asyncio
    async def test_a_streamed_error_wins_over_the_executions_own(self) -> None:
        output = _BoundedOutput(100)
        await output.on_error(
            SimpleNamespace(name="ValueError", value="bad", traceback=["line 1"])  # type: ignore[arg-type]
        )
        execution = Execution(
            exit_code=1,
            error=SdkError(name="Other", value="ignored", timestamp=0, traceback=[]),
        )

        result = output.response(execution)

        assert result.error is not None
        assert result.error.name == "ValueError"
        assert result.error.traceback == ["line 1"]
        assert result.exit_code == 1

    def test_the_executions_error_is_used_when_nothing_was_streamed(self) -> None:
        output = _BoundedOutput(100)
        execution = Execution(
            exit_code=1,
            error=SdkError(name="KeyError", value="'x'", timestamp=0, traceback=["tb"]),
        )

        result = output.response(execution)

        assert result.error is not None
        assert (result.error.name, result.error.value) == ("KeyError", "'x'")
        assert result.error.traceback == ["tb"]

    def test_handlers_route_every_stream_and_skip_sdk_accumulation(self) -> None:
        output = _BoundedOutput(100)

        handlers = output.handlers()

        assert handlers.on_stdout == output.on_stdout
        assert handlers.on_stderr == output.on_stderr
        assert handlers.on_result == output.on_result
        assert handlers.on_error == output.on_error
        assert handlers.skip_accumulation is True


class TestHostMemory:
    @pytest.mark.asyncio
    async def test_a_configured_total_overrides_the_host(self) -> None:
        configured = 4096
        runtime = OpenSandboxRuntime(Settings(total_memory_mb=configured))

        assert await runtime.total_memory_mb() == configured
        await runtime.close()

    @pytest.mark.asyncio
    async def test_meminfo_is_read_in_megabytes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:  8388608 kB\nMemAvailable:  2097152 kB\n")
        monkeypatch.setattr(runtime_module, "Path", lambda _: meminfo)
        runtime = OpenSandboxRuntime(Settings())

        total, available = await runtime.total_memory_mb(), await runtime.available_memory_mb()
        assert (total, available) == (8192, 2048)
        await runtime.close()

    @pytest.mark.asyncio
    async def test_unreadable_meminfo_falls_back_to_sysconf(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(runtime_module, "Path", lambda _: tmp_path / "missing")
        sysconf = {"SC_PHYS_PAGES": 1024 * 1024, "SC_PAGE_SIZE": 4096}
        monkeypatch.setattr(runtime_module.os, "sysconf", sysconf.__getitem__)
        runtime = OpenSandboxRuntime(Settings())

        # No MemAvailable either, so available falls back to the same total.
        total, available = await runtime.total_memory_mb(), await runtime.available_memory_mb()
        assert (total, available) == (4096, 4096)
        await runtime.close()

    def test_a_missing_or_garbled_key_reads_as_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:  lots kB\n")
        monkeypatch.setattr(runtime_module, "Path", lambda _: meminfo)

        assert OpenSandboxRuntime._read_meminfo_mb("MemAvailable") == 0
        assert OpenSandboxRuntime._read_meminfo_mb("MemTotal") == 0


@pytest.mark.asyncio
async def test_start_and_reservation_are_the_warm_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = OpenSandboxRuntime(Settings())
    started: list[bool] = []
    reservation = WarmPoolReservation(memory_mb=512, cpu=1.0)

    async def start() -> None:
        started.append(True)

    monkeypatch.setattr(runtime._warm_pools, "start", start)
    monkeypatch.setattr(runtime._warm_pools, "reservation", lambda: reservation)

    await runtime.start()

    assert started == [True]
    assert runtime.warm_pool_reservation() is reservation
    await runtime.close()


class TestCommands:
    @pytest.mark.asyncio
    async def test_a_command_runs_with_its_options_and_returns_its_output(self) -> None:
        handle = OpsHandle()
        handle.commands.stdout = ["hello\n"]
        handle.commands.execution = Execution(exit_code=3)
        runtime = ops_runtime(handle)

        result = await runtime.execute_command(
            sandbox_record(),
            RuntimeCommandRequest(
                command="echo hello",
                timeout_seconds=7,
                max_output_bytes=1024,
                env={"A": "1"},
                cwd="/work",
            ),
        )

        command, opts = handle.commands.calls[0]
        assert command == "echo hello"
        assert opts.working_directory == "/work"
        assert opts.timeout == timedelta(seconds=7)
        assert opts.envs == {"A": "1"}
        assert (result.logs.stdout, result.exit_code) == (["hello\n"], 3)
        await runtime.close()

    @pytest.mark.asyncio
    async def test_a_process_is_quoted_and_stdin_is_piped_through_base64(self) -> None:
        handle = OpsHandle()
        runtime = ops_runtime(handle)

        await runtime.execute_process(
            sandbox_record(),
            RuntimeProcessRequest(
                executable="python3",
                args=["-c", "it's"],
                stdin="x y",
                timeout_seconds=5,
                max_output_bytes=64,
            ),
        )

        command, _ = handle.commands.calls[0]
        encoded = base64.b64encode(b"x y").decode("ascii")
        assert command == f"printf %s {encoded} | base64 -d | python3 -c 'it'\"'\"'s'"
        await runtime.close()

    @pytest.mark.asyncio
    async def test_a_process_without_stdin_runs_bare(self) -> None:
        handle = OpsHandle()
        runtime = ops_runtime(handle)

        await runtime.execute_process(
            sandbox_record(),
            RuntimeProcessRequest(
                executable="ls", args=["-la", "/my dir"], timeout_seconds=5, max_output_bytes=64
            ),
        )

        assert handle.commands.calls[0][0] == "ls -la '/my dir'"
        await runtime.close()

    @pytest.mark.asyncio
    async def test_an_sdk_failure_is_reported_as_unavailable(self) -> None:
        runtime = ops_runtime(FailingCommandHandle())

        with pytest.raises(SandboxUnavailableError, match=COMMAND_FAILURE_MESSAGE):
            await runtime.execute_command(
                sandbox_record(),
                RuntimeCommandRequest(command="true", timeout_seconds=5, max_output_bytes=64),
            )
        await runtime.close()


class TestFiles:
    @pytest.mark.asyncio
    async def test_text_reads_back_as_utf8_and_binary_as_base64(self) -> None:
        handle = OpsHandle()
        handle.files.contents = {"/a.txt": "héllo".encode(), "/b.bin": b"\xff\x00"}
        runtime = ops_runtime(handle)

        text = await runtime.read_file(sandbox_record(), "/a.txt")
        binary = await runtime.read_file(sandbox_record(), "/b.bin")

        assert (text.content, text.encoding) == ("héllo", "utf-8")
        assert (binary.content, binary.encoding) == ("/wA=", "base64")
        await runtime.close()

    @pytest.mark.asyncio
    async def test_writes_decode_base64_and_echo_the_request(self) -> None:
        handle = OpsHandle()
        runtime = ops_runtime(handle)

        utf8 = await runtime.write_file(
            sandbox_record(), FileWriteRequest(path="/a.txt", content="hi")
        )
        binary = await runtime.write_file(
            sandbox_record(),
            FileWriteRequest(path="/b.bin", content="/wA=", encoding="base64"),
        )

        assert handle.files.contents == {"/a.txt": b"hi", "/b.bin": b"\xff\x00"}
        assert (utf8.path, utf8.content, utf8.encoding) == ("/a.txt", "hi", "utf-8")
        assert binary.encoding == "base64"
        await runtime.close()

    @pytest.mark.asyncio
    async def test_a_streamed_upload_is_written_whole_and_sized(self) -> None:
        handle = OpsHandle()
        runtime = ops_runtime(handle)

        async def chunks() -> AsyncIterator[bytes]:
            yield b"abc"
            yield b"def"

        uploaded = await runtime.write_file_stream(sandbox_record(), "/up.bin", chunks())

        assert handle.files.contents["/up.bin"] == b"abcdef"
        assert (uploaded.path, uploaded.size) == ("/up.bin", 6)
        await runtime.close()

    @pytest.mark.asyncio
    async def test_an_upload_over_the_limit_is_refused_before_it_is_written(self) -> None:
        handle = OpsHandle()
        runtime = ops_runtime(handle, settings=Settings(max_upload_bytes=1024))

        async def chunks() -> AsyncIterator[bytes]:
            yield b"x" * 1000
            yield b"x" * 100

        with pytest.raises(SandboxUnavailableError, match="exceeds configured limit"):
            await runtime.write_file_stream(sandbox_record(), "/big.bin", chunks())
        assert handle.files.contents == {}
        await runtime.close()

    @pytest.mark.asyncio
    async def test_a_listing_names_entries_and_tells_directories_from_files(self) -> None:
        handle = OpsHandle()
        handle.files.listing = [
            SimpleNamespace(path="/work/src/", entry_type="DIR", size=0),
            SimpleNamespace(path="/work/main.py", entry_type="file", size=12),
            SimpleNamespace(path="/work/link", entry_type=None, size=None),
        ]
        runtime = ops_runtime(handle)

        listing = await runtime.list_files(sandbox_record(), "/work")

        assert [(e.name, e.type, e.size) for e in listing.entries] == [
            ("src", "directory", 0),
            ("main.py", "file", 12),
            ("link", "file", None),
        ]
        await runtime.close()

    @pytest.mark.asyncio
    async def test_remove_deletes_a_directory_as_a_directory(self) -> None:
        handle = OpsHandle()
        handle.files.kinds = {"/work/src": "directory", "/work/main.py": "file"}
        runtime = ops_runtime(handle)

        await runtime.remove_file(sandbox_record(), "/work/src")
        await runtime.remove_file(sandbox_record(), "/work/main.py")
        await runtime.remove_file(sandbox_record(), "/work/unknown")

        assert handle.files.deleted_directories == ["/work/src"]
        assert handle.files.deleted_files == ["/work/main.py", "/work/unknown"]
        await runtime.close()

    @pytest.mark.parametrize(
        "operation",
        ["read", "write", "stream", "list", "remove"],
    )
    @pytest.mark.asyncio
    async def test_every_file_operation_reports_an_sdk_failure_as_unavailable(
        self, operation: str
    ) -> None:
        handle = OpsHandle()
        handle.files.contents = {"/a.txt": b"hi"}
        handle.files.error = SandboxException("filesystem said no")
        runtime = ops_runtime(handle)
        sandbox = sandbox_record()

        async def one_chunk() -> AsyncIterator[bytes]:
            yield b"x"

        calls = {
            "read": lambda: runtime.read_file(sandbox, "/a.txt"),
            "write": lambda: runtime.write_file(
                sandbox, FileWriteRequest(path="/a.txt", content="x")
            ),
            "stream": lambda: runtime.write_file_stream(sandbox, "/a.txt", one_chunk()),
            "list": lambda: runtime.list_files(sandbox, "/"),
            "remove": lambda: runtime.remove_file(sandbox, "/a.txt"),
        }

        with pytest.raises(SandboxUnavailableError, match="filesystem said no"):
            await calls[operation]()
        await runtime.close()


class TestHandles:
    @pytest.mark.asyncio
    async def test_a_sandbox_with_no_runtime_id_cannot_be_reached(self) -> None:
        runtime = OpenSandboxRuntime(Settings())

        with pytest.raises(SandboxUnavailableError, match="no OpenSandbox runtime id"):
            await runtime.wait_until_ready(sandbox_record(container_id=None))
        await runtime.close()

    @pytest.mark.asyncio
    async def test_ready_connects_once_with_a_health_check_and_caches_the_handle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connects: list[dict[str, Any]] = []
        handle = OpsHandle("osb-1")

        class FakeOpenSandbox:
            @classmethod
            async def connect(cls, sandbox_id: str, **kwargs: Any) -> OpsHandle:  # noqa: ANN401
                connects.append({"id": sandbox_id, **kwargs})
                return handle

        monkeypatch.setattr(runtime_module, "OpenSandbox", FakeOpenSandbox)
        runtime = OpenSandboxRuntime(Settings())
        sandbox = sandbox_record(container_id="osb-1")

        await runtime.wait_until_ready(sandbox)
        await runtime.wait_until_ready(sandbox)

        assert len(connects) == 1
        assert connects[0]["id"] == "osb-1"
        assert connects[0]["skip_health_check"] is False
        assert runtime._sandboxes["sbx-test"] is handle
        await runtime.close()

    @pytest.mark.asyncio
    async def test_a_failed_connect_is_reported_as_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeOpenSandbox:
            @classmethod
            async def connect(cls, *_: object, **__: object) -> NoReturn:
                message = "connect refused"
                raise SandboxException(message)

        monkeypatch.setattr(runtime_module, "OpenSandbox", FakeOpenSandbox)
        runtime = OpenSandboxRuntime(Settings())
        manager = FakeManager()
        manager.sandbox_info = sandbox_info(state="running")
        runtime._manager = manager  # type: ignore[assignment]

        with pytest.raises(SandboxUnavailableError, match="connect refused"):
            await runtime.wait_until_ready(sandbox_record(container_id="osb-1"))
        assert "sbx-test" not in runtime._sandboxes
        await runtime.close()

    @pytest.mark.asyncio
    async def test_the_manager_is_created_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        created: list[object] = []
        manager = FakeManager()

        class FakeSandboxManager:
            @classmethod
            async def create(cls, connection: object) -> FakeManager:
                created.append(connection)
                return manager

        monkeypatch.setattr(runtime_module, "SandboxManager", FakeSandboxManager)
        runtime = OpenSandboxRuntime(Settings())

        first, second = await asyncio.gather(runtime._get_manager(), runtime._get_manager())

        assert first is second is manager
        assert created == [runtime._connection]
        await runtime.close()


class TestPauseAndResume:
    @pytest.mark.asyncio
    async def test_a_sandbox_that_never_started_has_nothing_to_pause(self) -> None:
        handle = OpsHandle()
        runtime = ops_runtime(handle)

        await runtime.pause(sandbox_record(container_id=None), memory=True)

        assert handle.paused is False
        assert runtime._sandboxes["sbx-test"] is handle
        await runtime.close()

    @pytest.mark.asyncio
    async def test_a_memory_pause_pauses_and_drops_the_handle(self) -> None:
        handle = OpsHandle()
        runtime = ops_runtime(handle)

        await runtime.pause(sandbox_record(container_id="osb-runtime-1"), memory=True)

        assert handle.paused is True
        assert handle.killed is False
        assert handle.closed is True
        assert "sbx-test" not in runtime._sandboxes
        await runtime.close()

    @pytest.mark.asyncio
    async def test_a_cold_pause_replaces_the_previous_snapshot(self) -> None:
        handle = OpsHandle()
        manager = FakeManager()
        runtime = ops_runtime(handle)
        runtime._manager = manager  # type: ignore[assignment]
        sandbox = sandbox_record(
            container_id="osb-runtime-1",
            metadata_={"template": "base", SNAPSHOT_METADATA_KEY: "snap-old"},
        )

        await runtime.pause(sandbox, memory=False)

        assert manager.deleted == ["snap-old"]
        assert sandbox.metadata_[SNAPSHOT_METADATA_KEY] == "snap-test"
        await runtime.close()

    @pytest.mark.asyncio
    async def test_a_snapshot_that_cannot_be_deleted_does_not_fail_the_pause(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class StuckManager(FakeManager):
            async def delete_snapshot(self, snapshot_id: str) -> None:  # noqa: ARG002
                raise sdk_api_error(HTTPStatus.INTERNAL_SERVER_ERROR)

        handle = OpsHandle()
        runtime = ops_runtime(handle)
        runtime._manager = StuckManager()  # type: ignore[assignment]
        sandbox = sandbox_record(
            container_id="osb-runtime-1",
            metadata_={SNAPSHOT_METADATA_KEY: "snap-old"},
        )

        with caplog.at_level(logging.WARNING, logger=runtime_module.__name__):
            await runtime.pause(sandbox, memory=False)

        assert sandbox.metadata_[SNAPSHOT_METADATA_KEY] == "snap-test"
        assert handle.killed is True
        assert "snap-old" in caplog.text
        await runtime.close()

    @pytest.mark.asyncio
    async def test_a_failed_pause_still_closes_and_drops_the_handle(self) -> None:
        class FailingPause(OpsHandle):
            async def pause(self) -> None:
                message = "pause refused"
                raise SandboxException(message)

        handle = FailingPause()
        runtime = ops_runtime(handle)
        # A pause needs a container id, so the OOM diagnostic will ask the
        # manager; answer it "not OOM" so the failure surfaces as unavailable.
        manager = FakeManager()
        manager.sandbox_info = sandbox_info(state="paused")
        runtime._manager = manager  # type: ignore[assignment]

        with pytest.raises(SandboxUnavailableError, match="pause refused"):
            await runtime.pause(sandbox_record(container_id="osb-runtime-1"), memory=True)
        assert handle.closed is True
        assert "sbx-test" not in runtime._sandboxes
        await runtime.close()

    @pytest.mark.asyncio
    async def test_resume_without_a_runtime_id_starts_fresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeOpenSandbox:
            @classmethod
            async def create(cls, *_: object, **__: object) -> FakeHandle:
                return FakeHandle("osb-fresh")

        monkeypatch.setattr(runtime_module, "OpenSandbox", FakeOpenSandbox)
        runtime = OpenSandboxRuntime(Settings())

        started = await runtime.resume(sandbox_record(container_id=None))

        assert started.id == "osb-fresh"
        await runtime.close()

    @pytest.mark.asyncio
    async def test_resume_reattaches_the_paused_sandbox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resumed: list[str] = []

        class FakeOpenSandbox:
            @classmethod
            async def resume(cls, sandbox_id: str, **_: object) -> FakeHandle:
                resumed.append(sandbox_id)
                return FakeHandle(sandbox_id)

        monkeypatch.setattr(runtime_module, "OpenSandbox", FakeOpenSandbox)
        runtime = OpenSandboxRuntime(Settings())

        started = await runtime.resume(sandbox_record(container_id="osb-paused"))

        assert resumed == ["osb-paused"]
        assert (started.id, started.name) == ("osb-paused", "osb-paused")
        assert runtime._sandboxes["sbx-test"].id == "osb-paused"
        await runtime.close()

    @pytest.mark.asyncio
    async def test_a_resume_that_times_out_is_retryable_not_dead(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeOpenSandbox:
            @classmethod
            async def resume(cls, *_: object, **__: object) -> NoReturn:
                message = "not ready in time"
                raise SandboxReadyTimeoutException(message)

        monkeypatch.setattr(runtime_module, "OpenSandbox", FakeOpenSandbox)
        runtime = OpenSandboxRuntime(Settings())

        with pytest.raises(SandboxStartTimeoutError):
            await runtime.resume(sandbox_record(container_id="osb-paused"))
        await runtime.close()


class TestKill:
    @pytest.mark.asyncio
    async def test_an_uncached_sandbox_is_connected_to_and_killed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handle = FakeHandle("osb-1")

        class FakeOpenSandbox:
            @classmethod
            async def connect(cls, sandbox_id: str, **kwargs: Any) -> FakeHandle:  # noqa: ANN401
                assert sandbox_id == "osb-1"
                assert kwargs["skip_health_check"] is True
                return handle

        monkeypatch.setattr(runtime_module, "OpenSandbox", FakeOpenSandbox)
        runtime = OpenSandboxRuntime(Settings())

        await runtime.kill(sandbox_record(container_id="osb-1"))

        assert handle.killed is True
        assert handle.closed is True
        await runtime.close()

    @pytest.mark.asyncio
    async def test_a_sandbox_already_gone_upstream_is_a_successful_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeOpenSandbox:
            @classmethod
            async def connect(cls, *_: object, **__: object) -> NoReturn:
                raise sdk_api_error(HTTPStatus.NOT_FOUND)

        monkeypatch.setattr(runtime_module, "OpenSandbox", FakeOpenSandbox)
        runtime = OpenSandboxRuntime(Settings())

        await runtime.kill(sandbox_record(container_id="osb-gone"))
        await runtime.close()

    @pytest.mark.asyncio
    async def test_a_connect_that_fails_otherwise_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeOpenSandbox:
            @classmethod
            async def connect(cls, *_: object, **__: object) -> NoReturn:
                raise sdk_api_error(HTTPStatus.BAD_GATEWAY)

        monkeypatch.setattr(runtime_module, "OpenSandbox", FakeOpenSandbox)
        runtime = OpenSandboxRuntime(Settings())
        manager = FakeManager()
        manager.sandbox_info = sandbox_info(state="running")
        runtime._manager = manager  # type: ignore[assignment]

        with pytest.raises(SandboxUnavailableError, match="502"):
            await runtime.kill(sandbox_record(container_id="osb-1"))
        await runtime.close()

    @pytest.mark.parametrize(
        ("status", "raises"),
        [(HTTPStatus.NOT_FOUND, False), (HTTPStatus.INTERNAL_SERVER_ERROR, True)],
    )
    @pytest.mark.asyncio
    async def test_a_kill_the_upstream_refuses_is_reported_unless_already_gone(
        self, status: HTTPStatus, *, raises: bool
    ) -> None:
        class RefusingKill(FakeHandle):
            async def kill(self) -> None:
                raise sdk_api_error(status)

        handle = RefusingKill()
        runtime = ops_runtime(handle)

        if raises:
            with pytest.raises(SandboxUnavailableError):
                await runtime.kill(sandbox_record(container_id=None))
        else:
            await runtime.kill(sandbox_record(container_id=None))
        assert handle.closed is True
        await runtime.close()


class TestContainerStatus:
    @pytest.mark.asyncio
    async def test_a_sandbox_with_no_runtime_id_has_no_status(self) -> None:
        runtime = OpenSandboxRuntime(Settings())

        assert await runtime.container_status(sandbox_record(container_id=None)) is None
        await runtime.close()

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("Running", "running"),
            ("Pending", "created"),
            ("Pausing", "paused"),
            ("Paused", "paused"),
            ("Resuming", "created"),
            ("Stopping", "exited"),
            ("Terminated", "exited"),
            ("Failed", "dead"),
            ("Migrating", "migrating"),
        ],
    )
    @pytest.mark.asyncio
    async def test_upstream_states_map_onto_harborbox_states(
        self, state: str, expected: str
    ) -> None:
        manager = FakeManager()
        manager.sandbox_info = sandbox_info(state=state)
        runtime = OpenSandboxRuntime(Settings())
        runtime._manager = manager  # type: ignore[assignment]

        assert await runtime.container_status(sandbox_record(container_id="osb-1")) == expected
        await runtime.close()

    @pytest.mark.asyncio
    async def test_a_sandbox_the_upstream_no_longer_knows_has_no_status(self) -> None:
        manager = FakeManager()
        manager.sandbox_info_error = sdk_api_error(HTTPStatus.NOT_FOUND)
        runtime = OpenSandboxRuntime(Settings())
        runtime._manager = manager  # type: ignore[assignment]

        assert await runtime.container_status(sandbox_record(container_id="osb-1")) is None
        await runtime.close()

    @pytest.mark.asyncio
    async def test_any_other_lookup_failure_is_reported(self) -> None:
        manager = FakeManager()
        manager.sandbox_info_error = sdk_api_error(HTTPStatus.SERVICE_UNAVAILABLE)
        runtime = OpenSandboxRuntime(Settings())
        runtime._manager = manager  # type: ignore[assignment]

        with pytest.raises(SandboxUnavailableError, match="503"):
            await runtime.container_status(sandbox_record(container_id="osb-1"))
        await runtime.close()


class TestSnapshots:
    @staticmethod
    def manager_with_states(*states: tuple[str, str | None]) -> FakeManager:
        remaining = list(states)

        class SequencedManager(FakeManager):
            async def get_snapshot(self, snapshot_id: str) -> SimpleNamespace:  # noqa: ARG002
                state, message = remaining.pop(0) if len(remaining) > 1 else remaining[0]
                return SimpleNamespace(status=SimpleNamespace(state=state, message=message))

        return SequencedManager()

    @pytest.mark.asyncio
    async def test_a_snapshot_still_creating_is_polled_until_ready(self) -> None:
        runtime = OpenSandboxRuntime(Settings())
        runtime._manager = self.manager_with_states(  # type: ignore[assignment]
            ("Creating", None), ("Ready", None)
        )

        await runtime._wait_snapshot_ready("snap-1")
        await runtime.close()

    @pytest.mark.parametrize(
        ("message", "expected"),
        [("disk full", "disk full"), (None, "OpenSandbox snapshot failed")],
    )
    @pytest.mark.asyncio
    async def test_a_failed_snapshot_says_why(self, message: str | None, expected: str) -> None:
        runtime = OpenSandboxRuntime(Settings())
        runtime._manager = self.manager_with_states(("Failed", message))  # type: ignore[assignment]

        with pytest.raises(SandboxUnavailableError, match=expected):
            await runtime._wait_snapshot_ready("snap-1")
        await runtime.close()

    @pytest.mark.asyncio
    async def test_a_snapshot_that_never_settles_times_out(self) -> None:
        runtime = OpenSandboxRuntime(Settings(opensandbox_snapshot_timeout_seconds=0.05))
        runtime._manager = self.manager_with_states(("Creating", None))  # type: ignore[assignment]

        with pytest.raises(SandboxUnavailableError, match="did not become ready"):
            await runtime._wait_snapshot_ready("snap-1")
        await runtime.close()

    @pytest.mark.asyncio
    async def test_a_missing_snapshot_is_only_ignored_when_asked(self) -> None:
        class GoneManager(FakeManager):
            async def delete_snapshot(self, snapshot_id: str) -> None:  # noqa: ARG002
                raise sdk_api_error(HTTPStatus.NOT_FOUND)

        runtime = OpenSandboxRuntime(Settings())
        runtime._manager = GoneManager()  # type: ignore[assignment]

        await runtime._delete_snapshot("snap-1", ignore_missing=True)
        with pytest.raises(SandboxUnavailableError, match="404"):
            await runtime._delete_snapshot("snap-1")
        await runtime.close()
