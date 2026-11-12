"""No sandbox outlives `max_sandbox_lifetime_seconds`, whatever it is doing.

Native /v1 sandboxes had no maximum lifetime, and `idle_timeout_seconds` only
ends a sandbox that goes quiet. In November 2026 a trial account kept a public
shell (ttyd behind a tunnel) alive in a sandbox for an hour that way, and
nothing would have ended it at a week either: a busy sandbox is never idle.

Three rules have to hold together, and each is useless without the others:

- an over-age sandbox is killed by the expiry sweep;
- a running execution is not cut off, so the sweep waits for the sandbox to be
  free of active work;
- new executions on an over-age sandbox are refused, or that wait never ends.

SQLite and a fake runtime, the same harness as `test_reaper_missing_sandbox.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from harborbox import scheduler as scheduler_module
from harborbox.config import Settings
from harborbox.models import Base, Execution, Sandbox
from harborbox.opensandbox_compat import INTERNAL_PREFIX
from harborbox.scheduler import (
    ERROR_NAME_SANDBOX_UNAVAILABLE,
    OVER_AGE_DETAIL,
    _reject_ineligible,
    sandbox_is_over_age,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]

NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)
ONE_DAY = 86_400


@pytest.fixture
async def sessions(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Sessions]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler_module, "session_factory", factory)
    yield factory
    await engine.dispose()


def sandbox_row(identifier: str = "sbx-1", **overrides: object) -> Sandbox:
    """Build a running sandbox created a moment ago; tests age it as they need."""
    values: dict[str, object] = {
        "id": identifier,
        "status": "running",
        "container_id": f"c-{identifier}",
        "container_name": f"n-{identifier}",
        "agent_token": "test-token",
        "memory_mb": 512,
        "cpu": 1.0,
        "pids_limit": 1024,
        "idle_timeout_seconds": 300,
        "metadata_": {"template": "base"},
        "created_at": datetime.now(UTC),
        "last_activity_at": datetime.now(UTC),
    }
    values.update(overrides)
    return Sandbox(**values)


def created(**kw: float) -> datetime:
    return datetime.now(UTC) - timedelta(**kw)


def execution_row(sandbox_id: str, status: str) -> Execution:
    return Execution(
        id=f"exe-{sandbox_id}",
        sandbox_id=sandbox_id,
        kind="command",
        status=status,
        command="true",
        timeout_seconds=30,
    )


class KillRuntime:
    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing = failing or set()
        self.killed: list[str] = []

    async def kill(self, sandbox: Sandbox) -> None:
        if sandbox.id in self.failing:
            message = "opensandbox is restarting"
            raise RuntimeError(message)
        self.killed.append(sandbox.id)


async def add(sessions: Sessions, *rows: Sandbox | Execution) -> None:
    async with sessions() as session:
        session.add_all(rows)
        await session.commit()


async def status_of(sessions: Sessions, sandbox_id: str) -> str:
    async with sessions() as session:
        row = await session.get(Sandbox, sandbox_id)
    assert row is not None
    return row.status


def make_scheduler(runtime: KillRuntime, **settings: object) -> scheduler_module.Scheduler:
    return scheduler_module.Scheduler(Settings(**settings), runtime)  # type: ignore[arg-type]


# --- the age rule itself ------------------------------------------------------


class TestOverAge:
    def test_a_sandbox_past_the_lifetime_is_over_age(self) -> None:
        assert sandbox_is_over_age(
            NOW - timedelta(days=2), now=NOW, max_lifetime_seconds=ONE_DAY
        )

    def test_a_younger_sandbox_is_not(self) -> None:
        assert not sandbox_is_over_age(
            NOW - timedelta(hours=23), now=NOW, max_lifetime_seconds=ONE_DAY
        )

    def test_boundary_is_inclusive(self) -> None:
        assert sandbox_is_over_age(
            NOW - timedelta(seconds=ONE_DAY), now=NOW, max_lifetime_seconds=ONE_DAY
        )

    def test_zero_disables_the_limit(self) -> None:
        assert not sandbox_is_over_age(
            NOW - timedelta(days=400), now=NOW, max_lifetime_seconds=0
        )

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        """SQLite hands `created_at` back naive; PostgreSQL does not."""
        naive = (NOW - timedelta(days=2)).replace(tzinfo=None)

        assert sandbox_is_over_age(naive, now=NOW, max_lifetime_seconds=ONE_DAY)
        assert sandbox_is_over_age(
            naive, now=NOW.replace(tzinfo=None), max_lifetime_seconds=ONE_DAY
        )

    def test_the_default_lifetime_is_one_day(self) -> None:
        assert Settings().max_sandbox_lifetime_seconds == ONE_DAY


# --- new work is refused ------------------------------------------------------


class TestAdmission:
    def test_an_execution_on_an_over_age_sandbox_is_failed_as_unavailable(self) -> None:
        """The existing error name, so callers already know what to do with it."""
        sandbox = sandbox_row(created_at=NOW - timedelta(days=2))
        execution = execution_row(sandbox.id, "queued")
        execution.cancel_requested = False

        rejected = _reject_ineligible(
            execution, sandbox, NOW, max_lifetime_seconds=ONE_DAY
        )

        assert rejected
        assert execution.status == "failed"
        assert execution.finished_at == NOW
        assert execution.error == {
            "name": ERROR_NAME_SANDBOX_UNAVAILABLE,
            "value": OVER_AGE_DETAIL,
            "traceback": [],
        }

    def test_an_execution_on_a_young_sandbox_is_admitted(self) -> None:
        sandbox = sandbox_row(created_at=NOW - timedelta(hours=1))
        execution = execution_row(sandbox.id, "queued")
        execution.cancel_requested = False

        assert not _reject_ineligible(
            execution, sandbox, NOW, max_lifetime_seconds=ONE_DAY
        )
        assert execution.status == "queued"

    def test_a_killed_sandbox_keeps_its_own_message(self) -> None:
        sandbox = sandbox_row(status="killed", created_at=NOW - timedelta(days=2))
        execution = execution_row(sandbox.id, "queued")
        execution.cancel_requested = False

        assert _reject_ineligible(execution, sandbox, NOW, max_lifetime_seconds=ONE_DAY)
        assert execution.error is not None
        assert execution.error["value"] == "sandbox is killed"

    def test_a_cancelled_execution_is_still_just_cancelled(self) -> None:
        sandbox = sandbox_row(created_at=NOW - timedelta(days=2))
        execution = execution_row(sandbox.id, "queued")
        execution.cancel_requested = True

        assert _reject_ineligible(execution, sandbox, NOW, max_lifetime_seconds=ONE_DAY)
        assert execution.status == "cancelled"


# --- the sweep ----------------------------------------------------------------


async def test_an_over_age_sandbox_is_killed_and_a_young_one_is_not(
    sessions: Sessions,
) -> None:
    await add(
        sessions,
        sandbox_row("sbx-old", created_at=created(days=2)),
        sandbox_row("sbx-young", created_at=created(hours=1)),
    )
    runtime = KillRuntime()

    await make_scheduler(runtime)._terminate_expired_sandboxes()

    assert runtime.killed == ["sbx-old"]
    assert await status_of(sessions, "sbx-old") == "killed"
    assert await status_of(sessions, "sbx-young") == "running"
    async with sessions() as session:
        row = await session.get(Sandbox, "sbx-old")
    assert row is not None
    assert row.container_id is None


async def test_recent_activity_does_not_extend_the_lifetime(
    sessions: Sessions,
) -> None:
    """The shell that prompted this was busy the whole time."""
    await add(
        sessions,
        sandbox_row(created_at=created(days=2), last_activity_at=datetime.now(UTC)),
    )
    runtime = KillRuntime()

    await make_scheduler(runtime)._terminate_expired_sandboxes()

    assert runtime.killed == ["sbx-1"]


async def test_a_running_execution_is_not_cut_off_mid_run(
    sessions: Sessions,
) -> None:
    """It is killed on a later pass, once the execution has finished."""
    await add(
        sessions,
        sandbox_row(created_at=created(days=2)),
        execution_row("sbx-1", "running"),
    )
    runtime = KillRuntime()
    scheduler = make_scheduler(runtime)

    await scheduler._terminate_expired_sandboxes()
    assert runtime.killed == []
    assert await status_of(sessions, "sbx-1") == "running"

    async with sessions() as session:
        execution = await session.get(Execution, "exe-sbx-1")
        assert execution is not None
        execution.status = "succeeded"
        await session.commit()

    await scheduler._terminate_expired_sandboxes()
    assert runtime.killed == ["sbx-1"]
    assert await status_of(sessions, "sbx-1") == "killed"


async def test_a_cold_paused_sandbox_is_covered_too(sessions: Sessions) -> None:
    """Exempting it would only move the kill to seconds after its next resume."""
    await add(
        sessions,
        sandbox_row(status="paused_cold", container_id=None, created_at=created(days=2)),
    )
    runtime = KillRuntime()

    await make_scheduler(runtime)._terminate_expired_sandboxes()

    assert await status_of(sessions, "sbx-1") == "killed"


async def test_a_lifetime_of_zero_disables_the_sweep(sessions: Sessions) -> None:
    await add(sessions, sandbox_row(created_at=created(days=400)))
    runtime = KillRuntime()

    await make_scheduler(
        runtime, max_sandbox_lifetime_seconds=0
    )._terminate_expired_sandboxes()

    assert runtime.killed == []
    assert await status_of(sessions, "sbx-1") == "running"


async def test_an_explicit_expiry_still_kills_a_young_sandbox(
    sessions: Sessions,
) -> None:
    """The OpenSandbox-shaped `expires_at` keeps working beside the lifetime."""
    expired = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await add(
        sessions,
        sandbox_row(
            "sbx-expired",
            metadata_={"template": "base", f"{INTERNAL_PREFIX}expires_at": expired},
        ),
        sandbox_row(
            "sbx-later",
            metadata_={"template": "base", f"{INTERNAL_PREFIX}expires_at": later},
        ),
    )
    runtime = KillRuntime()

    await make_scheduler(runtime)._terminate_expired_sandboxes()

    assert runtime.killed == ["sbx-expired"]


async def test_one_failed_kill_does_not_stop_the_others(sessions: Sessions) -> None:
    """The first deploy meets every sandbox older than a day in one pass.

    The error is still raised afterwards, so the stage is logged as failed and
    backs off; the sandbox that could not be killed stays for the next pass.
    """
    await add(
        sessions,
        sandbox_row("sbx-a", created_at=created(days=3)),
        sandbox_row("sbx-b", created_at=created(days=2)),
    )
    runtime = KillRuntime(failing={"sbx-a"})

    with pytest.raises(RuntimeError, match="opensandbox is restarting"):
        await make_scheduler(runtime)._terminate_expired_sandboxes()

    assert runtime.killed == ["sbx-b"]
    assert await status_of(sessions, "sbx-a") == "running"
    assert await status_of(sessions, "sbx-b") == "killed"
