from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ci_freshness import assess

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


def stamp(hours_ago: float) -> str:
    """Return `hours_ago` before NOW, as the ISO-8601 Zulu string the API emits."""
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    created: float = 1.0,
    updated: float | None = None,
    number: int = 1,
) -> dict[str, object]:
    return {
        "id": 1000 + number,
        "run_number": number,
        "status": status,
        "conclusion": conclusion,
        "created_at": stamp(created),
        "updated_at": stamp(created if updated is None else updated),
        "html_url": f"https://github.com/onvo-ai/harborbox/actions/runs/{1000 + number}",
        "head_branch": "main",
    }


def test_a_recent_real_conclusion_is_healthy() -> None:
    verdict = assess([run(conclusion="success", created=3)], now=NOW)

    assert verdict.stale is False


def test_a_recent_failure_is_healthy_because_the_gate_reported() -> None:
    # A red CI is not this monitor's problem. It watches for CI that says
    # nothing at all, which is the failure mode that hid for four days.
    verdict = assess([run(conclusion="failure", created=3)], now=NOW)

    assert verdict.stale is False


def test_a_run_queued_past_the_threshold_is_stale() -> None:
    # DEV-1971's exact signature: created, never picked up by a runner, and
    # left to sit until GitHub's 48-hour queue timeout cancels it.
    verdict = assess([run(status="queued", conclusion=None, created=6, number=52)], now=NOW)

    assert verdict.stale is True
    assert "queued" in verdict.headline
    assert any("#52" in detail for detail in verdict.details)


def test_a_run_queued_briefly_is_healthy() -> None:
    verdict = assess([run(status="queued", conclusion=None, created=0.5)], now=NOW)

    assert verdict.stale is False


def test_a_run_stuck_in_progress_is_stale() -> None:
    # The e2e job caps itself at 40 minutes, so hours of `in_progress` is a
    # hung runner, not a slow build.
    verdict = assess([run(status="in_progress", conclusion=None, created=6)], now=NOW)

    assert verdict.stale is True


def test_cancelled_does_not_count_as_a_conclusion() -> None:
    # The eight runs DEV-1971 was filed over all concluded `cancelled`, at
    # exactly 48 hours each. Counting those as "CI reported" would make the
    # monitor read the outage as health.
    verdict = assess(
        [
            run(status="completed", conclusion="cancelled", created=40, updated=1),
            run(status="completed", conclusion="cancelled", created=44, updated=2),
        ],
        now=NOW,
    )

    assert verdict.stale is True
    assert "no CI run has reported" in verdict.headline


def test_a_timed_out_run_counts_as_a_conclusion() -> None:
    # It executed and produced a verdict; the verdict was "too slow".
    verdict = assess([run(status="completed", conclusion="timed_out", created=3)], now=NOW)

    assert verdict.stale is False


def test_an_idle_repo_with_no_runs_at_all_is_healthy() -> None:
    # Nobody pushed. That is a quiet weekend, not a broken gate, and paging
    # for it is how a monitor gets muted.
    assert assess([], now=NOW).stale is False


def test_an_idle_repo_whose_last_run_is_old_is_healthy() -> None:
    # Same reason: the newest run is ancient, but nothing has been asked of
    # CI since, so there is nothing failing to report.
    verdict = assess([run(conclusion="success", created=200)], now=NOW)

    assert verdict.stale is False


def test_recent_work_with_no_recent_conclusion_is_stale() -> None:
    # Nothing is visibly stuck any more - GitHub cancelled it - but CI moved
    # recently and still answered nothing. This is the backstop, and it is the
    # distinction that keeps a quiet weekend quiet and a dead gate loud.
    verdict = assess(
        [
            run(status="completed", conclusion="cancelled", created=50, updated=2, number=9),
            run(conclusion="success", created=200, number=1),
        ],
        now=NOW,
    )

    assert verdict.stale is True
    assert "no CI run has reported" in verdict.headline


def test_details_name_the_run_and_link_it() -> None:
    verdict = assess([run(status="queued", conclusion=None, created=6, number=52)], now=NOW)

    joined = "\n".join(verdict.details)
    assert "actions/runs/1052" in joined
    assert "6.0h" in joined


def test_a_healthy_verdict_still_says_when_ci_last_reported() -> None:
    verdict = assess([run(conclusion="success", created=3, number=7)], now=NOW)

    assert "#7" in verdict.headline
