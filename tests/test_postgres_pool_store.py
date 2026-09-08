"""Behaviour of the OpenSandbox pool state store backed by Harborbox's Postgres.

These tests need a real PostgreSQL: the store uses
``dialects.postgresql.insert(...).on_conflict_do_nothing`` and
``with_for_update(skip_locked=True)``, neither of which SQLite can execute, and a
fake session would only assert that the queries we wrote are the queries we
wrote. CI provisions ``postgres:17-alpine`` and points
``HARBORBOX_TEST_DATABASE_URL`` at it; without that variable the module skips.

Anything that has to observe a deadline passing uses a one-millisecond TTL and a
fifty-millisecond sleep -- a 50x margin, rather than a wall-clock assertion that
a loaded runner can lose.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from opensandbox.exceptions import (
    PoolDestroyedException,
    PoolStateStoreUnavailableException,
)
from opensandbox.pool_types import PoolDestroyState
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from harborbox.db import Base
from harborbox.models import WarmPoolIdleSandbox, WarmPoolState
from harborbox.postgres_pool_store import AsyncPostgresPoolStateStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.postgres

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
# Short enough that a sleep can outlast it, long enough to survive scheduling jitter.
BLINK = timedelta(milliseconds=1)
BLINK_SLEEP_SECONDS = 0.05
DEFAULT_IDLE_TTL = timedelta(hours=24)


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("HARBORBOX_TEST_DATABASE_URL")
    if not url:
        pytest.skip("HARBORBOX_TEST_DATABASE_URL is not set; needs a real PostgreSQL")
    return url


@pytest.fixture
async def sessions(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session factory over the warm-pool tables, wired like `harborbox.db`."""
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[WarmPoolState.__table__, WarmPoolIdleSandbox.__table__],
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def store(sessions: async_sessionmaker[AsyncSession]) -> AsyncPostgresPoolStateStore:
    return AsyncPostgresPoolStateStore(sessions)


@pytest.fixture
def pool() -> str:
    """Name a pool namespace nothing else touches, so tests can share one database."""
    return f"pool-{uuid4().hex}"


def sandbox_id(pool_name: str, suffix: str) -> str:
    """Namespace a sandbox id: the column is globally unique, not per-pool."""
    return f"{pool_name}-{suffix}"


async def test_first_touch_creates_the_pool_namespace_with_defaults(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    assert await store.get_max_idle(pool) is None
    assert await store.get_destroy_state(pool) is PoolDestroyState.ACTIVE
    assert (await store.snapshot_counters(pool)).idle_count == 0


async def test_a_blank_pool_name_is_rejected_rather_than_reported_as_an_outage(
    store: AsyncPostgresPoolStateStore,
) -> None:
    """ValueError is a caller bug and must survive the store's exception wrapping."""
    with pytest.raises(ValueError, match="pool_name must not be blank"):
        await store.try_take_idle("   ")


async def test_a_database_failure_is_reported_as_the_store_being_unavailable(
    database_url: str, pool: str
) -> None:
    unreachable = create_async_engine(make_url(database_url).set(database="harborbox_absent_db"))
    store = AsyncPostgresPoolStateStore(async_sessionmaker(unreachable, expire_on_commit=False))
    try:
        with pytest.raises(PoolStateStoreUnavailableException, match="snapshot_counters"):
            await store.snapshot_counters(pool)
    finally:
        await unreachable.dispose()


async def test_an_idle_sandbox_can_be_put_back_and_taken_again(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    only = sandbox_id(pool, "only")
    await store.put_idle(pool, only)

    assert (await store.snapshot_counters(pool)).idle_count == 1
    assert await store.try_take_idle(pool) == only
    assert (await store.snapshot_counters(pool)).idle_count == 0


async def test_taking_from_an_empty_pool_returns_nothing(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    assert await store.try_take_idle(pool) is None


async def test_idle_sandboxes_are_handed_out_oldest_first(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    first, second, third = (sandbox_id(pool, s) for s in ("first", "second", "third"))
    for name in (first, second, third):
        await store.put_idle(pool, name)

    taken = [await store.try_take_idle(pool) for _ in range(3)]

    assert taken == [first, second, third]


async def test_putting_a_blank_sandbox_id_is_rejected(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    with pytest.raises(ValueError, match="sandbox_id must not be blank"):
        await store.put_idle(pool, "  ")


async def test_putting_a_known_sandbox_back_refreshes_it_instead_of_duplicating_it(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    returning = sandbox_id(pool, "returning")
    await store.set_idle_entry_ttl(pool, MINUTE)
    await store.put_idle(pool, returning)
    (first_entry,) = await store.snapshot_idle_entries(pool)

    await store.set_idle_entry_ttl(pool, HOUR)
    await store.put_idle(pool, returning)
    entries = await store.snapshot_idle_entries(pool)

    assert [entry.sandbox_id for entry in entries] == [returning]
    assert entries[0].expires_at > first_entry.expires_at


async def test_putting_a_sandbox_under_a_second_pool_moves_it_there(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    """One sandbox belongs to one pool: the row moves rather than being cloned."""
    other = f"{pool}-other"
    migrant = sandbox_id(pool, "migrant")
    await store.put_idle(pool, migrant)

    await store.put_idle(other, migrant)

    assert (await store.snapshot_counters(pool)).idle_count == 0
    assert [entry.sandbox_id for entry in await store.snapshot_idle_entries(other)] == [migrant]


async def test_the_configured_idle_ttl_decides_when_an_entry_expires(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    await store.set_idle_entry_ttl(pool, HOUR)

    before = datetime.now(UTC)
    await store.put_idle(pool, sandbox_id(pool, "ttl"))
    (entry,) = await store.snapshot_idle_entries(pool)

    assert before + HOUR <= entry.expires_at <= datetime.now(UTC) + HOUR


async def test_removing_an_idle_sandbox_leaves_the_rest_of_the_pool_alone(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    kept, dropped = sandbox_id(pool, "kept"), sandbox_id(pool, "dropped")
    await store.put_idle(pool, kept)
    await store.put_idle(pool, dropped)

    await store.remove_idle(pool, dropped)
    # Removing what is already gone is how the pool recovers from a lost sandbox,
    # so it must not raise.
    await store.remove_idle(pool, dropped)

    assert [entry.sandbox_id for entry in await store.snapshot_idle_entries(pool)] == [kept]


async def test_taking_with_a_minimum_ttl_skips_and_reports_soon_expiring_sandboxes(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    """A sandbox too close to its deadline is still alive, so the caller is told to kill it."""
    soon, fresh = sandbox_id(pool, "soon"), sandbox_id(pool, "fresh")
    await store.set_idle_entry_ttl(pool, MINUTE)
    await store.put_idle(pool, soon)
    await store.set_idle_entry_ttl(pool, HOUR)
    await store.put_idle(pool, fresh)

    result = await store.try_take_idle_min_ttl(pool, timedelta(minutes=5))

    assert result.sandbox_id == fresh
    assert result.discarded_alive_sandbox_ids == (soon,)
    assert (await store.snapshot_counters(pool)).idle_count == 0


async def test_taking_with_no_minimum_ttl_is_a_plain_take(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    only = sandbox_id(pool, "only")
    await store.put_idle(pool, only)

    result = await store.try_take_idle_min_ttl(pool, timedelta(0))

    assert result.sandbox_id == only
    assert result.discarded_alive_sandbox_ids == ()


async def test_an_already_expired_sandbox_is_dropped_without_being_reported_as_alive(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    """The server has already reaped it; telling the caller to kill it is a wasted call."""
    await store.set_idle_entry_ttl(pool, BLINK)
    await store.put_idle(pool, sandbox_id(pool, "expired"))
    await asyncio.sleep(BLINK_SLEEP_SECONDS)

    result = await store.try_take_idle_min_ttl(pool, MINUTE)

    assert result.sandbox_id is None
    assert result.discarded_alive_sandbox_ids == ()
    assert (await store.snapshot_counters(pool)).idle_count == 0


async def test_the_primary_lock_is_exclusive_and_reentrant_for_its_holder(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    assert await store.try_acquire_primary_lock(pool, "owner-a", HOUR) is True
    assert await store.try_acquire_primary_lock(pool, "owner-b", HOUR) is False
    assert await store.try_acquire_primary_lock(pool, "owner-a", HOUR) is True


async def test_an_expired_primary_lock_falls_to_the_next_claimant(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    await store.try_acquire_primary_lock(pool, "owner-a", BLINK)
    await asyncio.sleep(BLINK_SLEEP_SECONDS)

    assert await store.try_acquire_primary_lock(pool, "owner-b", HOUR) is True


async def test_only_the_holder_can_renew_the_primary_lock(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    await store.try_acquire_primary_lock(pool, "owner-a", HOUR)

    assert await store.renew_primary_lock(pool, "owner-a", HOUR) is True
    assert await store.renew_primary_lock(pool, "owner-b", HOUR) is False


async def test_a_lapsed_primary_lock_cannot_be_renewed_back_into_life(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    await store.try_acquire_primary_lock(pool, "owner-a", BLINK)
    await asyncio.sleep(BLINK_SLEEP_SECONDS)

    assert await store.renew_primary_lock(pool, "owner-a", HOUR) is False
    assert await store.try_acquire_primary_lock(pool, "owner-b", HOUR) is True


async def test_renewing_a_lock_nobody_holds_is_refused(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    assert await store.renew_primary_lock(pool, "owner-a", HOUR) is False


async def test_releasing_the_primary_lock_only_works_for_its_holder(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    await store.try_acquire_primary_lock(pool, "owner-a", HOUR)

    await store.release_primary_lock(pool, "owner-b")
    assert await store.try_acquire_primary_lock(pool, "owner-c", HOUR) is False

    await store.release_primary_lock(pool, "owner-a")
    assert await store.try_acquire_primary_lock(pool, "owner-c", HOUR) is True


@pytest.mark.parametrize(
    ("owner_id", "ttl", "message"),
    [
        ("", HOUR, "owner_id must not be blank"),
        ("   ", HOUR, "owner_id must not be blank"),
        ("owner-a", timedelta(0), "ttl must be positive"),
        ("owner-a", -HOUR, "ttl must be positive"),
    ],
)
async def test_lock_calls_reject_a_blank_owner_or_a_non_positive_ttl(
    store: AsyncPostgresPoolStateStore, pool: str, owner_id: str, ttl: timedelta, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await store.try_acquire_primary_lock(pool, owner_id, ttl)
    with pytest.raises(ValueError, match=message):
        await store.renew_primary_lock(pool, owner_id, ttl)


async def test_reaping_removes_expired_entries_and_keeps_live_ones(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    stale, live = sandbox_id(pool, "stale"), sandbox_id(pool, "live")
    await store.set_idle_entry_ttl(pool, BLINK)
    await store.put_idle(pool, stale)
    await asyncio.sleep(BLINK_SLEEP_SECONDS)
    await store.set_idle_entry_ttl(pool, HOUR)
    await store.put_idle(pool, live)

    await store.reap_expired_idle(pool, datetime.now(UTC))

    assert [entry.sandbox_id for entry in await store.snapshot_idle_entries(pool)] == [live]


async def test_reaping_with_a_minimum_ttl_reports_the_still_alive_entries_it_removed(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    soon, fresh = sandbox_id(pool, "soon"), sandbox_id(pool, "fresh")
    await store.set_idle_entry_ttl(pool, MINUTE)
    await store.put_idle(pool, soon)
    await store.set_idle_entry_ttl(pool, HOUR)
    await store.put_idle(pool, fresh)

    reaped = await store.reap_expired_idle_min_ttl(pool, datetime.now(UTC), timedelta(minutes=5))

    assert reaped == (soon,)
    assert [entry.sandbox_id for entry in await store.snapshot_idle_entries(pool)] == [fresh]


async def test_reaping_with_no_minimum_ttl_reports_nothing_to_kill(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    await store.set_idle_entry_ttl(pool, BLINK)
    await store.put_idle(pool, sandbox_id(pool, "expired"))
    await asyncio.sleep(BLINK_SLEEP_SECONDS)

    reaped = await store.reap_expired_idle_min_ttl(pool, datetime.now(UTC), timedelta(0))

    assert reaped == ()
    assert (await store.snapshot_counters(pool)).idle_count == 0


async def test_snapshots_are_scoped_to_one_pool(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    neighbour = f"{pool}-neighbour"
    mine = sandbox_id(pool, "mine")
    await store.put_idle(pool, mine)
    await store.put_idle(neighbour, sandbox_id(neighbour, "theirs"))

    assert (await store.snapshot_counters(pool)).idle_count == 1
    assert [entry.sandbox_id for entry in await store.snapshot_idle_entries(pool)] == [mine]


async def test_max_idle_round_trips(store: AsyncPostgresPoolStateStore, pool: str) -> None:
    target = 3
    await store.set_max_idle(pool, target)

    assert await store.get_max_idle(pool) == target


@pytest.mark.parametrize(
    ("max_idle", "message"),
    [(-1, "max_idle must be >= 0"), (-10, "max_idle must be >= 0")],
)
async def test_a_negative_max_idle_is_rejected(
    store: AsyncPostgresPoolStateStore, pool: str, max_idle: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await store.set_max_idle(pool, max_idle)


@pytest.mark.parametrize("idle_ttl", [timedelta(0), -HOUR])
async def test_a_non_positive_idle_ttl_is_rejected(
    store: AsyncPostgresPoolStateStore, pool: str, idle_ttl: timedelta
) -> None:
    with pytest.raises(ValueError, match="idle_ttl must be positive"):
        await store.set_idle_entry_ttl(pool, idle_ttl)


async def test_beginning_a_destroy_freezes_the_pool(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    await store.begin_destroy(pool, "owner-a")

    assert await store.get_destroy_state(pool) is PoolDestroyState.DESTROYING
    with pytest.raises(PoolDestroyedException, match="DESTROYING"):
        await store.put_idle(pool, sandbox_id(pool, "late"))
    with pytest.raises(PoolDestroyedException, match="DESTROYING"):
        await store.try_take_idle(pool)
    with pytest.raises(PoolDestroyedException, match="DESTROYING"):
        await store.try_acquire_primary_lock(pool, "owner-a", HOUR)
    with pytest.raises(PoolDestroyedException, match="DESTROYING"):
        await store.set_max_idle(pool, 1)


async def test_a_destroyed_pool_refuses_a_second_destroy(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    await store.begin_destroy(pool, "owner-a")
    await store.mark_destroyed(pool, "owner-a", None)

    assert await store.get_destroy_state(pool) is PoolDestroyState.DESTROYED
    with pytest.raises(PoolDestroyedException, match="already DESTROYED"):
        await store.begin_destroy(pool, "owner-a")


async def test_a_destroy_tombstone_expires_back_into_an_active_pool(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    """The tombstone stops the name being reused immediately, not forever."""
    await store.mark_destroyed(pool, "owner-a", BLINK)
    await asyncio.sleep(BLINK_SLEEP_SECONDS)

    assert await store.get_destroy_state(pool) is PoolDestroyState.ACTIVE

    reborn = sandbox_id(pool, "reborn")
    await store.put_idle(pool, reborn)
    assert await store.try_take_idle(pool) == reborn


async def test_a_tombstone_with_no_ttl_never_expires(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    await store.mark_destroyed(pool, "owner-a", None)
    await asyncio.sleep(BLINK_SLEEP_SECONDS)

    assert await store.get_destroy_state(pool) is PoolDestroyState.DESTROYED


@pytest.mark.parametrize("owner_id", ["", "   "])
async def test_destroy_calls_reject_a_blank_owner(
    store: AsyncPostgresPoolStateStore, pool: str, owner_id: str
) -> None:
    with pytest.raises(ValueError, match="owner_id must not be blank"):
        await store.begin_destroy(pool, owner_id)
    with pytest.raises(ValueError, match="owner_id must not be blank"):
        await store.mark_destroyed(pool, owner_id, HOUR)


@pytest.mark.parametrize("tombstone_ttl", [timedelta(0), -HOUR])
async def test_a_non_positive_tombstone_ttl_is_rejected(
    store: AsyncPostgresPoolStateStore, pool: str, tombstone_ttl: timedelta
) -> None:
    with pytest.raises(ValueError, match="tombstone_ttl must be positive"):
        await store.mark_destroyed(pool, "owner-a", tombstone_ttl)


async def test_clearing_pool_state_drains_the_idle_set_and_restores_the_defaults(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    await store.set_max_idle(pool, 5)
    await store.set_idle_entry_ttl(pool, MINUTE)
    await store.try_acquire_primary_lock(pool, "owner-a", HOUR)
    await store.put_idle(pool, sandbox_id(pool, "drained"))

    await store.clear_pool_state(pool)

    assert (await store.snapshot_counters(pool)).idle_count == 0
    assert await store.get_max_idle(pool) is None
    assert await store.try_acquire_primary_lock(pool, "owner-b", HOUR) is True

    before = datetime.now(UTC)
    await store.put_idle(pool, sandbox_id(pool, "after-clear"))
    (entry,) = await store.snapshot_idle_entries(pool)
    assert entry.expires_at >= before + DEFAULT_IDLE_TTL


async def test_clearing_pool_state_works_on_a_pool_that_is_being_destroyed(
    store: AsyncPostgresPoolStateStore, pool: str
) -> None:
    """Teardown has to run after `begin_destroy`, which is what freezes the pool."""
    await store.put_idle(pool, sandbox_id(pool, "doomed"))
    await store.begin_destroy(pool, "owner-a")

    await store.clear_pool_state(pool)

    assert (await store.snapshot_counters(pool)).idle_count == 0
