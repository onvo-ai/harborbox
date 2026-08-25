"""A start that is merely slow must stay retryable, not be recorded dead (DEV-2032).

DEV-1996 gave the lazy-start 503 two machine-readable meanings --
`SANDBOX_STARTING` (retry, the start is still going) and `SANDBOX_START_FAILED`
(give up, the row is `failed`). This is the layer underneath: which of those two
a *slow* start actually produced.

It produced the wrong one. `opensandbox_ready_timeout_seconds` (30s) bounded
both the create POST and the readiness wait, and cold starts were measured at
20-31s under concurrency -- inside that bound. When it tripped, the SDK's
timeout arrived as a plain `SandboxException`, `_raise_start_error`'s
predecessor turned every one of those into `SandboxUnavailableError`, and both
`_ensure_running` and `ensure_ready` wrote `failed`. A user whose sandbox was
slow to start did not get a slow sandbox, they got a dead one.

It also made the retryable branch nearly unreachable: the inner 30s always
pre-empted the outer 60s, so `SANDBOX_STARTING` could only fire when
`wait_until_ready` was slow, never when `create` was.

Two fixes, and these tests hold both:

* `sandbox_start_timeout_seconds` is the start's own budget, separate from the
  per-request bound, and a validator refuses to let the caller's wait pre-empt
  it -- the same "these two must not cross" shape as
  `test_live_client_retry.py`'s client/server pair.
* A timeout on the start path raises `SandboxStartTimeoutError`, which nothing
  treats as a dead sandbox.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from opensandbox.exceptions import (
    SandboxException,
    SandboxInternalException,
    SandboxReadyTimeoutException,
)
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import harborbox.opensandbox_runtime as runtime_module
from harborbox import scheduler as scheduler_module
from harborbox.api import SANDBOX_STARTING_CODE, app
from harborbox.config import Settings
from harborbox.db import Base, get_session
from harborbox.errors import SandboxStartTimeoutError, SandboxUnavailableError
from harborbox.models import Execution, Sandbox
from harborbox.opensandbox_runtime import OpenSandboxRuntime
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

CREATE_TIMED_OUT = "Sandbox health check timed out after 30.0s (140 attempts)"
CONTROL_PLANE_BOOM = "opensandbox refused the create"


def sandbox_row(**overrides: Any) -> Sandbox:  # noqa: ANN401
    values: dict[str, Any] = {
        "id": "sbx-slow",
        "status": "created",
        "container_id": None,
        "container_name": None,
        "agent_token": "test-token",
        "memory_mb": 256,
        "cpu": 0.5,
        "pids_limit": 128,
        "idle_timeout_seconds": 300,
        "metadata_": {"template": "base"},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "last_activity_at": datetime.now(UTC),
    }
    values.update(overrides)
    return Sandbox(**values)


# --- the two timeouts must not cross -----------------------------------------


def test_the_start_budget_stays_above_the_callers_wait() -> None:
    """The inner bound must not be able to pre-empt the outer one.

    `lazy_start_wait_timeout_seconds` bounds how long the *caller* waits; the
    start continues in the background either way. The start's own budget has to
    outlast it, or the start dies first and the caller is told the sandbox
    failed rather than that it is still coming up -- which is exactly how the
    `SANDBOX_STARTING` branch became unreachable for a slow create.

    Read as one assertion with two halves, the same shape as
    `test_the_client_timeout_stays_above_the_server_start_budget`: change
    either default without the other and this fails, which is the point.
    """
    settings = Settings()
    assert settings.sandbox_start_timeout_seconds > settings.lazy_start_wait_timeout_seconds


def test_settings_refuse_a_start_budget_the_callers_wait_can_pre_empt() -> None:
    """The crossing must be a startup error, not a silent misconfiguration.

    The pre-DEV-2032 numbers -- a 30s start budget under a 60s caller wait --
    are the exact pair this refuses, so the shipped default can never quietly
    drift back to it through the environment.
    """
    with pytest.raises(ValidationError) as caught:
        Settings(
            sandbox_start_timeout_seconds=30.0,
            lazy_start_wait_timeout_seconds=60.0,
        )

    message = str(caught.value)
    assert "sandbox_start_timeout_seconds" in message
    assert "lazy_start_wait_timeout_seconds" in message


def test_the_per_request_bound_is_no_longer_the_start_budget() -> None:
    """A long start budget must not become a long bound on every other call.

    `opensandbox_ready_timeout_seconds` is `ConnectionConfig.request_timeout`
    for every control-plane call the runtime makes, and the codebase already
    pays for that being short (see `oom_diagnostic_timeout_seconds`). Sizing
    the start against a real cold start therefore had to be a *separate* knob,
    not a raise of this one.
    """
    settings = Settings()
    assert settings.opensandbox_ready_timeout_seconds < settings.sandbox_start_timeout_seconds


# --- the runtime: slow is not dead -------------------------------------------


def fake_opensandbox_raising(error: Exception) -> type:
    class FakeOpenSandbox:
        @classmethod
        async def create(cls, *_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
            raise error

        @classmethod
        async def resume(cls, *_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
            raise error

    return FakeOpenSandbox


@pytest.mark.asyncio
async def test_a_create_that_times_out_waiting_for_ready_is_not_an_unavailable_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`SandboxReadyTimeoutException` is the SDK saying "slow", not "broken".

    The SDK kills its own zombie sandbox before raising this, so there is
    nothing stranded upstream and a retry is safe.
    """
    monkeypatch.setattr(
        runtime_module,
        "OpenSandbox",
        fake_opensandbox_raising(SandboxReadyTimeoutException(CREATE_TIMED_OUT)),
    )
    runtime = OpenSandboxRuntime(Settings())

    with pytest.raises(SandboxStartTimeoutError):
        await runtime.start_sandbox(sandbox_row())
    await runtime.close()


@pytest.mark.asyncio
async def test_a_read_timeout_against_the_control_plane_is_also_a_start_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of a slow create, and the one the SDK does not name.

    `ConnectionConfig.request_timeout` bounds the create POST itself. When it
    trips, httpx raises `ReadTimeout` and the SDK's converter wraps it as a
    generic `SandboxInternalException` -- indistinguishable from a real
    failure by class alone. The cause chain is what tells them apart.
    """
    wrapped = SandboxInternalException(
        "Network connectivity error: timed out",
        cause=httpx.ReadTimeout("timed out"),
    )
    monkeypatch.setattr(runtime_module, "OpenSandbox", fake_opensandbox_raising(wrapped))
    runtime = OpenSandboxRuntime(Settings())

    with pytest.raises(SandboxStartTimeoutError):
        await runtime.start_sandbox(sandbox_row())
    await runtime.close()


@pytest.mark.asyncio
async def test_a_create_that_genuinely_fails_is_still_an_unavailable_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The classification must be narrow: only a timeout earns the retry.

    A control plane that refuses the create is a dead sandbox, and treating it
    as retryable would spin a caller against an error that will never clear.
    """
    monkeypatch.setattr(
        runtime_module,
        "OpenSandbox",
        fake_opensandbox_raising(SandboxException(CONTROL_PLANE_BOOM)),
    )
    runtime = OpenSandboxRuntime(Settings())

    with pytest.raises(SandboxUnavailableError):
        await runtime.start_sandbox(sandbox_row())
    await runtime.close()


@pytest.mark.asyncio
async def test_a_resume_that_times_out_is_a_start_timeout_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`paused_cold` -> `running` is the same wait, on the same budget."""
    monkeypatch.setattr(
        runtime_module,
        "OpenSandbox",
        fake_opensandbox_raising(SandboxReadyTimeoutException(CREATE_TIMED_OUT)),
    )
    runtime = OpenSandboxRuntime(Settings())

    with pytest.raises(SandboxStartTimeoutError):
        await runtime.resume(sandbox_row(container_id="c-1", status="paused_cold"))
    await runtime.close()


# --- the scheduler: a timed-out start leaves a row someone can retry ---------


class SlowThenReadyRuntime:
    """Times out on the first start, succeeds on the next.

    Models the real thing: the container is coming up, the caller's SDK gave
    up on it, and the retry finds a box that is now warm.
    """

    def __init__(self, *, failures: int = 1) -> None:
        self.remaining_failures = failures
        self.start_calls: list[str] = []
        self.ready_calls: list[str] = []

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def warm_pool_reservation(self) -> WarmPoolReservation:
        return WarmPoolReservation()

    async def total_memory_mb(self) -> int:
        return 8192

    async def available_memory_mb(self) -> int:
        return 8192

    async def start_sandbox(self, sandbox: Sandbox) -> StartedSandbox:
        self.start_calls.append(sandbox.id)
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise SandboxStartTimeoutError(CREATE_TIMED_OUT)
        return StartedSandbox(id=f"c-{sandbox.id}", name=f"c-{sandbox.id}")

    async def wait_until_ready(self, sandbox: Sandbox) -> None:
        self.ready_calls.append(sandbox.id)

    async def resume(self, sandbox: Sandbox) -> StartedSandbox:
        return await self.start_sandbox(sandbox)

    async def kill(self, _sandbox: Sandbox) -> None:
        return None

    async def container_status(self, _sandbox: Sandbox) -> str | None:
        # A start that timed out never persisted a container_id, so the real
        # runtime's own lookup finds nothing either -- which is precisely why
        # `_mark_start_failed` used to write `failed` here.
        return None

    async def read_file(self, _sandbox: Sandbox, path: str) -> FileReadResponse:
        return FileReadResponse(path=path, content="hi", encoding="utf-8")

    async def write_file(
        self, _sandbox: Sandbox, request: FileWriteRequest
    ) -> FileReadResponse:
        return FileReadResponse(
            path=request.path, content=request.content, encoding=request.encoding
        )

    async def write_file_stream(
        self,
        _sandbox: Sandbox,
        path: str,
        content: Any,  # noqa: ANN401
    ) -> FileUploadResponse:
        size = 0
        async for chunk in content:
            size += len(chunk)
        return FileUploadResponse(path=path, size=size)

    async def list_files(self, _sandbox: Sandbox, path: str) -> FileListResponse:
        return FileListResponse(path=path, entries=[])

    async def remove_file(self, _sandbox: Sandbox, _path: str) -> None:
        return None

    # No execute_command/execute_process on purpose: every test here stops at
    # the start, so an execution reaching the runtime would raise
    # AttributeError rather than pass quietly on a stub.


@pytest.fixture
async def sessions() -> AsyncIterator[Sessions]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_a_timed_out_start_leaves_the_sandbox_retryable(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core of DEV-2032: `starting`, not `failed`.

    `_ensure_running` records a failed start so a row cannot sit in `starting`
    holding capacity forever. That is right for a start that broke and wrong
    for one that was merely slow -- and it could not tell the difference,
    because every runtime error arrived as `SandboxUnavailableError`.

    Asserted as "a retry would start this", not as one exact status, because
    which retryable status the row holds depends on who called: `ensure_ready`
    reserves `starting` up front for capacity accounting, a queued execution
    does the same in `_mark_starting`, and the scheduler driven on its own
    leaves `created`. All three are the same claim -- `lazy_start_action` sends
    every one of them back into `_ensure_running_locked` -- and `failed` is not
    one of them. Neither is a leak: `reaper_stuck_starting_after_seconds` and
    `reaper_stuck_created_after_seconds` sweep a row nobody comes back for.
    """
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-t1", status="created"))
        await session.commit()
    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    scheduler = scheduler_module.Scheduler(Settings(), SlowThenReadyRuntime())

    with pytest.raises(SandboxStartTimeoutError):
        await scheduler.ensure_sandbox_ready("sbx-t1", timeout_seconds=5)

    async with sessions() as session:
        sandbox = await session.get(Sandbox, "sbx-t1")
    assert sandbox is not None
    assert sandbox.status != "failed"
    assert scheduler_module.lazy_start_action(sandbox.status) == "start"


async def test_a_retry_after_a_timed_out_start_brings_the_sandbox_up(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Retryable" has to mean a retry actually works, not just a softer status."""
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-t2", status="created"))
        await session.commit()
    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    runtime = SlowThenReadyRuntime()
    scheduler = scheduler_module.Scheduler(Settings(), runtime)

    with pytest.raises(SandboxStartTimeoutError):
        await scheduler.ensure_sandbox_ready("sbx-t2", timeout_seconds=5)
    await scheduler.ensure_sandbox_ready("sbx-t2", timeout_seconds=5)

    assert runtime.start_calls == ["sbx-t2", "sbx-t2"]
    async with sessions() as session:
        sandbox = await session.get(Sandbox, "sbx-t2")
    assert sandbox is not None
    assert sandbox.status == "running"


async def test_a_start_that_genuinely_fails_is_still_marked_failed(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capacity-leak protection `_ensure_running` exists for must survive."""

    class BrokenRuntime(SlowThenReadyRuntime):
        async def start_sandbox(self, sandbox: Sandbox) -> StartedSandbox:
            self.start_calls.append(sandbox.id)
            raise SandboxUnavailableError(CONTROL_PLANE_BOOM)

    async with sessions() as session:
        session.add(sandbox_row(id="sbx-t3", status="created"))
        await session.commit()
    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    scheduler = scheduler_module.Scheduler(Settings(), BrokenRuntime())

    with pytest.raises(SandboxUnavailableError):
        await scheduler.ensure_sandbox_ready("sbx-t3", timeout_seconds=5)

    async with sessions() as session:
        sandbox = await session.get(Sandbox, "sbx-t3")
    assert sandbox is not None
    assert sandbox.status == "failed"


# --- the API: a slow start answers SANDBOX_STARTING --------------------------


@pytest.fixture
def slow_runtime() -> SlowThenReadyRuntime:
    return SlowThenReadyRuntime()


@pytest.fixture
async def client(
    sessions: Sessions,
    slow_runtime: SlowThenReadyRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    settings = Settings(lazy_start_wait_timeout_seconds=5)
    app.state.settings = settings
    app.state.runtime = slow_runtime
    app.state.scheduler = scheduler_module.Scheduler(settings, slow_runtime)

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


async def test_a_slow_upload_start_answers_sandbox_starting_and_a_retry_succeeds(
    client: httpx.AsyncClient,
    sessions: Sessions,
    slow_runtime: SlowThenReadyRuntime,
) -> None:
    """The exit criterion, end to end on Onvo Lite's own path.

    `create, upload, transform`: the upload is what forces the lazy start. A
    slow one used to answer `SANDBOX_START_FAILED` and leave a `failed` row,
    so the retry the code told the caller not to make was also the only thing
    that could have worked. Now it answers `SANDBOX_STARTING` with
    `Retry-After`, and the retry brings the sandbox up.
    """
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-api", status="created"))
        await session.commit()

    first = await client.put(
        "/v1/sandboxes/sbx-api/files/content",
        params={"path": "/tmp/onvo.bin"},  # noqa: S108
        content=b"payload",
    )

    assert first.status_code == httpx.codes.SERVICE_UNAVAILABLE
    assert first.json()["detail"]["code"] == SANDBOX_STARTING_CODE
    assert first.headers.get("Retry-After") is not None
    async with sessions() as session:
        sandbox = await session.get(Sandbox, "sbx-api")
    assert sandbox is not None
    assert sandbox.status == "starting"

    second = await client.put(
        "/v1/sandboxes/sbx-api/files/content",
        params={"path": "/tmp/onvo.bin"},  # noqa: S108
        content=b"payload",
    )

    assert second.status_code == httpx.codes.OK
    assert slow_runtime.start_calls == ["sbx-api", "sbx-api"]


# --- the execution path shares the same start ---------------------------------


async def test_an_execution_whose_start_times_out_leaves_the_sandbox_retryable(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`POST /executions` reaches the same `_ensure_running`, and must agree.

    The execution itself is over -- the caller asked for a result and is not
    getting one -- but the sandbox behind it was only slow. Failing the
    execution *and* the sandbox would put the next call back at square one
    against a `failed` row.
    """
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-exec", status="created"))
        session.add(
            Execution(
                id="exec-1",
                sandbox_id="sbx-exec",
                kind="command",
                status="queued",
                command="echo hi",
                environment={},
                timeout_seconds=30,
                cancel_requested=False,
            )
        )
        await session.commit()
    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    scheduler = scheduler_module.Scheduler(Settings(), SlowThenReadyRuntime())

    await scheduler._run_execution("exec-1")

    async with sessions() as session:
        execution = await session.get(Execution, "exec-1")
        sandbox = await session.get(Sandbox, "sbx-exec")
    assert execution is not None
    assert execution.status == "failed"
    assert sandbox is not None
    assert sandbox.status == "starting"


async def test_a_slow_explicit_resume_answers_sandbox_starting_too(
    client: httpx.AsyncClient, sessions: Sessions
) -> None:
    """`POST /resume` runs its own start, outside the scheduler entirely.

    It calls `runtime.resume` directly rather than going through
    `ensure_sandbox_ready`, so it has its own copy of the classification and
    its own chance to get it wrong. Before this it had only a
    `SandboxUnavailableError` branch, which meant the new timeout would have
    escaped as a 500 -- a worse answer than the wrong 503 it replaced.
    """
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-resume", status="paused_cold"))
        await session.commit()

    response = await client.post("/v1/sandboxes/sbx-resume/resume")

    assert response.status_code == httpx.codes.SERVICE_UNAVAILABLE
    assert response.json()["detail"]["code"] == SANDBOX_STARTING_CODE
    assert response.headers.get("Retry-After") is not None
    async with sessions() as session:
        sandbox = await session.get(Sandbox, "sbx-resume")
    assert sandbox is not None
    assert sandbox.status != "failed"
    assert scheduler_module.lazy_start_action(sandbox.status) == "start"
