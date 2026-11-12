"""Reclaim sandboxes nothing will ever come back for.

Harborbox had no sweep of any kind. Sandboxes that failed to start stayed in
`created` and rows for `failed` sandboxes stayed forever — production was
carrying rows from five days earlier, and the local instance had accumulated
enough noise to make a real fault ("which of these is my stuck one?") genuinely
hard to read.

Three distinct leaks, deliberately handled differently:

* **`created` that never started.** A sandbox is created, then started lazily by
  its first execution. If that start fails — a bad image, no capacity, a Docker
  refusal — the row sits in `created` indefinitely. These are deleted, because
  the runtime may hold a half-built container behind them.

* **`failed` that already finished.** These hold no runtime resources; they are
  only a record. They are pruned on a longer horizon so a recent failure is
  still there to be inspected, which is exactly when someone wants it.

* **`starting` that never finished starting.** Unlike the two states above,
  `starting` *is* a `RESERVED_SANDBOX_STATES` member — it holds real memory
  and CPU reservation. `Scheduler._ensure_running` is the primary fix for
  this (it marks a sandbox `failed` itself the moment its own start errors,
  regardless of whether the original caller is still around to notice), but
  this sweep is the backstop for whatever gets past that -- a process
  restart mid-start is already recovered by `Scheduler._recover_interrupted_jobs`
  on boot, so what is left for this to catch is narrow, but "the one thing
  that holds a capacity reservation forever" is exactly the kind of leak
  that deserves a second layer.

`created` and `failed` reserve nothing (see `RESERVED_SANDBOX_STATES`), so
reclaiming those is housekeeping rather than a capacity fix — the capacity
bug they were once mistaken for is guarded in
`Settings.validate_warm_pool_budget`. `starting` is the exception: reclaiming
it *is* a capacity fix, which is why it gets its own, shorter threshold.

Three more were found in production on 2026-11-12, each of them a state the
sweep above never looked at:

* **`pooled` / `pooling` left by the old warm pool.** That pool kept spare
  sandboxes as rows in this table until 0.2.0 moved it into OpenSandbox
  (`warm_pool.py`). Nothing creates or adopts those rows now, but both are
  `RESERVED_SANDBOX_STATES` members. Four rows from 2026-08-05 had no
  container behind them and still reserved 768 MB each -- 3,072 MB of a
  10,686 MB budget, for 99 days. They are killed like a stuck `starting`.

* **`paused_cold` nobody came back for.** 205 rows, 204 of them idle for more
  than 30 days, each holding a filesystem snapshot. Killed after
  `reaper_paused_cold_retention_days`; `runtime.kill` deletes the snapshot.

* **Rows that are only history.** 21,910 `killed` sandboxes and 123,373
  executions (250 MB; 89,760 older than 30 days). `keep_completed_jobs_seconds`
  was defined for this and read by nothing, so it is gone and
  `reaper_killed_retention_days` does the work. See `_purge_history`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select

from harborbox.models import Execution, Sandbox

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from harborbox.config import Settings
    from harborbox.runtime_protocol import SandboxRuntime

log = logging.getLogger(__name__)

# The statuses `reap_once` loads. Everything else is live, or is history that
# `_purge_history` handles without loading rows.
REAPABLE_STATUSES = (
    "created",
    "failed",
    "starting",
    "pooled",
    "pooling",
    "paused_cold",
)

# Execution states that will never change again. A copy of
# `scheduler.TERMINAL_EXECUTION_STATES`: importing it would pull the scheduler,
# and with it `harborbox.db`'s engine, into a module that takes its session
# factory as an argument precisely so it does not need one.
FINISHED_EXECUTION_STATES = ("succeeded", "failed", "cancelled")

# Rows deleted per table, per sweep. The first sweep in production meets about
# 22,000 killed sandboxes and 90,000 old executions; deleting them in one
# statement would hold one long transaction and its locks against the
# scheduler, which writes the same tables every few hundred milliseconds. At
# the default five-minute interval this clears 288,000 rows of each kind a
# day, so the backlog is gone within a few hours and never builds again.
PURGE_BATCH_SIZE = 1000


@dataclass(frozen=True)
class ReapCandidate:
    """The fields the decision needs, so it can be tested without a database."""

    id: str
    status: str
    created_at: datetime
    last_activity_at: datetime | None = None


@dataclass(frozen=True)
class ReapPlan:
    """Ids to act on, split by what should happen to them."""

    delete: tuple[str, ...]
    prune: tuple[str, ...]
    # History removed by `_purge_history`. Counts, not ids: nothing needs to
    # act on a row that no longer exists. Not part of `total`, which is the
    # number of sandboxes the plan itself decided on.
    purged_sandboxes: int = 0
    purged_executions: int = 0

    @property
    def total(self) -> int:
        return len(self.delete) + len(self.prune)


def plan_reap(  # noqa: PLR0913 - one threshold per state it decides on; a
    # wrapper type would move the count somewhere else without lowering it.
    candidates: list[ReapCandidate],
    *,
    now: datetime,
    stuck_created_after: timedelta,
    failed_retention: timedelta,
    stuck_starting_after: timedelta | None = None,
    legacy_pooled_after: timedelta | None = None,
    paused_cold_retention: timedelta | None = None,
) -> ReapPlan:
    """Decide what to reclaim.

    Uses `last_activity_at` when present and falls back to `created_at`: a
    sandbox touched recently is one someone may still be waiting on, and the
    cost of waiting another cycle is far lower than the cost of deleting a
    sandbox out from under a live execution.

    `stuck_starting_after` defaults to `None` (skip `starting` entirely)
    rather than reusing `stuck_created_after` -- `starting` reserves real
    capacity and deserves its own, shorter threshold; callers that care
    about reclaiming it (`reap_once`) pass one explicitly.

    `legacy_pooled_after` and `paused_cold_retention` default to `None` for
    the same reason: a caller that does not name a threshold gets those
    states left alone, not an implicit cutoff. Both land in `delete`, which
    means "ask the runtime to kill it, then mark the row `killed`" -- for a
    `paused_cold` sandbox that kill is what deletes the snapshot.
    """
    delete: list[str] = []
    prune: list[str] = []

    for c in candidates:
        age_from = c.last_activity_at or c.created_at
        age = now - age_from
        # A clock skew or a future timestamp must never make something look
        # ancient; treat anything not clearly old as young.
        if age < timedelta(0):
            continue

        if c.status == "created" and age >= stuck_created_after:
            delete.append(c.id)
        elif c.status == "failed" and age >= failed_retention:
            prune.append(c.id)
        elif (
            (
                c.status == "starting"
                and stuck_starting_after is not None
                and age >= stuck_starting_after
            )
            or (
                c.status in {"pooled", "pooling"}
                and legacy_pooled_after is not None
                and age >= legacy_pooled_after
            )
            or (
                c.status == "paused_cold"
                and paused_cold_retention is not None
                and age >= paused_cold_retention
            )
        ):
            delete.append(c.id)

    return ReapPlan(delete=tuple(delete), prune=tuple(prune))


async def reap_once(
    session_factory: async_sessionmaker[AsyncSession],
    runtime: SandboxRuntime,
    settings: Settings,
) -> ReapPlan:
    """Run one sweep. Returns what it acted on, for logging and tests."""
    now = datetime.now(UTC)

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Sandbox).where(Sandbox.status.in_(REAPABLE_STATUSES))
            )
        ).scalars().all()

        plan = plan_reap(
            [
                ReapCandidate(
                    id=r.id,
                    status=r.status,
                    created_at=_require(_aware(r.created_at)),
                    last_activity_at=_aware(getattr(r, "last_activity_at", None)),
                )
                for r in rows
            ],
            now=now,
            stuck_created_after=timedelta(
                seconds=settings.reaper_stuck_created_after_seconds
            ),
            failed_retention=timedelta(hours=settings.reaper_failed_retention_hours),
            stuck_starting_after=timedelta(
                seconds=settings.reaper_stuck_starting_after_seconds
            ),
            legacy_pooled_after=timedelta(
                seconds=settings.reaper_legacy_pooled_after_seconds
            ),
            paused_cold_retention=(
                timedelta(days=settings.reaper_paused_cold_retention_days)
                if settings.reaper_paused_cold_retention_days > 0
                else None
            ),
        )

        by_id = {r.id: r for r in rows}
        for sandbox_id in plan.delete:
            sandbox = by_id[sandbox_id]
            try:
                # A stuck `created` may still have a half-built container behind
                # it; ask the runtime to clear it before dropping the row, or the
                # container is orphaned with nothing left pointing at it.
                await runtime.kill(sandbox)
            except Exception as exc:  # noqa: BLE001 - never let one row stop the sweep
                log.warning("reaper: kill failed for %s: %s", sandbox_id, exc)
                if sandbox.status == "paused_cold":
                    # The one state where the kill is the point: it deletes the
                    # snapshot, and the row holds the only record of the
                    # snapshot's id. Marking it `killed` now would orphan the
                    # snapshot for good. `runtime.kill` already treats "not
                    # found" as success, so a failure here is transient: leave
                    # the row and let the next sweep try again.
                    continue
            sandbox.status = "killed"
            sandbox.container_id = None
            sandbox.container_name = None
            # `runtime.kill` drops the snapshot id from the metadata. Reassign
            # so the JSON column is seen as changed and the id is not kept.
            sandbox.metadata_ = dict(sandbox.metadata_)

        for sandbox_id in plan.prune:
            await session.delete(by_id[sandbox_id])

        if plan.total:
            await session.commit()

    if plan.total:
        log.info(
            "reaper: cleared %d stuck or stale sandbox(es), pruned %d old failure(s)",
            len(plan.delete),
            len(plan.prune),
        )

    if settings.reaper_killed_retention_days > 0:
        purged_sandboxes, purged_executions = await _purge_history(
            session_factory,
            cutoff=now - timedelta(days=settings.reaper_killed_retention_days),
        )
        plan = replace(
            plan,
            purged_sandboxes=purged_sandboxes,
            purged_executions=purged_executions,
        )
    return plan


async def _purge_history(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cutoff: datetime,
    batch_size: int = PURGE_BATCH_SIZE,
) -> tuple[int, int]:
    """Delete one bounded batch of history older than `cutoff`.

    Two kinds of row, each in its own short transaction:

    * `killed` sandboxes, with their executions. Age is `updated_at`, which
      the kill itself set, so the retention runs from when the sandbox died
      and not from when it was created. The executions are deleted by an
      explicit statement first. The foreign key says ON DELETE CASCADE, but
      `create_all` never alters an existing table, so a database created
      before the cascade was declared does not have it, and SQLite ignores it
      unless asked. A bulk delete does not run the ORM's cascade either.

    * Finished executions of sandboxes that are still around. A long-lived
      `paused_cold` sandbox would otherwise keep every execution it ever ran.
      Only terminal states are touched, so a queued or running execution is
      never deleted however old it is.

    Returns `(sandboxes deleted, executions deleted)`. The execution count
    includes those removed with their sandbox.
    """
    async with session_factory() as session:
        sandbox_ids = (
            await session.scalars(
                select(Sandbox.id)
                .where(Sandbox.status == "killed", Sandbox.updated_at <= cutoff)
                .limit(batch_size)
            )
        ).all()
        with_sandbox = 0
        if sandbox_ids:
            with_sandbox = _rowcount(
                await session.execute(
                    delete(Execution).where(Execution.sandbox_id.in_(sandbox_ids))
                )
            )
            await session.execute(delete(Sandbox).where(Sandbox.id.in_(sandbox_ids)))
            await session.commit()

    async with session_factory() as session:
        execution_ids = (
            await session.scalars(
                select(Execution.id)
                .where(
                    Execution.status.in_(FINISHED_EXECUTION_STATES),
                    # `finished_at` is nullable; fall back so a terminal row
                    # that somehow lacks it is still aged out.
                    func.coalesce(Execution.finished_at, Execution.created_at)
                    <= cutoff,
                )
                .limit(batch_size)
            )
        ).all()
        if execution_ids:
            await session.execute(
                delete(Execution).where(Execution.id.in_(execution_ids))
            )
            await session.commit()

    purged_executions = with_sandbox + len(execution_ids)
    if sandbox_ids or purged_executions:
        log.info(
            "reaper: purged %d killed sandbox(es) and %d finished execution(s)",
            len(sandbox_ids),
            purged_executions,
        )
    return len(sandbox_ids), purged_executions


def _rowcount(result: object) -> int:
    """Rows a DELETE touched; the attribute lives on `CursorResult` only."""
    return max(int(getattr(result, "rowcount", 0) or 0), 0)


def _aware(value: datetime | None) -> datetime | None:
    """Normalise to UTC-aware; the column may come back naive."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def reaper_loop(
    session_factory: async_sessionmaker[AsyncSession],
    runtime: SandboxRuntime,
    settings: Settings,
    stop: asyncio.Event,
) -> None:
    """Sweep on an interval until told to stop.

    Failures are logged and swallowed on purpose: a reaper that dies takes the
    leak protection with it, and every condition it hits is transient by nature
    (a database blip, a runtime that is restarting).
    """
    while not stop.is_set():
        try:
            await reap_once(session_factory, runtime, settings)
        except Exception as exc:  # noqa: BLE001
            log.warning("reaper: sweep failed: %s", exc)
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=settings.reaper_interval_seconds
            )
        except TimeoutError:
            continue


def _require(value: datetime | None) -> datetime:
    """`created_at` is NOT NULL in the schema; this narrows the type."""
    if value is None:  # pragma: no cover - defensive
        message = "sandbox has no created_at"
        raise ValueError(message)
    return value
