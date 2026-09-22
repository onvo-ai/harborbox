"""DEV-2400: a session held open across an unrelated await doubles pool use.

The idle-sandbox reaper hit `QueuePool limit of size 5 overflow 10 reached` in
production with all 15 connections checked out at once. The query that timed
out (`_cold_pause_idle_sandboxes`'s own read) does not itself hold a
connection across anything slow -- the exhaustion came from elsewhere.

Two shapes of the same bug, both fixed here:

* `Scheduler._admit_available_jobs` called `self.capacity()` -- two HTTP
  round trips to OpenSandbox, only then a DB session -- while still holding
  its own FOR-UPDATE-locked session open. `capacity()` is now read before
  that session opens.
* `Scheduler.capacity()` always opened its own session even when the caller
  already had one open (four `/v1` handlers did exactly that), so every such
  request checked out two pool connections for one capacity check. `capacity`
  now takes an optional `session` so a caller with one open can reuse it.

These tests assert the checkout shape directly -- how many sessions are open
*at once* -- rather than just that the code still produces the right answer,
since the bug never changed the answer, only how much of the pool it held
while computing it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from harborbox import scheduler as scheduler_module
from harborbox.config import Settings
from harborbox.models import Base
from harborbox.runtime_protocol import WarmPoolReservation

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]


class FakeRuntime:
    """The subset of `SandboxRuntime` that `capacity()` touches."""

    def __init__(self, *, total_memory_mb: int = 8192) -> None:
        self._total_memory_mb = total_memory_mb

    def warm_pool_reservation(self) -> WarmPoolReservation:
        return WarmPoolReservation()

    async def total_memory_mb(self) -> int:
        return self._total_memory_mb

    async def available_memory_mb(self) -> int:
        return self._total_memory_mb


class ConcurrencyTrackingSessions:
    """Wraps a real sessionmaker and records how many sessions were open at once.

    A caller that nests one `session_factory()` block inside another -- the
    exact shape of this bug -- makes `max_concurrent` climb past 1, even
    though both blocks individually behave correctly and return the right
    data. That is what these tests check for.
    """

    def __init__(self, real: Sessions) -> None:
        self._real = real
        self.max_concurrent = 0
        self.opened = 0
        self._current = 0

    def __call__(self) -> Any:  # noqa: ANN401 - mirrors async_sessionmaker's own untyped __call__
        return self._track()

    @asynccontextmanager
    async def _track(self) -> AsyncIterator[AsyncSession]:
        self._current += 1
        self.opened += 1
        self.max_concurrent = max(self.max_concurrent, self._current)
        try:
            async with self._real() as session:
                yield session
        finally:
            self._current -= 1


@pytest.fixture
async def tracking_sessions() -> AsyncIterator[ConcurrencyTrackingSessions]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    real = async_sessionmaker(engine, expire_on_commit=False)
    yield ConcurrencyTrackingSessions(real)
    await engine.dispose()


async def test_capacity_with_no_session_opens_exactly_one(
    tracking_sessions: ConcurrencyTrackingSessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "session_factory", tracking_sessions)
    scheduler = scheduler_module.Scheduler(Settings(), FakeRuntime())  # type: ignore[arg-type]

    await scheduler.capacity()

    assert tracking_sessions.opened == 1
    assert tracking_sessions.max_concurrent == 1


async def test_capacity_reuses_a_passed_session_instead_of_opening_a_second(
    tracking_sessions: ConcurrencyTrackingSessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The four `/v1` handlers' shape: a session already open, then capacity()."""
    monkeypatch.setattr(scheduler_module, "session_factory", tracking_sessions)
    scheduler = scheduler_module.Scheduler(Settings(), FakeRuntime())  # type: ignore[arg-type]

    async with tracking_sessions() as caller_session:
        await scheduler.capacity(caller_session)

    # The caller's own session, and nothing else -- capacity() must not have
    # opened a second one on top of it.
    assert tracking_sessions.opened == 1
    assert tracking_sessions.max_concurrent == 1


async def test_admit_available_jobs_never_holds_two_sessions_at_once(
    tracking_sessions: ConcurrencyTrackingSessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduler-loop shape: a FOR-UPDATE session, then capacity() inside it.

    Regression test for the exact DEV-2400 traceback: this loop runs every
    `scheduler_poll_seconds` regardless of traffic, so nesting capacity()'s
    two HTTP round trips (and, before the fix, its own second session) inside
    this session's row lock was a constant, unconditional source of held
    connections -- with or without anything actually queued, which is why
    this test leaves the queue empty rather than needing a real admission to
    exercise the shape.
    """
    monkeypatch.setattr(scheduler_module, "session_factory", tracking_sessions)
    scheduler = scheduler_module.Scheduler(Settings(), FakeRuntime())  # type: ignore[arg-type]

    await scheduler._admit_available_jobs()

    # capacity() opens its own session, and the admission scan opens a
    # second, separate one -- but never both at the same time.
    sessions_opened_by_capacity_and_admission = 2
    assert tracking_sessions.opened == sessions_opened_by_capacity_and_admission
    assert tracking_sessions.max_concurrent == 1
