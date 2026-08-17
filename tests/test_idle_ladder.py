"""The idle ladder: running -> paused_memory -> paused_cold.

Idle sandboxes used to drop straight to `paused_cold`, which releases the
memory but makes the next call rebuild a container from a snapshot. Freezing
first keeps the container and its warm interpreter, so a sandbox touched again
soon after resumes by unfreezing. What has to hold is that the cheap tier never
grows without bound -- a frozen sandbox still holds its whole reservation.
"""

from __future__ import annotations

from harborbox.scheduler import IdleSandbox, plan_suspensions

HOT_IDLE = 60
COLD_IDLE = 300


def sandbox(
    identifier: str,
    *,
    status: str = "running",
    idle_seconds: float,
    memory_mb: int = 512,
    idle_timeout_seconds: int = COLD_IDLE,
) -> IdleSandbox:
    return IdleSandbox(
        id=identifier,
        status=status,
        memory_mb=memory_mb,
        idle_seconds=idle_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
    )


def plan(
    candidates: list[IdleSandbox],
    *,
    hot_idle_seconds: int = HOT_IDLE,
    hot_budget_mb: int = 2048,
    frozen_memory_mb: int = 0,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    result = plan_suspensions(
        candidates,
        hot_idle_seconds=hot_idle_seconds,
        hot_budget_mb=hot_budget_mb,
        frozen_memory_mb=frozen_memory_mb,
    )
    return result.freeze, result.cool


def test_a_briefly_idle_sandbox_is_left_alone() -> None:
    assert plan([sandbox("a", idle_seconds=5)]) == ((), ())


def test_idle_past_the_hot_threshold_freezes_rather_than_snapshotting() -> None:
    """The point of the tier: keep the container so the resume is an unfreeze."""
    assert plan([sandbox("a", idle_seconds=HOT_IDLE + 1)]) == (("a",), ())


def test_idle_past_the_cold_timeout_still_goes_cold() -> None:
    """`idle_timeout_seconds` keeps its old meaning; the tier only precedes it."""
    assert plan([sandbox("a", idle_seconds=COLD_IDLE + 1)]) == ((), ("a",))


def test_a_frozen_sandbox_cools_at_its_own_timeout() -> None:
    frozen = sandbox("a", status="paused_memory", idle_seconds=COLD_IDLE + 1)

    assert plan([frozen]) == ((), ("a",))


def test_a_frozen_sandbox_is_never_frozen_twice() -> None:
    """Freezing an already-frozen sandbox would be a wasted runtime call."""
    frozen = sandbox("a", status="paused_memory", idle_seconds=HOT_IDLE + 1)

    assert plan([frozen]) == ((), ())


def test_freezing_stops_at_the_budget() -> None:
    """A frozen sandbox holds its full reservation, so the tier has to be capped.

    Without the cap the hot tier would consume the headroom that live
    admissions need, trading throughput for a resume that may never happen.
    """
    candidates = [
        sandbox("a", idle_seconds=HOT_IDLE + 1, memory_mb=1024),
        sandbox("b", idle_seconds=HOT_IDLE + 1, memory_mb=1024),
        sandbox("c", idle_seconds=HOT_IDLE + 1, memory_mb=1024),
    ]

    freeze, cool = plan(candidates, hot_budget_mb=2048)

    assert freeze == ("a", "b")
    assert cool == ()


def test_memory_already_frozen_counts_against_the_budget() -> None:
    """The cap is on the tier's total, not on what one pass adds to it."""
    freeze, _ = plan(
        [sandbox("a", idle_seconds=HOT_IDLE + 1, memory_mb=1024)],
        hot_budget_mb=2048,
        frozen_memory_mb=1536,
    )

    assert freeze == ()


def test_a_sandbox_over_the_budget_still_cools_on_its_own_timeout() -> None:
    """Losing the fast tier must not mean never being reclaimed at all."""
    freeze, cool = plan(
        [sandbox("a", idle_seconds=COLD_IDLE + 1, memory_mb=4096)],
        hot_budget_mb=512,
    )

    assert freeze == ()
    assert cool == ("a",)


def test_zero_idle_timeout_still_means_never_suspend() -> None:
    """The documented opt-out has to survive the new tier, at both levels."""
    candidates = [
        sandbox("a", idle_seconds=100_000, idle_timeout_seconds=0),
        sandbox(
            "b",
            status="paused_memory",
            idle_seconds=100_000,
            idle_timeout_seconds=0,
        ),
    ]

    assert plan(candidates) == ((), ())


def test_disabling_the_hot_tier_restores_the_old_behaviour() -> None:
    """`hot_pause_idle_seconds=0` must be a true revert, not a slower path."""
    candidates = [
        sandbox("a", idle_seconds=HOT_IDLE + 1),
        sandbox("b", idle_seconds=COLD_IDLE + 1),
    ]

    assert plan(candidates, hot_idle_seconds=0) == ((), ("b",))


def test_a_zero_budget_also_disables_the_hot_tier() -> None:
    assert plan([sandbox("a", idle_seconds=HOT_IDLE + 1)], hot_budget_mb=0) == ((), ())
