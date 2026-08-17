from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from opensandbox.exceptions import SandboxException

import harborbox.opensandbox_runtime as runtime_module
from harborbox.config import Settings
from harborbox.errors import SandboxMemoryExceededError, SandboxUnavailableError
from harborbox.models import Sandbox
from harborbox.opensandbox_runtime import (
    SNAPSHOT_METADATA_KEY,
    OpenSandboxRuntime,
    _BoundedOutput,
)


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
        "metadata_": {"template": "onvo-pro", "team": "test"},
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
    assert captured["args"] == ("harborbox-sandbox-onvo-pro:local",)
    assert captured["kwargs"]["resource"] == {
        "cpu": "1.5",
        "memory": "768Mi",
    }
    assert captured["kwargs"]["metadata"]["harborbox.sandbox_id"] == "sbx-test"
    assert captured["kwargs"]["timeout"] is None
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
