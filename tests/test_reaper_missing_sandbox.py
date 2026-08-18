"""What the idle reaper does when a sandbox has vanished underneath it.

OpenSandbox is the source of truth for whether a container exists; the
scheduler's rows are a cache of that. So a sandbox disappearing between the
pause plan and the pause call is ordinary, not exceptional -- a redeploy, an
operator `docker rm`, or Coolify's nightly cleanup all produce it.

`terminate` already treats a missing sandbox as success
(`opensandbox_runtime.py`, the NOT_FOUND branches). The pause path did not, and
in production that surfaced as a full traceback every reaper poll:

    SandboxUnavailableError: Get endpoint for sandbox 87b6925a-... failed:
    Sandbox 87b6925a-... not found | [DOCKER::SANDBOX_NOT_FOUND]
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from harborbox import scheduler as scheduler_module
from harborbox.config import Settings
from harborbox.errors import SandboxUnavailableError
from harborbox.models import Base, Sandbox

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
async def sessions() -> AsyncIterator[Sessions]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def sandbox_row(**overrides: object) -> Sandbox:
    """Build an idle, running sandbox, the shape the cold-pause planner acts on."""
    # Naive UTC to match what SQLite hands back; the tests align the
    # scheduler's clock to the same, see the monkeypatch below.
    idle = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
    values: dict[str, object] = {
        "id": "sbx-1",
        "status": "running",
        "container_id": "c1",
        "container_name": "n1",
        "agent_token": "test-token",
        "memory_mb": 512,
        "cpu": 1.0,
        "pids_limit": 128,
        "idle_timeout_seconds": 60,
        "metadata_": {"template": "base"},
        "created_at": idle,
        "last_activity_at": idle,
    }
    values.update(overrides)
    return Sandbox(**values)


class PauseRuntime:
    """Pauses everything except the sandboxes named as already gone."""

    def __init__(self, missing: set[str]) -> None:
        self.missing = missing
        self.paused: list[str] = []

    async def pause(self, sandbox: Sandbox, *, memory: bool) -> None:  # noqa: ARG002
        if sandbox.id in self.missing:
            # Copied from the production log, so the test fails if the runtime
            # ever stops raising this for a container OpenSandbox has lost.
            message = (
                f"Get endpoint for sandbox {sandbox.id} failed: "
                f"Sandbox {sandbox.id} not found. | [DOCKER::SANDBOX_NOT_FOUND]"
            )
            raise SandboxUnavailableError(message)
        self.paused.append(sandbox.id)


async def test_a_vanished_sandbox_does_not_stop_the_others_being_paused(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure that mattered: one missing row aborted the whole pass.

    `_cold_pause_idle_sandboxes` raised out of the loop, so every sandbox after
    the missing one stayed running past its idle timeout, and
    `_sweep_unused_templates` -- which runs after it in the same iteration --
    never ran at all.
    """
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-gone"))
        session.add(sandbox_row(id="sbx-alive"))
        await session.commit()
    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    # SQLite drops tzinfo on the round trip where PostgreSQL keeps it, so the
    # stored timestamps come back naive and `utc_now()` is aware. Match them
    # here rather than change production behaviour for a harness difference.
    monkeypatch.setattr(
        scheduler_module, "utc_now", lambda: datetime.now(UTC).replace(tzinfo=None)
    )

    runtime = PauseRuntime(missing={"sbx-gone"})
    scheduler = scheduler_module.Scheduler(Settings(), runtime)  # type: ignore[arg-type]

    await scheduler._cold_pause_idle_sandboxes()

    assert "sbx-alive" in runtime.paused


async def test_a_vanished_sandbox_is_not_retried_every_poll(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the traceback repeats at the poll interval, forever.

    The row has to leave the set the planner considers; if it stays `running`
    the reaper picks it up again a second later and logs the same failure.
    """
    async with sessions() as session:
        session.add(sandbox_row(id="sbx-gone"))
        await session.commit()
    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    # SQLite drops tzinfo on the round trip where PostgreSQL keeps it, so the
    # stored timestamps come back naive and `utc_now()` is aware. Match them
    # here rather than change production behaviour for a harness difference.
    monkeypatch.setattr(
        scheduler_module, "utc_now", lambda: datetime.now(UTC).replace(tzinfo=None)
    )

    runtime = PauseRuntime(missing={"sbx-gone"})
    scheduler = scheduler_module.Scheduler(Settings(), runtime)  # type: ignore[arg-type]

    await scheduler._cold_pause_idle_sandboxes()
    await scheduler._cold_pause_idle_sandboxes()

    async with sessions() as session:
        row = await session.get(Sandbox, "sbx-gone")
    assert row is not None
    assert row.status not in {"running", "paused_memory"}
