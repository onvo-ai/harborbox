"""The idle sweep must not pause a sandbox that a command was just admitted to.

The sweep picks idle sandboxes in one read and pauses them later; a cold pause
takes a snapshot (about 5 s on a live stack) and then removes the container.
A command admitted in between ran in that container and was cut off, and
`_fail_execution` then marked the sandbox `failed`, so the caller lost the
sandbox and its files at the moment it came back to use them.

This replays the path the TLA+ model in `docs/state-machines/models/H2/`
found, one step per model action, against the real scheduler. PostgreSQL,
because the sweep's idle arithmetic needs the timezone-aware timestamps SQLite
does not return, and a fake runtime that holds the pause open until the test
lets it finish.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from harborbox import scheduler as scheduler_module
from harborbox.api import app
from harborbox.config import Settings
from harborbox.db import Base, get_session
from harborbox.errors import SandboxUnavailableError
from harborbox.models import Execution, Sandbox, SandboxTemplate
from harborbox.opensandbox_runtime import SNAPSHOT_METADATA_KEY
from harborbox.runtime_protocol import StartedSandbox, WarmPoolReservation
from harborbox.scheduler import Scheduler
from harborbox.schemas import LogOutput, RuntimeExecutionResult
from harborbox.security import require_api_key

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.postgres

Sessions = async_sessionmaker[AsyncSession]

# Every wait in the test is bounded, so a regression fails instead of hanging.
STEP_TIMEOUT_SECONDS = 10


class PausingRuntime:
    """One sandbox's container and snapshots, as OpenSandbox would hold them.

    `pause` stops after it is entered and waits for `release_pause`, which is
    the model's gap between "the sweep picks the sandbox" and "the snapshot is
    taken and the container removed". A command waits for `finish_command`;
    if its container is removed first, it fails the way `_run_command` does
    when the sandbox dies under it.
    """

    def __init__(self) -> None:
        self.container: str | None = "c-1"
        self.snapshots: set[str] = set()
        self.cut_off: list[str] = []
        self.pause_entered = asyncio.Event()
        self.release_pause = asyncio.Event()
        self.container_removed = asyncio.Event()
        self.command_started = asyncio.Event()
        self.finish_command = asyncio.Event()

    async def total_memory_mb(self) -> int:
        return 8192

    async def available_memory_mb(self) -> int:
        return 8192

    def warm_pool_reservation(self) -> WarmPoolReservation:
        return WarmPoolReservation()

    async def pause(self, sandbox: Sandbox, *, memory: bool) -> None:
        assert not memory
        self.pause_entered.set()
        await self.release_pause.wait()
        self.snapshots.add("snap-1")
        sandbox.metadata_ = {**sandbox.metadata_, SNAPSHOT_METADATA_KEY: "snap-1"}
        self.container = None
        self.container_removed.set()

    async def start_sandbox(self, sandbox: Sandbox) -> StartedSandbox:
        snapshot = sandbox.metadata_.get(SNAPSHOT_METADATA_KEY)
        if snapshot is not None and snapshot not in self.snapshots:
            message = f"snapshot {snapshot} does not exist"
            raise SandboxUnavailableError(message)
        self.container = "c-2"
        return StartedSandbox(id="c-2", name="c-2")

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

    async def execute_command(
        self, sandbox: Sandbox, request: Any  # noqa: ANN401
    ) -> RuntimeExecutionResult:
        if self.container is None or sandbox.container_id != self.container:
            message = "the sandbox has no container"
            raise SandboxUnavailableError(message)
        self.command_started.set()
        finished = asyncio.ensure_future(self.finish_command.wait())
        removed = asyncio.ensure_future(self.container_removed.wait())
        await asyncio.wait({finished, removed}, return_when=asyncio.FIRST_COMPLETED)
        finished.cancel()
        removed.cancel()
        if sandbox.container_id != self.container:
            self.cut_off.append(request.command)
            message = "the sandbox died while the command was running"
            raise SandboxUnavailableError(message)
        return RuntimeExecutionResult(logs=LogOutput(stdout=["ok"]), exit_code=0)


@pytest.fixture
async def sessions(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Sessions]:
    url = os.environ.get("HARBORBOX_TEST_DATABASE_URL")
    if not url:
        pytest.skip("HARBORBOX_TEST_DATABASE_URL is not set; needs a real PostgreSQL")
    engine = create_async_engine(url)
    tables = [Sandbox.__table__, Execution.__table__, SandboxTemplate.__table__]
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=tables)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler_module, "session_factory", factory)
    await clear(factory)
    try:
        yield factory
    finally:
        await clear(factory)
        await engine.dispose()


async def clear(sessions: Sessions) -> None:
    async with sessions() as session:
        await session.execute(delete(Execution).where(Execution.sandbox_id == "sbx-1"))
        await session.execute(delete(Sandbox).where(Sandbox.id == "sbx-1"))
        await session.commit()


async def admit_and_run(scheduler: Scheduler) -> list[asyncio.Task[None]]:
    """One admission pass, then each admitted execution's run task, started."""
    held: list[str] = []

    async def hold(execution_id: str) -> None:
        held.append(execution_id)

    scheduler._run_execution = hold  # type: ignore[method-assign]
    await scheduler._admit_available_jobs()
    await asyncio.sleep(0)
    del scheduler._run_execution
    return [asyncio.create_task(scheduler._run_execution(i)) for i in held]


def cold_only(runtime: PausingRuntime) -> Scheduler:
    """Build a scheduler with freezing off, so an idle sandbox goes straight to cold."""
    return Scheduler(
        Settings(max_parallel_cpu=8, hot_pause_idle_seconds=0),
        runtime,  # type: ignore[arg-type]
    )


async def seed_idle_sandbox(sessions: Sessions) -> None:
    """Store a running sandbox that has been idle for 400 s of its 300 s timeout."""
    idle_since = datetime.now(UTC) - timedelta(seconds=400)
    sandbox: dict[str, Any] = {
        "id": "sbx-1",
        "status": "running",
        "container_id": "c-1",
        "container_name": "c-1",
        "agent_token": "token",
        "memory_mb": 512,
        "cpu": 0.5,
        "pids_limit": 128,
        "idle_timeout_seconds": 300,
        "metadata_": {"template": "base"},
        "last_activity_at": idle_since,
    }
    async with sessions() as session:
        session.add(Sandbox(**sandbox))
        await session.commit()


async def test_an_idle_pause_does_not_cut_off_a_command_admitted_after_it_began(
    sessions: Sessions,
) -> None:
    runtime = PausingRuntime()
    scheduler = cold_only(runtime)
    await seed_idle_sandbox(sessions)

    # 1. The caller posts a command.
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
        # 2. The idle sweep picks the sandbox: idle for 400 s, nothing active.
        sweep = asyncio.create_task(scheduler._cold_pause_idle_sandboxes())
        await runtime.pause_entered.wait()
        # 3-4. The scheduler reads the queue and admits the command.
        runs = await admit_and_run(scheduler)
        # 5. The command starts.
        if runs:
            await runtime.command_started.wait()
        # 6. The sandbox is snapshotted and its container removed.
        runtime.release_pause.set()
        # 7. The sweep records the pause.
        await sweep
        # 8. The command returns.
        runtime.finish_command.set()
        await asyncio.gather(*runs)
        # A command held back is still the caller's to run: one more pass.
        await asyncio.gather(*await admit_and_run(scheduler))

    async with sessions() as session:
        stored = await session.get(Sandbox, "sbx-1")
        execution = await session.get(Execution, "exe-1")
    assert stored is not None
    assert execution is not None
    assert stored.status != "failed"
    assert runtime.cut_off == []
    assert execution.status == "succeeded"
    assert runtime.snapshots <= {stored.metadata_.get(SNAPSHOT_METADATA_KEY)}


async def test_a_pause_claim_waits_for_an_admission_pass_and_sees_what_it_admitted(
    sessions: Sessions,
) -> None:
    """The model's lock between "the scheduler reads the queue" and its commit.

    Admission locks the sandbox row and writes `admitted`; the claim asks for
    the same lock and only then counts active executions. Without the lock, or
    with the count read before it, the claim would see no active execution
    and pause the sandbox the command was just admitted to.
    """
    runtime = PausingRuntime()
    runtime.release_pause.set()
    scheduler = cold_only(runtime)
    await seed_idle_sandbox(sessions)
    async with sessions() as session:
        planned = await session.get(Sandbox, "sbx-1")
    assert planned is not None

    async with asyncio.timeout(STEP_TIMEOUT_SECONDS):
        async with sessions() as admission:
            # The scheduler reads the queue and locks the sandbox row.
            await admission.execute(
                select(Sandbox).where(Sandbox.id == "sbx-1").with_for_update()
            )
            admission.add(
                Execution(
                    id="exe-1",
                    sandbox_id="sbx-1",
                    kind="command",
                    status="admitted",
                    command="echo hi",
                    timeout_seconds=30,
                )
            )
            await admission.flush()
            # The idle sweep tries to claim the sandbox while the pass is open.
            claim = asyncio.create_task(scheduler.suspend(planned, memory=False))
            await asyncio.sleep(0.2)
            # The scheduler admits the command.
            await admission.commit()
        paused = await claim

    async with sessions() as session:
        stored = await session.get(Sandbox, "sbx-1")
    assert stored is not None
    assert paused is False
    assert not runtime.pause_entered.is_set()
    assert stored.status == "running"


class BootRuntime:
    """Reports each container's state as OpenSandbox would after a restart."""

    def __init__(self, states: dict[str, str | None]) -> None:
        self.states = states

    async def container_status(self, sandbox: Sandbox) -> str | None:
        return self.states[sandbox.id]


async def test_a_boot_settles_each_pause_a_restart_cut_short(sessions: Sessions) -> None:
    """Where the container got to says where the sandbox is now."""
    states: dict[str, str | None] = {
        "sbx-1": "running",  # cut short before the snapshot: still running
        "sbx-2": "paused",  # cut short before the thaw: still frozen
        "sbx-3": "exited",  # its container is gone and the snapshot id with it
    }
    async with sessions() as session:
        for sandbox_id in states:
            session.add(
                Sandbox(
                    id=sandbox_id,
                    status="pausing",
                    container_id=f"c-{sandbox_id}",
                    agent_token="token",  # noqa: S106
                    memory_mb=512,
                    cpu=0.5,
                    pids_limit=128,
                    idle_timeout_seconds=300,
                    metadata_={},
                )
            )
        await session.commit()
    try:
        scheduler = Scheduler(Settings(), BootRuntime(states))  # type: ignore[arg-type]
        await scheduler._recover_interrupted_jobs()
        async with sessions() as session:
            settled = {
                sandbox_id: (await session.get(Sandbox, sandbox_id)).status  # type: ignore[union-attr]
                for sandbox_id in states
            }
    finally:
        async with sessions() as session:
            await session.execute(delete(Sandbox).where(Sandbox.id.in_(list(states))))
            await session.commit()
    assert settled == {"sbx-1": "running", "sbx-2": "paused_memory", "sbx-3": "failed"}


async def test_a_sandbox_deleted_during_its_pause_leaves_no_snapshot_behind(
    sessions: Sessions,
) -> None:
    """DELETE lands while the sweep's pause is taking its snapshot.

    DELETE removes what the row knows of, which is not the new snapshot. The
    sweep then finds `killed` where its claim was, and deletes that snapshot
    itself rather than leave it with nothing pointing at it.
    """
    runtime = PausingRuntime()
    scheduler = cold_only(runtime)
    await seed_idle_sandbox(sessions)
    app.state.settings = scheduler.settings
    app.state.runtime = runtime
    app.state.scheduler = scheduler

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as opened:
            yield opened

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[require_api_key] = lambda: None
    try:
        async with (
            asyncio.timeout(STEP_TIMEOUT_SECONDS),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://harborbox"
            ) as client,
        ):
            sweep = asyncio.create_task(scheduler._cold_pause_idle_sandboxes())
            await runtime.pause_entered.wait()
            deleted = await client.delete("/v1/sandboxes/sbx-1")
            runtime.release_pause.set()
            await sweep
    finally:
        app.dependency_overrides.clear()

    async with sessions() as session:
        stored = await session.get(Sandbox, "sbx-1")
    assert stored is not None
    assert deleted.status_code == HTTPStatus.NO_CONTENT
    assert stored.status == "killed"
    assert runtime.snapshots == set()
