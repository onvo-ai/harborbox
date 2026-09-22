"""DEV-2400: the reaper must survive its own failure, per stage.

Before this, `_reaper_loop` wrapped all three stages (`_terminate_expired_
sandboxes`, `_cold_pause_idle_sandboxes`, `_sweep_unused_templates`) in one
`try`/`except`. A failure partway through -- the connection-pool exhaustion
that prompted this ticket, in particular -- aborted every stage after it, on
every single cycle, for as long as the failure lasted. And the retry had no
damping: a persistently failing stage hammered whatever it depends on at the
same fixed `reaper_poll_seconds` cadence forever, which is itself a way to
keep a struggling connection pool struggling.

These tests cover `_run_reaper_stage` in isolation -- the unit both fixes
live in -- rather than the real `_terminate_expired_sandboxes` and friends,
which `test_reaper_missing_sandbox.py` already covers for the pause-ladder's
own not-stopping-on-one-vanished-sandbox behaviour.
"""

from __future__ import annotations

import pytest

from harborbox import scheduler as scheduler_module
from harborbox.config import Settings
from harborbox.runtime_protocol import WarmPoolReservation

pytestmark = pytest.mark.anyio


class FakeRuntime:
    def warm_pool_reservation(self) -> WarmPoolReservation:
        return WarmPoolReservation()

    async def total_memory_mb(self) -> int:
        return 8192

    async def available_memory_mb(self) -> int:
        return 8192


def make_scheduler(**settings: object) -> scheduler_module.Scheduler:
    return scheduler_module.Scheduler(Settings(**settings), FakeRuntime())  # type: ignore[arg-type]


async def test_a_failing_stage_does_not_stop_its_siblings_running_in_the_same_cycle() -> None:
    """The exact DEV-2400 gap: one broken stage used to cost the other two."""
    scheduler = make_scheduler()
    calls: list[str] = []

    async def failing_terminate() -> None:
        calls.append("terminate")
        message = "pool exhausted"
        raise RuntimeError(message)

    async def ok_pause() -> None:
        calls.append("pause")

    async def ok_sweep() -> None:
        calls.append("sweep")

    # Mirrors the exact stage list and order `_reaper_loop` drives.
    for name, stage in (
        ("terminate_expired_sandboxes", failing_terminate),
        ("cold_pause_idle_sandboxes", ok_pause),
        ("sweep_unused_templates", ok_sweep),
    ):
        await scheduler._run_reaper_stage(name, stage)

    assert calls == ["terminate", "pause", "sweep"]


async def test_a_failure_returns_positive_backoff_and_a_success_returns_none() -> None:
    scheduler = make_scheduler(reaper_poll_seconds=5.0)

    async def boom() -> None:
        message = "boom"
        raise RuntimeError(message)

    async def ok() -> None:
        return None

    failure_backoff = await scheduler._run_reaper_stage("s", boom)
    success_backoff = await scheduler._run_reaper_stage("s", ok)

    assert failure_backoff > 0.0
    assert success_backoff == 0.0


async def test_backoff_escalates_with_consecutive_failures_of_the_same_stage() -> None:
    scheduler = make_scheduler(reaper_poll_seconds=1.0)

    async def boom() -> None:
        message = "boom"
        raise RuntimeError(message)

    delays = [await scheduler._run_reaper_stage("flaky", boom) for _ in range(4)]

    assert delays == sorted(delays)
    assert delays[0] < delays[-1]


async def test_backoff_is_capped_and_does_not_grow_without_bound() -> None:
    scheduler = make_scheduler(reaper_poll_seconds=1.0)

    async def boom() -> None:
        message = "boom"
        raise RuntimeError(message)

    for _ in range(50):
        delay = await scheduler._run_reaper_stage("stuck", boom)

    assert delay <= scheduler_module.REAPER_STAGE_BACKOFF_CAP_SECONDS


async def test_backoff_resets_to_zero_after_a_success_and_restarts_from_scratch() -> None:
    """A transient blip must not leave the stage permanently slow to retry."""
    scheduler = make_scheduler(reaper_poll_seconds=1.0)

    async def boom() -> None:
        message = "boom"
        raise RuntimeError(message)

    async def ok() -> None:
        return None

    first_failure = await scheduler._run_reaper_stage("flaky", boom)
    second_failure = await scheduler._run_reaper_stage("flaky", boom)
    assert second_failure > first_failure

    reset = await scheduler._run_reaper_stage("flaky", ok)
    assert reset == 0.0

    next_failure_after_reset = await scheduler._run_reaper_stage("flaky", boom)
    assert next_failure_after_reset == first_failure


async def test_different_stages_back_off_independently() -> None:
    scheduler = make_scheduler(reaper_poll_seconds=1.0)

    async def boom() -> None:
        message = "boom"
        raise RuntimeError(message)

    async def ok() -> None:
        return None

    await scheduler._run_reaper_stage("a", boom)
    await scheduler._run_reaper_stage("a", boom)
    a_third_failure = await scheduler._run_reaper_stage("a", boom)

    b_first_failure = await scheduler._run_reaper_stage("b", boom)
    ok_backoff = await scheduler._run_reaper_stage("c", ok)

    assert b_first_failure < a_third_failure
    assert ok_backoff == 0.0


async def test_cancellation_propagates_instead_of_being_swallowed_as_a_failure() -> None:
    scheduler = make_scheduler()

    async def cancelled() -> None:
        raise scheduler_module.asyncio.CancelledError

    with pytest.raises(scheduler_module.asyncio.CancelledError):
        await scheduler._run_reaper_stage("s", cancelled)

    # Not counted as a stage failure -- shutdown, not breakage.
    assert scheduler._reaper_stage_failures.get("s", 0) == 0
