"""`reap_once` against a database: stale rows are killed, history is deleted.

`test_reaper.py` covers the decision (`plan_reap`) with no database. This file
covers what `reap_once` then does with it, which until now had no test at all,
for the three leaks found in production on 2026-11-12:

- four `pooled` rows from a warm-pool design that ended on 2026-08-05, no
  container behind them, still reserving 3,072 MB of a 10,686 MB budget;
- 205 `paused_cold` rows, 204 idle for more than 30 days, each holding a
  snapshot;
- 21,910 `killed` sandboxes and 123,373 executions that nothing ever deleted.

SQLite, with a fake runtime. No Docker and no PostgreSQL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from harborbox import reaper as reaper_module
from harborbox.config import Settings
from harborbox.models import Base, Execution, Sandbox
from harborbox.reaper import reap_once

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]

SNAPSHOT_KEY = "harborbox.snapshot_id"
SMALL_BATCH = 2


@pytest.fixture
async def sessions() -> AsyncIterator[Sessions]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def ago(**kw: float) -> datetime:
    return datetime.now(UTC) - timedelta(**kw)


def sandbox_row(identifier: str, status: str, *, age: timedelta) -> Sandbox:
    stamp = datetime.now(UTC) - age
    return Sandbox(
        id=identifier,
        status=status,
        container_id=None,
        container_name=None,
        agent_token="test-token",  # noqa: S106 - not a credential
        memory_mb=768,
        cpu=1.0,
        pids_limit=1024,
        idle_timeout_seconds=300,
        metadata_={"template": "base"},
        created_at=stamp,
        updated_at=stamp,
        last_activity_at=stamp,
    )


def execution_row(
    identifier: str,
    sandbox_id: str,
    status: str,
    *,
    finished: datetime | None,
    created: datetime | None = None,
) -> Execution:
    return Execution(
        id=identifier,
        sandbox_id=sandbox_id,
        kind="command",
        status=status,
        command="true",
        timeout_seconds=30,
        created_at=created or finished or datetime.now(UTC),
        finished_at=finished,
    )


class KillRuntime:
    """Records kills, drops the snapshot id like the real one, and can fail."""

    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing = failing or set()
        self.killed: list[str] = []

    async def kill(self, sandbox: Sandbox) -> None:
        if sandbox.id in self.failing:
            message = "opensandbox is restarting"
            raise RuntimeError(message)
        self.killed.append(sandbox.id)
        metadata = dict(sandbox.metadata_)
        metadata.pop(SNAPSHOT_KEY, None)
        sandbox.metadata_ = metadata


async def add(sessions: Sessions, *rows: Sandbox | Execution) -> None:
    async with sessions() as session:
        session.add_all(rows)
        await session.commit()


async def statuses(sessions: Sessions) -> dict[str, str]:
    async with sessions() as session:
        rows = (await session.execute(select(Sandbox.id, Sandbox.status))).all()
    return {row[0]: row[1] for row in rows}


async def execution_ids(sessions: Sessions) -> set[str]:
    async with sessions() as session:
        return set((await session.scalars(select(Execution.id))).all())


async def sweep(
    sessions: Sessions, runtime: KillRuntime, **settings: object
) -> reaper_module.ReapPlan:
    return await reap_once(sessions, runtime, Settings(**settings))  # type: ignore[arg-type]


# --- legacy pooled rows -------------------------------------------------------


async def test_a_legacy_pooled_row_is_killed_and_stops_reserving_memory(
    sessions: Sessions,
) -> None:
    """`pooled` is a reserved state; `killed` is not. That is the whole fix."""
    await add(
        sessions,
        sandbox_row("sbx-pooled", "pooled", age=timedelta(days=99)),
        sandbox_row("sbx-pooling", "pooling", age=timedelta(days=99)),
    )
    runtime = KillRuntime()

    plan = await sweep(sessions, runtime)

    assert set(plan.delete) == {"sbx-pooled", "sbx-pooling"}
    assert set(runtime.killed) == {"sbx-pooled", "sbx-pooling"}
    assert await statuses(sessions) == {
        "sbx-pooled": "killed",
        "sbx-pooling": "killed",
    }


async def test_a_pooled_row_is_still_released_when_the_runtime_kill_fails(
    sessions: Sessions,
) -> None:
    """The production rows had no container at all; the reservation must go."""
    await add(sessions, sandbox_row("sbx-pooled", "pooled", age=timedelta(days=99)))

    await sweep(sessions, KillRuntime(failing={"sbx-pooled"}))

    assert await statuses(sessions) == {"sbx-pooled": "killed"}


# --- paused_cold retention ----------------------------------------------------


async def test_an_abandoned_cold_sandbox_is_killed_and_its_snapshot_released(
    sessions: Sessions,
) -> None:
    old = sandbox_row("sbx-cold", "paused_cold", age=timedelta(days=45))
    old.metadata_ = {"template": "base", SNAPSHOT_KEY: "snap-1"}
    recent = sandbox_row("sbx-recent", "paused_cold", age=timedelta(days=3))
    await add(sessions, old, recent)
    runtime = KillRuntime()

    # The lifetime sweep is the scheduler's, not this one's, so a three-day-old
    # row is left alone here whatever `max_sandbox_lifetime_seconds` says.
    await sweep(sessions, runtime)

    assert runtime.killed == ["sbx-cold"]
    assert await statuses(sessions) == {
        "sbx-cold": "killed",
        "sbx-recent": "paused_cold",
    }
    async with sessions() as session:
        row = await session.get(Sandbox, "sbx-cold")
    assert row is not None
    assert SNAPSHOT_KEY not in row.metadata_


async def test_a_cold_sandbox_whose_kill_fails_is_kept_for_the_next_sweep(
    sessions: Sessions,
) -> None:
    """Marking it `killed` anyway would orphan the snapshot for good.

    The row is the only record of the snapshot id, and `runtime.kill` already
    treats a missing sandbox as success, so a failure is transient.
    """
    await add(sessions, sandbox_row("sbx-cold", "paused_cold", age=timedelta(days=45)))

    await sweep(sessions, KillRuntime(failing={"sbx-cold"}))

    assert await statuses(sessions) == {"sbx-cold": "paused_cold"}


async def test_zero_retention_days_leaves_cold_sandboxes_alone(
    sessions: Sessions,
) -> None:
    await add(
        sessions, sandbox_row("sbx-cold", "paused_cold", age=timedelta(days=400))
    )
    runtime = KillRuntime()

    await sweep(sessions, runtime, reaper_paused_cold_retention_days=0)

    assert runtime.killed == []
    assert await statuses(sessions) == {"sbx-cold": "paused_cold"}


# --- history ------------------------------------------------------------------


async def test_an_old_killed_sandbox_is_deleted_with_its_executions(
    sessions: Sessions,
) -> None:
    await add(
        sessions,
        sandbox_row("sbx-old", "killed", age=timedelta(days=40)),
        sandbox_row("sbx-new", "killed", age=timedelta(days=2)),
        execution_row("exe-old", "sbx-old", "succeeded", finished=ago(days=40)),
        execution_row("exe-new", "sbx-new", "succeeded", finished=ago(days=2)),
    )

    plan = await sweep(sessions, KillRuntime())

    assert plan.purged_sandboxes == 1
    assert plan.purged_executions == 1
    assert await statuses(sessions) == {"sbx-new": "killed"}
    assert await execution_ids(sessions) == {"exe-new"}


async def test_old_finished_executions_go_even_when_their_sandbox_stays(
    sessions: Sessions,
) -> None:
    """Only terminal states. A queued execution is never history."""
    await add(
        sessions,
        sandbox_row("sbx-live", "running", age=timedelta(minutes=1)),
        execution_row("exe-done", "sbx-live", "succeeded", finished=ago(days=31)),
        execution_row("exe-failed", "sbx-live", "failed", finished=ago(days=31)),
        # Terminal with no `finished_at`: aged by `created_at` instead.
        execution_row(
            "exe-undated", "sbx-live", "cancelled", finished=None, created=ago(days=31)
        ),
        execution_row("exe-recent", "sbx-live", "succeeded", finished=ago(days=1)),
        execution_row(
            "exe-queued", "sbx-live", "queued", finished=None, created=ago(days=31)
        ),
    )

    plan = await sweep(sessions, KillRuntime())

    assert plan.purged_sandboxes == 0
    assert plan.purged_executions == len({"exe-done", "exe-failed", "exe-undated"})
    assert await execution_ids(sessions) == {"exe-recent", "exe-queued"}


async def test_history_is_deleted_in_bounded_batches(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first production sweep meets about 90,000 rows.

    One statement over all of them is one long transaction against tables the
    scheduler writes constantly. Each sweep takes one batch; the next sweep
    takes the next.
    """
    await add(
        sessions,
        sandbox_row("sbx-live", "running", age=timedelta(minutes=1)),
        *(
            sandbox_row(f"sbx-{n}", "killed", age=timedelta(days=40))
            for n in range(SMALL_BATCH + 1)
        ),
        *(
            execution_row(f"exe-{n}", "sbx-live", "succeeded", finished=ago(days=40))
            for n in range(SMALL_BATCH + 1)
        ),
    )
    original = reaper_module._purge_history

    async def small_batches(
        factory: Sessions, *, cutoff: datetime
    ) -> tuple[int, int]:
        return await original(factory, cutoff=cutoff, batch_size=SMALL_BATCH)

    monkeypatch.setattr(reaper_module, "_purge_history", small_batches)

    first = await sweep(sessions, KillRuntime())
    second = await sweep(sessions, KillRuntime())
    third = await sweep(sessions, KillRuntime())

    assert (first.purged_sandboxes, first.purged_executions) == (
        SMALL_BATCH,
        SMALL_BATCH,
    )
    assert (second.purged_sandboxes, second.purged_executions) == (1, 1)
    assert (third.purged_sandboxes, third.purged_executions) == (0, 0)
    async with sessions() as session:
        remaining = await session.scalar(select(func.count()).select_from(Sandbox))
    assert remaining == 1


async def test_zero_retention_days_keeps_all_history(sessions: Sessions) -> None:
    await add(
        sessions,
        sandbox_row("sbx-old", "killed", age=timedelta(days=400)),
        execution_row("exe-old", "sbx-old", "succeeded", finished=ago(days=400)),
    )

    plan = await sweep(sessions, KillRuntime(), reaper_killed_retention_days=0)

    assert (plan.purged_sandboxes, plan.purged_executions) == (0, 0)
    assert await statuses(sessions) == {"sbx-old": "killed"}
    assert await execution_ids(sessions) == {"exe-old"}


async def test_a_stuck_created_row_and_an_old_failure_are_still_handled(
    sessions: Sessions,
) -> None:
    """The sweep's original job, which had no database test either."""
    await add(
        sessions,
        sandbox_row("sbx-created", "created", age=timedelta(hours=2)),
        sandbox_row("sbx-failed", "failed", age=timedelta(days=3)),
        sandbox_row("sbx-running", "running", age=timedelta(hours=2)),
    )
    runtime = KillRuntime()

    plan = await sweep(sessions, runtime)

    assert plan.delete == ("sbx-created",)
    assert plan.prune == ("sbx-failed",)
    assert await statuses(sessions) == {
        "sbx-created": "killed",
        "sbx-running": "running",
    }
