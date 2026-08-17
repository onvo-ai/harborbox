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
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from harborbox.models import Sandbox

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from harborbox.config import Settings
    from harborbox.runtime_protocol import SandboxRuntime

log = logging.getLogger(__name__)


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

    @property
    def total(self) -> int:
        return len(self.delete) + len(self.prune)


def plan_reap(
    candidates: list[ReapCandidate],
    *,
    now: datetime,
    stuck_created_after: timedelta,
    failed_retention: timedelta,
    stuck_starting_after: timedelta | None = None,
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
            c.status == "starting"
            and stuck_starting_after is not None
            and age >= stuck_starting_after
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
                select(Sandbox).where(
                    Sandbox.status.in_(("created", "failed", "starting"))
                )
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
        )
        if plan.total == 0:
            return plan

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
            sandbox.status = "killed"
            sandbox.container_id = None
            sandbox.container_name = None

        for sandbox_id in plan.prune:
            await session.delete(by_id[sandbox_id])

        await session.commit()

    log.info(
        "reaper: cleared %d stuck sandbox(es), pruned %d old failure(s)",
        len(plan.delete),
        len(plan.prune),
    )
    return plan


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
