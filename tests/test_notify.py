"""Queue and completion wake-ups.

These cover the local half of `ExecutionNotifier` -- delivery within one
process, and the bookkeeping around waiters. The Postgres half needs a real
database (`LISTEN`/`NOTIFY` is not something SQLite can stand in for) and is
covered by the e2e suite.

The property worth protecting is not that notifications arrive; it is that
nothing breaks when they do not. Every wait is bounded, and every timeout puts
the caller back on the polling behaviour this replaced.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

import asyncpg
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

from harborbox.config import Settings
from harborbox.notify import ExecutionNotifier


def notifier(database_url: str = "sqlite+aiosqlite:///:memory:") -> ExecutionNotifier:
    return ExecutionNotifier(Settings(database_url=database_url))


async def test_a_queue_notification_wakes_the_waiter_immediately() -> None:
    """The whole point: the scheduler stops waiting on a clock."""
    events = notifier()
    waiter = asyncio.create_task(events.wait_for_queue(timeout=30))
    await asyncio.sleep(0)

    await events.notify_queued()
    # Would raise if the wake-up had not arrived well inside the fallback.
    await asyncio.wait_for(waiter, timeout=1)


async def test_waiting_for_the_queue_gives_up_at_the_timeout() -> None:
    """A missed notification must cost one interval, never a stall.

    This is the failure mode the whole design leans on: nothing here is
    load-bearing for correctness, so expiry has to be an ordinary return.
    """
    events = notifier()

    await asyncio.wait_for(events.wait_for_queue(timeout=0.05), timeout=1)


async def test_an_execution_notification_wakes_only_its_own_waiter() -> None:
    events = notifier()
    with events.execution_waiter("exec_a") as first, events.execution_waiter(
        "exec_b"
    ) as second:
        await events.notify_execution_finished("exec_a")

        assert first.is_set()
        assert not second.is_set()


async def test_a_notification_for_an_unwatched_execution_is_harmless() -> None:
    """Completions land constantly with nobody blocked on them."""
    events = notifier()

    await events.notify_execution_finished("exec_nobody_is_waiting_on")


async def test_two_waiters_on_one_execution_are_both_woken() -> None:
    """An SSE stream and an inline `wait=true` can watch the same execution."""
    events = notifier()
    with events.execution_waiter("exec_a") as stream, events.execution_waiter(
        "exec_a"
    ) as inline:
        await events.notify_execution_finished("exec_a")

        assert stream.is_set()
        assert inline.is_set()


async def test_the_waiter_registry_is_emptied_when_the_last_waiter_leaves() -> None:
    """Otherwise the registry grows by one entry per execution, forever."""
    events = notifier()
    with events.execution_waiter("exec_a"), events.execution_waiter("exec_a"):
        assert events._execution_events

    assert not events._execution_events
    assert not events._waiter_counts


async def test_a_nested_waiter_leaving_does_not_strand_the_outer_one() -> None:
    """Reference counting, not last-write-wins: the outer waiter still needs its event."""
    events = notifier()
    with events.execution_waiter("exec_a") as outer:
        with events.execution_waiter("exec_a"):
            pass
        await events.notify_execution_finished("exec_a")

        assert outer.is_set()


async def test_a_non_postgres_database_starts_without_a_listener() -> None:
    """SQLite has no LISTEN/NOTIFY; local delivery has to keep working anyway."""
    events = notifier()
    await events.start()
    try:
        with events.execution_waiter("exec_a") as waiter:
            await events.notify_execution_finished("exec_a")

            assert waiter.is_set()
    finally:
        await events.close()


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (
            "postgresql+asyncpg://user:pw@db:5432/harborbox",
            "postgresql://user:pw@db:5432/harborbox",
        ),
        (
            "postgresql+asyncpg://user:pw@db/harborbox?ssl=require",
            "postgresql://user:pw@db/harborbox",
        ),
    ],
)
def test_the_listener_dsn_drops_what_asyncpg_cannot_read(
    configured: str, expected: str
) -> None:
    """Drop what asyncpg cannot read from the configured URL.

    asyncpg understands neither the `+asyncpg` driver marker nor SQLAlchemy's
    own query arguments, and a malformed DSN would take the listener down on
    every reconnect rather than failing visibly at startup.
    """
    assert notifier(configured)._dsn() == expected


class FakePostgresConnection:
    """A stand-in that enforces asyncpg's real concurrency invariant.

    asyncpg connections are not safe for concurrent use: a second operation
    started while one is still in flight raises `InterfaceError`. That is the
    production failure this fake reproduces -- 12 of 12 error spans in the
    72-hour window after DEV-1948's instrumentation went live were
    `InterfaceError: cannot perform operation: another operation is in
    progress` on `SELECT pg_notify($1, $2)`, and every one of them landed
    while two or more requests were in flight.
    """

    # asyncpg's own wording, so a reader grepping the production error string
    # lands here.
    BUSY_MESSAGE = "cannot perform operation: another operation is in progress"

    def __init__(self) -> None:
        self.executed: list[tuple[object, ...]] = []
        self.listeners: dict[str, object] = {}
        self.closed = False
        self._busy = False

    async def execute(self, query: str, *args: object) -> None:
        if self._busy:
            raise asyncpg.InterfaceError(self.BUSY_MESSAGE)
        self._busy = True
        try:
            # The await is the point: it hands control back to the event loop
            # mid-operation, which is exactly the window a second caller slips
            # into against a shared connection.
            await asyncio.sleep(0)
            self.executed.append((query, *args))
        finally:
            self._busy = False

    async def add_listener(self, channel: str, callback: object) -> None:
        self.listeners[channel] = callback

    def add_termination_listener(self, _callback: object) -> None:
        return None

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True


class FakePool:
    """A pool that hands out one connection per concurrent caller, up to a bound.

    The bound matters: it is what makes the assertion below prove that emits
    past `max_size` queue for a connection rather than collide on one.
    """

    def __init__(
        self, max_size: int, factory: Callable[[], FakePostgresConnection]
    ) -> None:
        self._free = [factory() for _ in range(max_size)]
        self._slots = asyncio.Semaphore(max_size)
        self.closed = False

    def acquire(self) -> AbstractAsyncContextManager[FakePostgresConnection]:
        return self._lease()

    @contextlib.asynccontextmanager
    async def _lease(self) -> AsyncIterator[FakePostgresConnection]:
        await self._slots.acquire()
        connection = self._free.pop()
        try:
            yield connection
        finally:
            self._free.append(connection)
            self._slots.release()

    async def close(self) -> None:
        self.closed = True


async def test_concurrent_emits_all_reach_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent notifications must not knock each other off the connection.

    The API task emits on enqueue (`notify_queued`) and the scheduler task
    emits on completion (`notify_execution_finished`). They are separate
    asyncio tasks, so sharing one asyncpg connection between them loses
    notifications under exactly the concurrency the notifier exists to speed
    up. A dropped `NOTIFY` costs correctness nothing -- every waiter has a
    timeout -- but it costs the other replicas their wake-up, which is the
    whole point of the module.

    `_emit` suppresses the error by design, so the assertion is on delivery
    rather than on a raise: count what actually reached the wire.
    """
    connections: list[FakePostgresConnection] = []

    def fake_connection() -> FakePostgresConnection:
        connection = FakePostgresConnection()
        connections.append(connection)
        return connection

    async def fake_connect(*_args: object, **_kwargs: object) -> FakePostgresConnection:
        return fake_connection()

    async def fake_create_pool(
        *_args: object, max_size: int = 1, **_kwargs: object
    ) -> FakePool:
        return FakePool(max_size, fake_connection)

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)

    events = notifier("postgresql+asyncpg://user:pw@db:5432/harborbox")
    await events.start()
    # Let the listener task dial before anything is emitted.
    for _ in range(10):
        await asyncio.sleep(0)

    emits = 20
    try:
        await asyncio.gather(
            *(events.notify_execution_finished(f"exec_{index}") for index in range(emits))
        )
    finally:
        await events.close()

    delivered = [
        statement for connection in connections for statement in connection.executed
    ]
    assert len(delivered) == emits, (
        f"{emits - len(delivered)} of {emits} notifications were dropped by a "
        "collision on a shared connection"
    )
