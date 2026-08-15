from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from opensandbox.exceptions import (
    PoolDestroyedException,
    PoolStateStoreUnavailableException,
)
from opensandbox.pool_types import (
    IdleEntry,
    PoolDestroyState,
    StoreCounters,
    TakeIdleResult,
)
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from harborbox.models import WarmPoolIdleSandbox, WarmPoolState

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class AsyncPostgresPoolStateStore:
    """OpenSandbox pool coordination using Harborbox's existing PostgreSQL."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._default_idle_ttl = timedelta(hours=24)

    @asynccontextmanager
    async def _operation(self, operation: str, pool_name: str) -> AsyncIterator[None]:
        try:
            yield
        except (ValueError, PoolDestroyedException):
            raise
        except Exception as exc:
            message = (
                "PostgreSQL pool state store operation failed: "
                f"operation={operation} pool_name={pool_name}"
            )
            raise PoolStateStoreUnavailableException(message, exc) from exc

    async def _locked_state(self, session: AsyncSession, pool_name: str) -> WarmPoolState:
        self._validate_pool_name(pool_name)
        await session.execute(
            insert(WarmPoolState)
            .values(pool_name=pool_name)
            .on_conflict_do_nothing(index_elements=[WarmPoolState.pool_name])
        )
        state = await session.get(WarmPoolState, pool_name, with_for_update=True)
        if state is None:  # pragma: no cover - protected by the upsert above
            message = f"could not initialize warm pool state: {pool_name}"
            raise RuntimeError(message)
        now = datetime.now(UTC)
        if (
            state.destroy_state == PoolDestroyState.DESTROYED.value
            and state.destroy_expires_at is not None
            and state.destroy_expires_at <= now
        ):
            state.destroy_state = PoolDestroyState.ACTIVE.value
            state.destroy_owner = None
            state.destroy_expires_at = None
        return state

    @staticmethod
    def _require_active(state: WarmPoolState) -> None:
        if state.destroy_state != PoolDestroyState.ACTIVE.value:
            message = f"Pool namespace is {state.destroy_state}: pool_name={state.pool_name}"
            raise PoolDestroyedException(message)

    async def try_take_idle(self, pool_name: str) -> str | None:
        return (await self._take_idle(pool_name, timedelta(0))).sandbox_id

    async def try_take_idle_min_ttl(
        self, pool_name: str, min_remaining_ttl: timedelta
    ) -> TakeIdleResult:
        if min_remaining_ttl.total_seconds() <= 0:
            return TakeIdleResult(sandbox_id=await self.try_take_idle(pool_name))
        return await self._take_idle(pool_name, min_remaining_ttl)

    async def _take_idle(self, pool_name: str, min_remaining_ttl: timedelta) -> TakeIdleResult:
        async with (
            self._operation("try_take_idle", pool_name),
            self._sessions.begin() as session,
        ):
            state = await self._locked_state(session, pool_name)
            self._require_active(state)
            now = datetime.now(UTC)
            threshold = now + min_remaining_ttl
            rows = list(
                (
                    await session.scalars(
                        select(WarmPoolIdleSandbox)
                        .where(WarmPoolIdleSandbox.pool_name == pool_name)
                        .order_by(
                            WarmPoolIdleSandbox.created_at,
                            WarmPoolIdleSandbox.id,
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            discarded: list[str] = []
            selected: str | None = None
            for row in rows:
                if row.expires_at <= threshold:
                    if row.expires_at > now:
                        discarded.append(row.sandbox_id)
                    await session.delete(row)
                    continue
                selected = row.sandbox_id
                await session.delete(row)
                break
            return TakeIdleResult(selected, tuple(discarded))

    async def put_idle(self, pool_name: str, sandbox_id: str) -> None:
        if not sandbox_id or not sandbox_id.strip():
            message = "sandbox_id must not be blank"
            raise ValueError(message)
        async with (
            self._operation("put_idle", pool_name),
            self._sessions.begin() as session,
        ):
            state = await self._locked_state(session, pool_name)
            self._require_active(state)
            now = datetime.now(UTC)
            existing = await session.scalar(
                select(WarmPoolIdleSandbox)
                .where(WarmPoolIdleSandbox.sandbox_id == sandbox_id)
                .with_for_update()
            )
            if existing is None:
                session.add(
                    WarmPoolIdleSandbox(
                        pool_name=pool_name,
                        sandbox_id=sandbox_id,
                        created_at=now,
                        expires_at=now + timedelta(seconds=state.idle_ttl_seconds),
                    )
                )
            else:
                existing.pool_name = pool_name
                existing.created_at = now
                existing.expires_at = now + timedelta(seconds=state.idle_ttl_seconds)

    async def remove_idle(self, pool_name: str, sandbox_id: str) -> None:
        async with (
            self._operation("remove_idle", pool_name),
            self._sessions.begin() as session,
        ):
            await session.execute(
                delete(WarmPoolIdleSandbox).where(
                    WarmPoolIdleSandbox.pool_name == pool_name,
                    WarmPoolIdleSandbox.sandbox_id == sandbox_id,
                )
            )

    async def try_acquire_primary_lock(self, pool_name: str, owner_id: str, ttl: timedelta) -> bool:
        self._validate_owner_ttl(owner_id, ttl)
        async with (
            self._operation("try_acquire_primary_lock", pool_name),
            self._sessions.begin() as session,
        ):
            state = await self._locked_state(session, pool_name)
            self._require_active(state)
            now = datetime.now(UTC)
            if (
                state.primary_owner is not None
                and state.primary_owner != owner_id
                and state.primary_expires_at is not None
                and state.primary_expires_at > now
            ):
                return False
            state.primary_owner = owner_id
            state.primary_expires_at = now + ttl
            return True

    async def renew_primary_lock(self, pool_name: str, owner_id: str, ttl: timedelta) -> bool:
        self._validate_owner_ttl(owner_id, ttl)
        async with (
            self._operation("renew_primary_lock", pool_name),
            self._sessions.begin() as session,
        ):
            state = await self._locked_state(session, pool_name)
            self._require_active(state)
            now = datetime.now(UTC)
            if (
                state.primary_owner != owner_id
                or state.primary_expires_at is None
                or state.primary_expires_at <= now
            ):
                return False
            state.primary_expires_at = now + ttl
            return True

    async def release_primary_lock(self, pool_name: str, owner_id: str) -> None:
        async with (
            self._operation("release_primary_lock", pool_name),
            self._sessions.begin() as session,
        ):
            state = await self._locked_state(session, pool_name)
            if state.primary_owner == owner_id:
                state.primary_owner = None
                state.primary_expires_at = None

    async def reap_expired_idle(self, pool_name: str, now: datetime) -> None:
        await self._reap_idle(pool_name, now, timedelta(0))

    async def reap_expired_idle_min_ttl(
        self, pool_name: str, now: datetime, min_remaining_ttl: timedelta
    ) -> tuple[str, ...]:
        if min_remaining_ttl.total_seconds() <= 0:
            await self.reap_expired_idle(pool_name, now)
            return ()
        return await self._reap_idle(pool_name, now, min_remaining_ttl)

    async def _reap_idle(
        self, pool_name: str, now: datetime, min_remaining_ttl: timedelta
    ) -> tuple[str, ...]:
        async with (
            self._operation("reap_expired_idle", pool_name),
            self._sessions.begin() as session,
        ):
            threshold = now + min_remaining_ttl
            rows = list(
                (
                    await session.scalars(
                        select(WarmPoolIdleSandbox)
                        .where(
                            WarmPoolIdleSandbox.pool_name == pool_name,
                            WarmPoolIdleSandbox.expires_at <= threshold,
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            alive = tuple(row.sandbox_id for row in rows if row.expires_at > now)
            for row in rows:
                await session.delete(row)
            return alive

    async def snapshot_counters(self, pool_name: str) -> StoreCounters:
        async with (
            self._operation("snapshot_counters", pool_name),
            self._sessions() as session,
        ):
            count = await session.scalar(
                select(func.count(WarmPoolIdleSandbox.id)).where(
                    WarmPoolIdleSandbox.pool_name == pool_name
                )
            )
            return StoreCounters(idle_count=int(count or 0))

    async def snapshot_idle_entries(self, pool_name: str) -> list[IdleEntry]:
        async with (
            self._operation("snapshot_idle_entries", pool_name),
            self._sessions() as session,
        ):
            rows = (
                await session.scalars(
                    select(WarmPoolIdleSandbox)
                    .where(WarmPoolIdleSandbox.pool_name == pool_name)
                    .order_by(
                        WarmPoolIdleSandbox.created_at,
                        WarmPoolIdleSandbox.id,
                    )
                )
            ).all()
            return [IdleEntry(row.sandbox_id, row.expires_at) for row in rows]

    async def get_max_idle(self, pool_name: str) -> int | None:
        async with (
            self._operation("get_max_idle", pool_name),
            self._sessions.begin() as session,
        ):
            return (await self._locked_state(session, pool_name)).max_idle

    async def set_max_idle(self, pool_name: str, max_idle: int) -> None:
        if max_idle < 0:
            message = "max_idle must be >= 0"
            raise ValueError(message)
        async with (
            self._operation("set_max_idle", pool_name),
            self._sessions.begin() as session,
        ):
            state = await self._locked_state(session, pool_name)
            self._require_active(state)
            state.max_idle = max_idle

    async def set_idle_entry_ttl(self, pool_name: str, idle_ttl: timedelta) -> None:
        if idle_ttl.total_seconds() <= 0:
            message = "idle_ttl must be positive"
            raise ValueError(message)
        async with (
            self._operation("set_idle_entry_ttl", pool_name),
            self._sessions.begin() as session,
        ):
            state = await self._locked_state(session, pool_name)
            self._require_active(state)
            state.idle_ttl_seconds = idle_ttl.total_seconds()

    async def get_destroy_state(self, pool_name: str) -> PoolDestroyState:
        async with (
            self._operation("get_destroy_state", pool_name),
            self._sessions.begin() as session,
        ):
            state = await self._locked_state(session, pool_name)
            return PoolDestroyState(state.destroy_state)

    async def begin_destroy(self, pool_name: str, owner_id: str) -> None:
        self._validate_owner(owner_id)
        async with (
            self._operation("begin_destroy", pool_name),
            self._sessions.begin() as session,
        ):
            state = await self._locked_state(session, pool_name)
            if state.destroy_state == PoolDestroyState.DESTROYED.value:
                message = f"Pool namespace is already DESTROYED: pool_name={pool_name}"
                raise PoolDestroyedException(message)
            state.destroy_state = PoolDestroyState.DESTROYING.value
            state.destroy_owner = owner_id
            state.destroy_expires_at = None

    async def clear_pool_state(self, pool_name: str) -> None:
        async with (
            self._operation("clear_pool_state", pool_name),
            self._sessions.begin() as session,
        ):
            state = await self._locked_state(session, pool_name)
            await session.execute(
                delete(WarmPoolIdleSandbox).where(WarmPoolIdleSandbox.pool_name == pool_name)
            )
            state.max_idle = None
            state.idle_ttl_seconds = self._default_idle_ttl.total_seconds()
            state.primary_owner = None
            state.primary_expires_at = None

    async def mark_destroyed(
        self, pool_name: str, owner_id: str, tombstone_ttl: timedelta | None
    ) -> None:
        self._validate_owner(owner_id)
        if tombstone_ttl is not None and tombstone_ttl.total_seconds() <= 0:
            message = "tombstone_ttl must be positive"
            raise ValueError(message)
        async with (
            self._operation("mark_destroyed", pool_name),
            self._sessions.begin() as session,
        ):
            state = await self._locked_state(session, pool_name)
            state.destroy_state = PoolDestroyState.DESTROYED.value
            state.destroy_owner = owner_id
            state.destroy_expires_at = (
                datetime.now(UTC) + tombstone_ttl if tombstone_ttl is not None else None
            )

    @staticmethod
    def _validate_pool_name(pool_name: str) -> None:
        if not pool_name or not pool_name.strip():
            message = "pool_name must not be blank"
            raise ValueError(message)

    @staticmethod
    def _validate_owner(owner_id: str) -> None:
        if not owner_id or not owner_id.strip():
            message = "owner_id must not be blank"
            raise ValueError(message)

    @classmethod
    def _validate_owner_ttl(cls, owner_id: str, ttl: timedelta) -> None:
        cls._validate_owner(owner_id)
        if ttl.total_seconds() <= 0:
            message = "ttl must be positive"
            raise ValueError(message)
