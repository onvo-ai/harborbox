"""The reaper reclaims only starts nobody is waiting on.

`reaper.py` treats a `starting` row as stuck once it is older than
`reaper_stuck_starting_after_seconds`, and used to measure that age from
`last_activity_at`. A sandbox coming back from `paused_cold` has been idle
past its timeout, so its last activity is already older than the threshold
the moment it enters `starting`. A sweep that landed during the resume killed
it and deleted the snapshot the resume was restoring from. The sweep also
wrote `killed` over whatever the row held by the time its kill returned.

Each test replays one path the TLA+ model in `docs/state-machines/models/H3/`
found, one step per model action. SQLite and a fake runtime.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from harborbox import scheduler as scheduler_module
from harborbox.config import Settings
from harborbox.db import Base
from harborbox.errors import SandboxUnavailableError
from harborbox.models import Execution, Sandbox
from harborbox.opensandbox_runtime import SNAPSHOT_METADATA_KEY
from harborbox.reaper import reap_once
from harborbox.runtime_protocol import StartedSandbox, WarmPoolReservation
from harborbox.scheduler import Scheduler
from harborbox.schemas import LogOutput, RuntimeExecutionResult

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

Sessions = async_sessionmaker[AsyncSession]

STEP_TIMEOUT_SECONDS = 10
SETTINGS = Settings(max_parallel_cpu=8)


class SnapshotRuntime:
    """One sandbox restored from a snapshot, as OpenSandbox would do it.

    `start_gate`, when set, holds a start open until the test releases it.
    """

    def __init__(self) -> None:
        self.snapshots = {"snap-1"}
        self.container: str | None = None
        self.start_gate: asyncio.Event | None = None
        self.start_entered = asyncio.Event()

    async def total_memory_mb(self) -> int:
        return 8192

    async def available_memory_mb(self) -> int:
        return 8192

    def warm_pool_reservation(self) -> WarmPoolReservation:
        return WarmPoolReservation()

    async def start_sandbox(self, sandbox: Sandbox) -> StartedSandbox:
        self.start_entered.set()
        if self.start_gate is not None:
            await self.start_gate.wait()
        if sandbox.metadata_.get(SNAPSHOT_METADATA_KEY) not in self.snapshots:
            message = "the snapshot to restore from does not exist"
            raise SandboxUnavailableError(message)
        self.container = "c-1"
        return StartedSandbox(id="c-1", name="c-1")

    async def wait_until_ready(self, sandbox: Sandbox) -> None:
        if self.container is None or sandbox.container_id != self.container:
            message = "the sandbox has no container"
            raise SandboxUnavailableError(message)

    async def container_status(self, sandbox: Sandbox) -> str | None:
        if not sandbox.container_id:
            return None
        return "running" if sandbox.container_id == self.container else "exited"

    async def kill(self, sandbox: Sandbox) -> None:
        self.container = None
        self.snapshots.discard(sandbox.metadata_.get(SNAPSHOT_METADATA_KEY, ""))
        metadata = dict(sandbox.metadata_)
        metadata.pop(SNAPSHOT_METADATA_KEY, None)
        sandbox.metadata_ = metadata

    async def execute_command(
        self,
        sandbox: Sandbox,  # noqa: ARG002
        request: Any,  # noqa: ANN401, ARG002
    ) -> RuntimeExecutionResult:
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


def long_ago() -> datetime:
    """Past both the 300 s idle timeout and the 300 s stuck-start threshold."""
    return datetime.now(UTC) - timedelta(seconds=400)


async def add_sandbox(sessions: Sessions, **overrides: Any) -> None:  # noqa: ANN401
    values: dict[str, Any] = {
        "id": "sbx-1",
        "status": "paused_cold",
        "agent_token": "token",
        "memory_mb": 512,
        "cpu": 0.5,
        "pids_limit": 128,
        "idle_timeout_seconds": 300,
        "metadata_": {"template": "base", SNAPSHOT_METADATA_KEY: "snap-1"},
        "created_at": long_ago(),
        "last_activity_at": long_ago(),
    }
    values.update(overrides)
    async with sessions() as session:
        session.add(Sandbox(**values))
        await session.commit()


async def stored(sessions: Sessions) -> Sandbox:
    async with sessions() as session:
        sandbox = await session.get(Sandbox, "sbx-1")
    assert sandbox is not None
    return sandbox


async def test_a_sweep_during_a_resume_from_idle_leaves_the_sandbox_alone(
    sessions: Sessions,
) -> None:
    runtime = SnapshotRuntime()
    runtime.start_gate = asyncio.Event()
    scheduler = Scheduler(SETTINGS, runtime)  # type: ignore[arg-type]
    await add_sandbox(sessions)
    async with sessions() as session:
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

    async with asyncio.timeout(STEP_TIMEOUT_SECONDS):
        # 1. The caller posts a command: it is admitted and the restore begins.
        await scheduler._admit_available_jobs()
        await runtime.start_entered.wait()
        # 2-4. The reaper reads the sandbox, kills it, and marks it killed.
        await reap_once(sessions, runtime, SETTINGS)  # type: ignore[arg-type]
        # 5. The start returns.
        runtime.start_gate.set()
        await asyncio.gather(*scheduler._running_tasks.values())

    async with sessions() as session:
        execution = await session.get(Execution, "exe-1")
    assert execution is not None
    assert (await stored(sessions)).status == "running"
    assert execution.status == "succeeded"
    assert runtime.snapshots == {"snap-1"}


class ReadThenRetry(AsyncSession):
    """A session that runs `after_read` once, right after its first statement.

    The sweep's first statement is its read of the candidates, so this puts a
    step exactly between "the reaper reads the sandbox" and whatever it does
    next, whichever that is.
    """

    after_read: Callable[[], Awaitable[None]] | None = None

    async def execute(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        result = await super().execute(*args, **kwargs)
        hook, ReadThenRetry.after_read = ReadThenRetry.after_read, None
        if hook is not None:
            await hook()
        return result


async def test_a_sweep_does_not_kill_a_stuck_start_retried_since_it_read_it(
    sessions: Sessions,
) -> None:
    runtime = SnapshotRuntime()
    scheduler = Scheduler(SETTINGS, runtime)  # type: ignore[arg-type]
    # 1-2. A start ran past its budget and nobody retried it for 400 s.
    await add_sandbox(sessions, status="starting", updated_at=long_ago())

    async def retry() -> None:
        # 4-5. The caller retries, and the retried start comes up.
        await scheduler._ensure_running("sbx-1")

    ReadThenRetry.after_read = retry
    sweeping = async_sessionmaker(
        sessions.kw["bind"], expire_on_commit=False, class_=ReadThenRetry
    )
    async with asyncio.timeout(STEP_TIMEOUT_SECONDS):
        # 3. The sweep reads the sandbox as stuck in `starting`, then
        # 6-7. kills it and marks it killed.
        await reap_once(sweeping, runtime, SETTINGS)  # type: ignore[arg-type]

    assert (await stored(sessions)).status == "running"
    assert runtime.container == "c-1"
