"""What the reaper reclaims, and — more importantly — what it leaves alone."""

from datetime import UTC, datetime, timedelta

from harborbox.reaper import ReapCandidate, ReapPlan, plan_reap

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
STUCK_AFTER = timedelta(minutes=15)
FAILED_RETENTION = timedelta(hours=24)


def run(candidates: list[ReapCandidate]) -> ReapPlan:
    return plan_reap(
        candidates,
        now=NOW,
        stuck_created_after=STUCK_AFTER,
        failed_retention=FAILED_RETENTION,
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


class TestLeavesEverythingElseAlone:
    def test_never_touches_live_or_pooled_sandboxes(self) -> None:
        """These hold real reservations; reaping them would kill live work."""
        old = at(days=7)
        plan = run(
            [
                ReapCandidate("a", "running", old),
                ReapCandidate("b", "pooled", old),
                ReapCandidate("c", "starting", old),
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
