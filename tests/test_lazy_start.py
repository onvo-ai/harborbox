"""Lazy-start plumbing for failures #2/#3/#4 (task 21).

Nothing but `create_execution`/`create_command`/`create_process` used to ever
start a sandbox: file I/O and the idle-timeout PATCH hard-required `running`
and 409'd otherwise, even though a freshly created sandbox is *supposed* to
start lazily (see `reaper_stuck_created_after_seconds`'s own docstring). These
tests cover the fix at both layers -- `Scheduler.ensure_sandbox_ready`'s
single-start guarantee and bounded wait, and the API's `ensure_ready` helper
that calls it -- without a live stack.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from harborbox import scheduler as scheduler_module
from harborbox.api import app
from harborbox.config import Settings
from harborbox.db import Base, get_session
from harborbox.models import Sandbox
from harborbox.runtime import SandboxUnavailableError
from harborbox.runtime_protocol import StartedSandbox, WarmPoolReservation
from harborbox.schemas import (
    FileListResponse,
    FileReadResponse,
    FileUploadResponse,
    FileWriteRequest,
)
from harborbox.security import require_api_key

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

Sessions = async_sessionmaker[AsyncSession]


def sandbox_row(**overrides: Any) -> Sandbox:  # noqa: ANN401
    values: dict[str, Any] = {
        "id": "sbx-lazy",
        "status": "created",
        "container_id": None,
        "container_name": None,
        "agent_token": "test-token",
        "memory_mb": 256,
        "cpu": 0.5,
        "pids_limit": 128,
        "idle_timeout_seconds": 300,
        "metadata_": {"template": "relaydeck"},
    }
    values.update(overrides)
    return Sandbox(**values)


class FakeRuntime:
    """Stands in for a runtime backend; records what lifecycle calls it took.

    Only implements the subset of `SandboxRuntime` exercised by lazy start
    and the file endpoints -- execution and pause/kill are not touched by
    any of these tests.
    """

    def __init__(
        self,
        *,
        total_memory_mb: int = 8192,
        start_delay: float = 0.0,
        start_error: Exception | None = None,
    ) -> None:
        self._total_memory_mb = total_memory_mb
        self.start_delay = start_delay
        self.start_error = start_error
        self.start_calls: list[str] = []
        self.ready_calls: list[str] = []

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def warm_pool_reservation(self) -> WarmPoolReservation:
        return WarmPoolReservation()

    async def total_memory_mb(self) -> int:
        return self._total_memory_mb

    async def available_memory_mb(self) -> int:
        return self._total_memory_mb

    async def start_sandbox(self, sandbox: Sandbox) -> StartedSandbox:
        self.start_calls.append(sandbox.id)
        # Delay before the error, not after: a runtime that is merely slow
        # and then fails (the CRITICAL scenario) needs the delay to run
        # first so a caller's timeout can elapse before the failure does.
        if self.start_delay:
            await asyncio.sleep(self.start_delay)
        if self.start_error is not None:
            raise self.start_error
        return StartedSandbox(id=f"c-{sandbox.id}", name=f"c-{sandbox.id}")

    async def wait_until_ready(self, sandbox: Sandbox) -> None:
        self.ready_calls.append(sandbox.id)

    async def resume(self, sandbox: Sandbox) -> StartedSandbox:
        return await self.start_sandbox(sandbox)

    async def kill(self, _sandbox: Sandbox) -> None:
        return None

    async def container_status(self, _sandbox: Sandbox) -> str | None:
        # A start that raised never persisted a container_id, so a real
        # runtime's own status lookup would find nothing either.
        return None if self.start_error is not None else "running"

    async def read_file(self, _sandbox: Sandbox, path: str) -> FileReadResponse:
        return FileReadResponse(path=path, content="hi", encoding="utf-8")

    async def write_file(
        self, _sandbox: Sandbox, request: FileWriteRequest
    ) -> FileReadResponse:
        return FileReadResponse(
            path=request.path, content=request.content, encoding=request.encoding
        )

    async def write_file_stream(
        self, _sandbox: Sandbox, path: str, content: Any  # noqa: ANN401
    ) -> FileUploadResponse:
        size = 0
        async for chunk in content:
            size += len(chunk)
        return FileUploadResponse(path=path, size=size)

    async def list_files(self, _sandbox: Sandbox, path: str) -> FileListResponse:
        return FileListResponse(path=path, entries=[])

    async def remove_file(self, _sandbox: Sandbox, _path: str) -> None:
        return None


# --- pure decision logic -----------------------------------------------------


class TestLazyStartAction:
    def test_running_needs_no_start(self) -> None:
        assert scheduler_module.lazy_start_action("running") == "ready"

    def test_dead_sandboxes_are_unavailable(self) -> None:
        assert scheduler_module.lazy_start_action("killed") == "unavailable"
        assert scheduler_module.lazy_start_action("failed") == "unavailable"

    def test_warm_pool_rows_are_unavailable(self) -> None:
        """Regression test: these used to fall into `"start"`.

        `_ensure_running_locked` has no branch for `pooled`/`pooling` and
        would raise, and with the capacity-leak fix that failure now marks
        the sandbox `failed` -- destroying a warm-pool row a request has no
        business touching at all.
        """
        assert scheduler_module.lazy_start_action("pooled") == "unavailable"
        assert scheduler_module.lazy_start_action("pooling") == "unavailable"

    def test_every_other_status_lazily_starts(self) -> None:
        for status in ("created", "starting", "paused_cold", "paused_memory"):
            assert scheduler_module.lazy_start_action(status) == "start"


# --- Scheduler.ensure_sandbox_ready ------------------------------------------


@pytest.fixture
async def sessions() -> AsyncIterator[Sessions]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_ensure_sandbox_ready_starts_a_created_sandbox_once_for_two_callers(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two callers racing a `created` sandbox produce exactly one start call.

    Regardless of whether the two callers are both file requests, or one file
    request and one queued execution admitted at the same moment -- both
    route through this same per-sandbox lock in `_ensure_running`.
    """
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-1", status="created"))
        await session.commit()
    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    runtime = FakeRuntime()
    scheduler = scheduler_module.Scheduler(Settings(), runtime)

    await asyncio.gather(
        scheduler.ensure_sandbox_ready("sbx-1", timeout_seconds=5),
        scheduler.ensure_sandbox_ready("sbx-1", timeout_seconds=5),
    )

    assert runtime.start_calls == ["sbx-1"]
    async with sessions() as session:
        sandbox = await session.get(Sandbox, "sbx-1")
    assert sandbox is not None
    assert sandbox.status == "running"


async def test_ensure_sandbox_ready_is_a_noop_for_an_already_running_sandbox(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with sessions() as session:
        session.add(
            sandbox_row(
                id="sbx-2", status="running", container_id="c-2", container_name="c-2"
            )
        )
        await session.commit()
    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    runtime = FakeRuntime()
    scheduler = scheduler_module.Scheduler(Settings(), runtime)

    await scheduler.ensure_sandbox_ready("sbx-2", timeout_seconds=5)

    assert runtime.start_calls == []
    assert runtime.ready_calls == ["sbx-2"]


async def test_ensure_sandbox_ready_bounds_the_wait_without_aborting_the_start(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow start must not hang the caller, and must not be aborted either.

    Cancelling `start_sandbox` partway through would strand a container the
    sandbox row never learns about. The caller gets a bounded, clear error;
    the start itself keeps running in the background and finishes normally.
    """
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-3", status="created"))
        await session.commit()
    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    runtime = FakeRuntime(start_delay=0.2)
    scheduler = scheduler_module.Scheduler(Settings(), runtime)

    with pytest.raises(scheduler_module.SandboxStartTimeoutError):
        await scheduler.ensure_sandbox_ready("sbx-3", timeout_seconds=0.02)

    # Not aborted: still tracked and running.
    pending = scheduler._pending_starts.get("sbx-3")
    assert pending is not None
    await pending

    assert runtime.start_calls == ["sbx-3"]
    async with sessions() as session:
        sandbox = await session.get(Sandbox, "sbx-3")
    assert sandbox is not None
    assert sandbox.status == "running"


async def test_ensure_sandbox_ready_propagates_a_dead_sandbox(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-4", status="killed"))
        await session.commit()
    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    scheduler = scheduler_module.Scheduler(Settings(), FakeRuntime())

    with pytest.raises(scheduler_module.SandboxUnavailableError):
        await scheduler.ensure_sandbox_ready("sbx-4", timeout_seconds=5)


async def test_a_background_start_failure_after_timeout_is_still_recorded(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The critical gap: timeout now, background failure later.

    A caller that already gave up with `SandboxStartTimeoutError` is not
    around to notice the shielded background task then errors --
    `asyncio.shield`'s own machinery does not surface that to anyone by
    itself. `_ensure_running` must record the failure from inside itself, or
    the sandbox stays `starting` -- reserving capacity -- forever. This is
    the scenario `test_a_failed_lazy_start_reports_503_and_does_not_strand_starting`
    does not cover: that one fails synchronously, before any timeout.
    """
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-5", status="created"))
        await session.commit()
    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    runtime = FakeRuntime(
        start_delay=0.05, start_error=SandboxUnavailableError("opensandbox boom")
    )
    scheduler = scheduler_module.Scheduler(Settings(), runtime)

    with pytest.raises(scheduler_module.SandboxStartTimeoutError):
        await scheduler.ensure_sandbox_ready("sbx-5", timeout_seconds=0.01)

    # Confirm the background task is the one that later fails -- i.e. this
    # genuinely exercises timeout-then-background-failure, not just a
    # not-yet-finished task.
    pending = scheduler._pending_starts.get("sbx-5")
    assert pending is not None
    with pytest.raises(SandboxUnavailableError):
        await pending

    async with sessions() as session:
        sandbox = await session.get(Sandbox, "sbx-5")
    assert sandbox is not None
    assert sandbox.status == "failed"


# --- API layer: ensure_ready wired into the file/status endpoints -----------


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.fixture
async def client(
    sessions: Sessions, runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    settings = Settings(lazy_start_wait_timeout_seconds=5)
    app.state.settings = settings
    app.state.runtime = runtime
    app.state.scheduler = scheduler_module.Scheduler(settings, runtime)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as opened:
            yield opened

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[require_api_key] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://harborbox"
    ) as opened:
        yield opened
    app.dependency_overrides.clear()


async def test_uploading_a_file_lazily_starts_a_created_sandbox(
    client: httpx.AsyncClient, sessions: Sessions, runtime: FakeRuntime
) -> None:
    """File I/O on a brand-new sandbox used to 409 unconditionally.

    Regression test for failures #2/#3: nothing had ever started it.
    """
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-file", status="created"))
        await session.commit()

    response = await client.put(
        "/v1/sandboxes/sbx-file/files/content",
        params={"path": "/tmp/onvo.bin"},  # noqa: S108
        content=b"payload",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == httpx.codes.OK
    assert response.json() == {"path": "/tmp/onvo.bin", "size": len(b"payload")}  # noqa: S108
    assert runtime.start_calls == ["sbx-file"]
    async with sessions() as session:
        sandbox = await session.get(Sandbox, "sbx-file")
    assert sandbox is not None
    assert sandbox.status == "running"


async def test_configuring_idle_timeout_lazily_starts_a_created_sandbox(
    client: httpx.AsyncClient, runtime: FakeRuntime, sessions: Sessions
) -> None:
    """`set_timeout` on a brand-new sandbox used to leave it stuck in `created`.

    Regression test for failure #4: nothing else about that PATCH ever
    started it, so the SDK's `refresh().status` never reported `running`.
    """
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-patch", status="created"))
        await session.commit()

    new_idle_timeout_seconds = 180
    response = await client.patch(
        "/v1/sandboxes/sbx-patch",
        json={"idle_timeout_seconds": new_idle_timeout_seconds},
    )

    assert response.status_code == httpx.codes.OK
    assert response.json()["status"] == "running"
    assert response.json()["idle_timeout_seconds"] == new_idle_timeout_seconds
    assert runtime.start_calls == ["sbx-patch"]


@pytest.mark.parametrize("paused_status", ["paused_cold", "paused_memory"])
async def test_configuring_idle_timeout_does_not_revive_a_paused_sandbox(
    client: httpx.AsyncClient,
    runtime: FakeRuntime,
    sessions: Sessions,
    paused_status: str,
) -> None:
    """A metadata PATCH must not have the side effect of waking a sandbox.

    Unlike a never-started sandbox (the case above), one the caller
    explicitly paused stays paused: reviving it and restarting idle
    accounting is not something a `set_timeout` call should ever trigger.
    """
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-paused", status=paused_status))
        await session.commit()

    new_idle_timeout_seconds = 180
    response = await client.patch(
        "/v1/sandboxes/sbx-paused",
        json={"idle_timeout_seconds": new_idle_timeout_seconds},
    )

    assert response.status_code == httpx.codes.OK
    assert response.json()["status"] == paused_status
    assert response.json()["idle_timeout_seconds"] == new_idle_timeout_seconds
    assert runtime.start_calls == []


async def test_file_endpoints_still_refuse_a_dead_sandbox(
    client: httpx.AsyncClient, runtime: FakeRuntime, sessions: Sessions
) -> None:
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-dead", status="killed"))
        await session.commit()

    response = await client.get(
        "/v1/sandboxes/sbx-dead/files", params={"path": "/tmp/x"}  # noqa: S108
    )

    assert response.status_code == httpx.codes.CONFLICT
    assert runtime.start_calls == []


async def test_lazy_start_reports_insufficient_capacity_as_a_429(
    client: httpx.AsyncClient, runtime: FakeRuntime, sessions: Sessions
) -> None:
    # `Settings()`'s defaults reserve more than this tiny fake host has, so
    # any sandbox's incremental memory is instantly over budget.
    runtime._total_memory_mb = 256
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-tight", status="created", memory_mb=256))
        await session.commit()

    response = await client.get(
        "/v1/sandboxes/sbx-tight/files/list", params={"path": "."}
    )

    assert response.status_code == httpx.codes.TOO_MANY_REQUESTS
    assert response.json()["detail"] == "insufficient memory capacity"
    assert runtime.start_calls == []


async def test_a_failed_lazy_start_reports_503_and_does_not_strand_starting(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start failure must not strand the sandbox row in `starting` forever.

    Nothing else would ever come along to notice: `ensure_ready` marks it
    `failed`, the same way `resume_sandbox` already does for the identical
    exception.
    """
    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    runtime = FakeRuntime(start_error=SandboxUnavailableError("upstream boom"))
    settings = Settings(lazy_start_wait_timeout_seconds=5)
    app.state.settings = settings
    app.state.runtime = runtime
    app.state.scheduler = scheduler_module.Scheduler(settings, runtime)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as opened:
            yield opened

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[require_api_key] = lambda: None
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-broken", status="created"))
        await session.commit()

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://harborbox"
        ) as client:
            response = await client.get(
                "/v1/sandboxes/sbx-broken/files/list", params={"path": "."}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == httpx.codes.SERVICE_UNAVAILABLE
    async with sessions() as session:
        sandbox = await session.get(Sandbox, "sbx-broken")
    assert sandbox is not None
    assert sandbox.status == "failed"
