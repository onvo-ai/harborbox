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

import pytest

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
