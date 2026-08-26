from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from ci_freshness import assess

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


def stamp(hours_ago: float) -> str:
    """Return `hours_ago` before NOW, as the ISO-8601 Zulu string the API emits."""
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(  # noqa: PLR0913 - a test factory earns one knob per field it fakes.
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    created: float = 1.0,
    updated: float | None = None,
    number: int = 1,
    event: str = "push",
    head_sha: str = "abc1234",
) -> dict[str, object]:
    return {
        "id": 1000 + number,
        "run_number": number,
        "status": status,
        "conclusion": conclusion,
        "event": event,
        "head_sha": head_sha,
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


# --- The 2026-08-26 blind spot -------------------------------------------
#
# The runners were healthy all day. What broke was *triggering*: pushes and
# pull requests stopped producing runs. The watchdog reported
# "CI last reported 20.8h ago ... success" throughout, for two compounding
# reasons, one per group of tests below.


def head(*, sha: str = "abc1234", pushed: float = 4.0, label: str = "PR #29") -> dict[str, object]:
    """Build a piece of work CI is expected to answer for."""
    return {"sha": sha, "label": label, "pushed_at": stamp(pushed)}


def test_a_dispatched_run_does_not_prove_the_gate_works() -> None:
    """Reason one: any run counted, whoever asked for it.

    `workflow_dispatch` proves a human poked CI and the runners answered. It
    says nothing about whether a push or a pull request still produces a run,
    which is the only thing that gates a merge. On 2026-08-26 four manual runs
    kept resetting this clock while no PR could get a check at all.
    """
    verdict = assess(
        [run(event="workflow_dispatch", created=1)],
        now=NOW,
        heads=[head(pushed=4)],
    )

    assert verdict.stale is True
    assert "no CI run" in verdict.headline


def test_a_scheduled_run_does_not_prove_the_gate_works_either() -> None:
    verdict = assess(
        [run(event="schedule", created=1)],
        now=NOW,
        heads=[head(pushed=4)],
    )

    assert verdict.stale is True


def test_work_with_no_run_at_all_is_stale() -> None:
    """Reason two: nothing looked at whether a run was even created.

    Both existing checks read the run list. A run that is never created is
    absent from it, so a total triggering failure looked exactly like an idle
    repository. This is the check that would have caught 2026-08-26.
    """
    verdict = assess([], now=NOW, heads=[head(sha="deadbee", pushed=5, label="PR #29")])

    assert verdict.stale is True
    assert "PR #29" in " ".join(verdict.details)


def test_work_that_did_get_a_run_is_healthy() -> None:
    verdict = assess(
        [run(event="pull_request", head_sha="deadbee", created=4)],
        now=NOW,
        heads=[head(sha="deadbee", pushed=5)],
    )

    assert verdict.stale is False


def test_work_pushed_moments_ago_is_not_yet_overdue() -> None:
    """The grace period, or every push alarms for the seconds before it starts."""
    verdict = assess([], now=NOW, heads=[head(pushed=0.2)])

    assert verdict.stale is False


def test_work_older_than_the_run_window_is_not_judged() -> None:
    """A long-open PR's run has scrolled out of the 30 runs we fetch.

    Judging it would alarm about every stale PR in the repository, which is
    the "cries on quiet weekends" failure this script is written to avoid.
    """
    verdict = assess([], now=NOW, heads=[head(pushed=200)])

    assert verdict.stale is False


def test_an_idle_repo_with_only_dispatched_runs_is_still_healthy() -> None:
    """No work outstanding means nothing to be silent about.

    Excluding dispatch from "CI answered" must not turn a quiet repo whose
    only activity was a manual run into an alarm.
    """
    verdict = assess([run(event="workflow_dispatch", created=1)], now=NOW, heads=[])

    assert verdict.stale is False


def test_the_workflow_grants_the_scopes_the_third_check_needs() -> None:
    """A missing scope makes the new check judge nothing, silently.

    `fetch_heads` reads the open pull requests and each head's commit date.
    Without `pull-requests: read` that call fails, `heads` comes back empty,
    and the watchdog goes back to reporting health from the run list alone --
    the precise blind spot it was just taught to see. That degradation is
    invisible in the job log, so it is pinned here instead.
    """
    workflow = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / ".github/workflows/ci-freshness.yml").read_text(
            encoding="utf-8"
        )
    )
    granted = workflow["jobs"]["freshness"]["permissions"]

    assert granted.get("pull-requests") == "read", (
        "ci-freshness.yml must grant pull-requests: read, or fetch_heads returns "
        "nothing and the triggering check quietly stops working."
    )
    assert granted.get("contents") == "read"
    assert granted.get("actions") == "read"
