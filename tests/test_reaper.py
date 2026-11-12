"""What the reaper reclaims, and — more importantly — what it leaves alone."""

from datetime import UTC, datetime, timedelta

from harborbox.reaper import ReapCandidate, ReapPlan, plan_reap

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
STUCK_AFTER = timedelta(minutes=15)
FAILED_RETENTION = timedelta(hours=24)
STUCK_STARTING_AFTER = timedelta(minutes=5)


def run(candidates: list[ReapCandidate]) -> ReapPlan:
    return plan_reap(
        candidates,
        now=NOW,
        stuck_created_after=STUCK_AFTER,
        failed_retention=FAILED_RETENTION,
        stuck_starting_after=STUCK_STARTING_AFTER,
    )


def at(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


class TestStuckCreated:
    def test_deletes_a_sandbox_that_never_started(self) -> None:
        plan = run([ReapCandidate("sbx_old", "created", at(hours=5))])
        assert plan.delete == ("sbx_old",)
        assert plan.prune == ()

    def test_leaves_one_that_was_just_created(self) -> None:
        """A sandbox is started lazily; a fresh one is normal, not stuck."""
        plan = run([ReapCandidate("sbx_new", "created", at(seconds=30))])
        assert plan.total == 0

    def test_boundary_is_inclusive(self) -> None:
        plan = run([ReapCandidate("sbx_edge", "created", at(minutes=15))])
        assert plan.delete == ("sbx_edge",)

    def test_recent_activity_protects_an_old_row(self) -> None:
        """Created long ago but touched a moment ago — someone is using it."""
        plan = run(
            [
                ReapCandidate(
                    "sbx_busy", "created", at(hours=9), last_activity_at=at(seconds=20)
                )
            ]
        )
        assert plan.total == 0


class TestFailedRetention:
    def test_prunes_an_old_failure(self) -> None:
        plan = run([ReapCandidate("sbx_f", "failed", at(hours=30))])
        assert plan.prune == ("sbx_f",)
        assert plan.delete == ()

    def test_keeps_a_recent_failure_for_inspection(self) -> None:
        """The moment someone wants a failure is right after it happens."""
        plan = run([ReapCandidate("sbx_f", "failed", at(minutes=20))])
        assert plan.total == 0

    def test_failed_uses_the_longer_horizon(self) -> None:
        """Older than the stuck threshold but well inside failed retention."""
        plan = run([ReapCandidate("sbx_f", "failed", at(hours=2))])
        assert plan.total == 0


class TestStuckStarting:
    """`starting` reserves real capacity, so it gets its own, shorter sweep.

    The primary fix for an abandoned start is `Scheduler._ensure_running`
    marking it `failed` itself; this is the backstop for whatever gets past
    that.
    """

    def test_deletes_a_start_abandoned_past_the_threshold(self) -> None:
        plan = run([ReapCandidate("sbx_stuck", "starting", at(minutes=10))])
        assert plan.delete == ("sbx_stuck",)
        assert plan.prune == ()

    def test_leaves_a_start_still_within_its_budget(self) -> None:
        plan = run([ReapCandidate("sbx_new", "starting", at(seconds=30))])
        assert plan.total == 0

    def test_boundary_is_inclusive(self) -> None:
        plan = run([ReapCandidate("sbx_edge", "starting", at(minutes=5))])
        assert plan.delete == ("sbx_edge",)

    def test_recent_activity_protects_an_old_row(self) -> None:
        plan = run(
            [
                ReapCandidate(
                    "sbx_busy",
                    "starting",
                    at(hours=1),
                    last_activity_at=at(seconds=10),
                )
            ]
        )
        assert plan.total == 0

    def test_a_none_threshold_skips_starting_entirely(self) -> None:
        """Omitting the threshold leaves `starting` alone altogether.

        `reap_once` always passes one; a caller that omits it gets the
        pre-existing behaviour rather than an arbitrary implicit cutoff.
        """
        plan = plan_reap(
            [ReapCandidate("sbx_old", "starting", at(days=7))],
            now=NOW,
            stuck_created_after=STUCK_AFTER,
            failed_retention=FAILED_RETENTION,
        )
        assert plan.total == 0


class TestLeavesEverythingElseAlone:
    def test_never_touches_live_or_pooled_sandboxes(self) -> None:
        """These hold real reservations; reaping them would kill live work."""
        old = at(days=7)
        plan = run(
            [
                ReapCandidate("a", "running", old),
                ReapCandidate("b", "pooled", old),
                ReapCandidate("d", "paused_memory", old),
                ReapCandidate("e", "pooling", old),
                ReapCandidate("f", "paused_cold", old),
                ReapCandidate("g", "killed", old),
            ]
        )
        assert plan.total == 0

    def test_a_future_timestamp_is_not_treated_as_ancient(self) -> None:
        """Clock skew must not cause a deletion."""
        plan = run(
            [ReapCandidate("sbx_future", "created", NOW + timedelta(hours=1))]
        )
        assert plan.total == 0

    def test_empty_input(self) -> None:
        assert run([]).total == 0


def test_mixed_batch_is_split_correctly() -> None:
    plan = run(
        [
            ReapCandidate("stuck", "created", at(hours=1)),
            ReapCandidate("fresh", "created", at(seconds=5)),
            ReapCandidate("oldfail", "failed", at(days=3)),
            ReapCandidate("newfail", "failed", at(minutes=5)),
            ReapCandidate("live", "running", at(days=3)),
        ]
    )
    assert plan.delete == ("stuck",)
    assert plan.prune == ("oldfail",)


LEGACY_POOLED_AFTER = timedelta(hours=1)
PAUSED_COLD_RETENTION = timedelta(days=30)


def run_with_stale_states(candidates: list[ReapCandidate]) -> ReapPlan:
    """`run`, plus the two thresholds `reap_once` passes for stale states."""
    return plan_reap(
        candidates,
        now=NOW,
        stuck_created_after=STUCK_AFTER,
        failed_retention=FAILED_RETENTION,
        stuck_starting_after=STUCK_STARTING_AFTER,
        legacy_pooled_after=LEGACY_POOLED_AFTER,
        paused_cold_retention=PAUSED_COLD_RETENTION,
    )


class TestLegacyPooled:
    """Rows the old in-table warm pool left behind still reserve memory.

    Production, 2026-11-12: four `pooled` rows from 2026-08-05 with no
    container behind them, 768 MB reserved each, for 99 days. Nothing creates
    or adopts these rows since 0.2.0, so an old one is never coming back.
    """

    def test_kills_a_pooled_row_left_by_the_old_pool(self) -> None:
        plan = run_with_stale_states([ReapCandidate("sbx_p", "pooled", at(days=99))])
        assert plan.delete == ("sbx_p",)
        assert plan.prune == ()

    def test_kills_a_row_that_never_finished_pooling(self) -> None:
        plan = run_with_stale_states([ReapCandidate("sbx_p", "pooling", at(hours=2))])
        assert plan.delete == ("sbx_p",)

    def test_leaves_one_inside_the_grace_period(self) -> None:
        plan = run_with_stale_states(
            [ReapCandidate("sbx_p", "pooled", at(minutes=10))]
        )
        assert plan.total == 0

    def test_boundary_is_inclusive(self) -> None:
        plan = run_with_stale_states([ReapCandidate("sbx_p", "pooled", at(hours=1))])
        assert plan.delete == ("sbx_p",)


class TestPausedColdRetention:
    """A cold-paused sandbox keeps a snapshot until something kills it.

    Production, 2026-11-12: 205 `paused_cold` rows, 204 idle for over 30 days.
    """

    def test_kills_one_idle_past_the_retention(self) -> None:
        plan = run_with_stale_states(
            [
                ReapCandidate(
                    "sbx_c", "paused_cold", at(days=90), last_activity_at=at(days=31)
                )
            ]
        )
        assert plan.delete == ("sbx_c",)

    def test_idleness_counts_not_age(self) -> None:
        """Created long ago, used last week: someone still wants the workspace."""
        plan = run_with_stale_states(
            [
                ReapCandidate(
                    "sbx_c", "paused_cold", at(days=90), last_activity_at=at(days=7)
                )
            ]
        )
        assert plan.total == 0

    def test_no_retention_leaves_paused_cold_alone(self) -> None:
        """`reaper_paused_cold_retention_days=0` reaches here as `None`."""
        plan = plan_reap(
            [ReapCandidate("sbx_c", "paused_cold", at(days=400))],
            now=NOW,
            stuck_created_after=STUCK_AFTER,
            failed_retention=FAILED_RETENTION,
            legacy_pooled_after=LEGACY_POOLED_AFTER,
        )
        assert plan.total == 0


def test_stale_states_never_take_live_sandboxes_with_them() -> None:
    old = at(days=400)
    plan = run_with_stale_states(
        [
            ReapCandidate("a", "running", old),
            ReapCandidate("b", "paused_memory", old),
            ReapCandidate("c", "killed", old),
        ]
    )
    assert plan.total == 0
