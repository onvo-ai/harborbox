"""Races between an execution's run task, DELETE and cancel.

Each test replays one path the TLA+ model in
`docs/state-machines/models/H4/` found, one step per model action, against the
real routes and the real scheduler. The rules:

- an execution whose run task has ended is finished: nothing else will finish it;
- a sandbox stays `starting` only while a start for it is still coming;
- a cancel that answers "cancelled" stopped the command from running, and a
  finished execution keeps the outcome it first had.

SQLite and a fake runtime, the same harness as `test_sandbox_routes.py`.
Admission is run by hand with the run task held back, so each step of the path
happens in the order the model gives.
"""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from harborbox import api as api_module
from harborbox import scheduler as scheduler_module
from harborbox.api import app
from harborbox.config import Settings
from harborbox.db import Base, get_session
from harborbox.models import Execution, Sandbox
from harborbox.runtime_protocol import StartedSandbox, WarmPoolReservation
from harborbox.scheduler import Scheduler
from harborbox.schemas import LogOutput, RuntimeExecutionResult
from harborbox.security import require_api_key

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

Sessions = async_sessionmaker[AsyncSession]

ACTIVE = {"admitted", "starting", "running"}


class FakeRuntime:
    """A runtime whose sandboxes always start and whose commands succeed.

    `on_command` runs inside the command, which is where a test puts a step
    the model places between "the command is sent" and "the command returns".
    """

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.on_command: Callable[[], Awaitable[None]] | None = None

    async def total_memory_mb(self) -> int:
        return 8192

    async def available_memory_mb(self) -> int:
        return 8192

    def warm_pool_reservation(self) -> WarmPoolReservation:
        return WarmPoolReservation()

    async def start_sandbox(self, sandbox: Sandbox) -> StartedSandbox:
        return StartedSandbox(id=f"c-{sandbox.id}", name=f"n-{sandbox.id}")

    async def wait_until_ready(self, sandbox: Sandbox) -> None:  # noqa: ARG002
        return None

    async def container_status(self, sandbox: Sandbox) -> str | None:
        return "running" if sandbox.container_id else None

    async def kill(self, sandbox: Sandbox) -> None:  # noqa: ARG002
        return None

    async def execute_command(
        self,
        sandbox: Sandbox,  # noqa: ARG002
        request: Any,  # noqa: ANN401
    ) -> RuntimeExecutionResult:
        self.commands.append(request.command)
        if self.on_command is not None:
            await self.on_command()
        return RuntimeExecutionResult(logs=LogOutput(stdout=["ok"]), exit_code=0)


@pytest.fixture
async def sessions(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Sessions]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler_module, "session_factory", factory)
    yield factory
    await engine.dispose()


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.fixture
def scheduler(runtime: FakeRuntime) -> Scheduler:
    return Scheduler(Settings(max_parallel_cpu=8), runtime)  # type: ignore[arg-type]


@pytest.fixture
async def client(
    sessions: Sessions, runtime: FakeRuntime, scheduler: Scheduler
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.settings = scheduler.settings
    app.state.runtime = runtime
    app.state.scheduler = scheduler

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


async def seed(sessions: Sessions) -> None:
    """Store a sandbox that has never started, and one command queued on it."""
    sandbox: dict[str, Any] = {
        "id": "sbx-1",
        "status": "created",
        "agent_token": "token",
        "memory_mb": 512,
        "cpu": 0.5,
        "pids_limit": 128,
        "idle_timeout_seconds": 300,
        "metadata_": {"template": "base"},
    }
    async with sessions() as session:
        session.add(Sandbox(**sandbox))
        session.add(
            Execution(
                id="exe-1",
                sandbox_id="sbx-1",
                kind="command",
                status="queued",
                command="echo hi",
                timeout_seconds=30,
            )
        )
        await session.commit()


async def admit(scheduler: Scheduler) -> list[str]:
    """One admission pass, with the run tasks it would spawn held back."""
    held: list[str] = []

    async def hold(execution_id: str) -> None:
        held.append(execution_id)

    scheduler._run_execution = hold  # type: ignore[method-assign]
    await scheduler._admit_available_jobs()
    await asyncio.sleep(0)
    del scheduler._run_execution
    return held


class PausedCancel:
    """A cancel request stopped between reading the execution and writing it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient) -> None:
        self.read = asyncio.Event()
        self.go = asyncio.Event()
        original = api_module.get_execution_or_404

        async def paused(session: AsyncSession, execution_id: str) -> Execution:
            execution = await original(session, execution_id)
            self.read.set()
            await self.go.wait()
            return execution

        monkeypatch.setattr(api_module, "get_execution_or_404", paused)
        self.task = asyncio.create_task(client.post("/v1/executions/exe-1/cancel"))

    async def finish(self) -> httpx.Response:
        self.go.set()
        return await self.task


async def execution_status(client: httpx.AsyncClient) -> str:
    return str((await client.get("/v1/executions/exe-1")).json()["status"])


async def sandbox_status(client: httpx.AsyncClient) -> str:
    return str((await client.get("/v1/sandboxes/sbx-1")).json()["status"])


async def test_an_execution_admitted_just_before_a_delete_still_finishes(
    sessions: Sessions, scheduler: Scheduler, client: httpx.AsyncClient
) -> None:
    await seed(sessions)
    # 1-2. The scheduler picks the execution and admits it.
    assert await admit(scheduler) == ["exe-1"]
    # 3-4. The caller deletes the sandbox. DELETE cancels only `queued` rows.
    assert (await client.delete("/v1/sandboxes/sbx-1")).status_code == HTTPStatus.NO_CONTENT
    # 5. The run task begins, finds the sandbox gone, and ends.
    await scheduler._run_execution("exe-1")

    assert await execution_status(client) not in ACTIVE


async def test_a_cancel_racing_admission_does_not_leave_the_sandbox_starting(
    monkeypatch: pytest.MonkeyPatch,
    sessions: Sessions,
    scheduler: Scheduler,
    client: httpx.AsyncClient,
) -> None:
    await seed(sessions)
    # 1. The scheduler picks the execution (its pass runs below, in one go).
    # 2. The caller cancels: the request reads `queued`.
    cancel = PausedCancel(monkeypatch, client)
    await cancel.read.wait()
    # 3. The scheduler admits it, and moves the sandbox to `starting`.
    assert await admit(scheduler) == ["exe-1"]
    # 4. The cancel is saved.
    await cancel.finish()
    # 5. The run task begins.
    await scheduler._run_execution("exe-1")

    assert await sandbox_status(client) != "starting"


async def test_a_cancel_that_answers_cancelled_stops_the_command(
    monkeypatch: pytest.MonkeyPatch,
    sessions: Sessions,
    runtime: FakeRuntime,
    scheduler: Scheduler,
    client: httpx.AsyncClient,
) -> None:
    await seed(sessions)
    # 1-2. The caller cancels: the request reads `queued`.
    cancel = PausedCancel(monkeypatch, client)
    await cancel.read.wait()
    # 3. The scheduler admits the execution.
    assert await admit(scheduler) == ["exe-1"]
    answer: dict[str, httpx.Response] = {}

    async def save_cancel() -> None:
        answer["cancel"] = await cancel.finish()

    # 7. The cancel is saved while the command runs.
    runtime.on_command = save_cancel
    # 4-6 and 8. The run task starts the sandbox, sends the command, and saves
    # the result after the command returns.
    await scheduler._run_execution("exe-1")

    said_cancelled = (
        answer["cancel"].status_code == HTTPStatus.OK
        and answer["cancel"].json()["status"] == "cancelled"
    )
    if said_cancelled:
        assert runtime.commands == []
        assert await execution_status(client) == "cancelled"
    else:
        assert answer["cancel"].status_code == HTTPStatus.CONFLICT
        assert await execution_status(client) == "succeeded"
