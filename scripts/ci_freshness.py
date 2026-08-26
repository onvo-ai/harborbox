"""Decide whether harborbox CI is still reporting a verdict, and say so if not.

DEV-1971: for four days no job in this repository was picked up by a runner.
Every run sat queued until GitHub's 48-hour queue timeout cancelled it, and
four PRs merged unvalidated in the meantime. Nothing alerted, because nothing
had gone *red* - a never-run check renders as neutral, not as a red X. The
signal to watch for is not failure, it is silence.

So this asks three questions, and deliberately not a fourth:

  1. Is any run stuck - queued or in progress far longer than a real run takes?
     That is DEV-1971's exact signature, and it is visible within hours rather
     than the four days it actually took.
  2. Was work asked of CI recently that CI never answered? A backstop for a
     failure that does not leave a single run visibly stuck.
  3. Is there work CI never created a run for at all? Added after 2026-08-26,
     when pushes and pull requests stopped triggering runs while the runners
     stayed perfectly healthy. Questions 1 and 2 both read the run list, and a
     run that is never created is not in it - so a total triggering failure
     looked exactly like an idle repository. This script reported
     "CI last reported 20.8h ago ... success" for the entire outage.

     Two things had to change. Runs triggered by `workflow_dispatch` or
     `schedule` no longer count as CI having answered: they prove a human
     poked CI, or a timer fired, not that the merge gate works. On 2026-08-26
     four manual runs kept resetting the clock in exactly that way. And the
     open pull request heads are now read directly, so "no run exists" is
     something this script can see rather than infer.

The question not asked is "has CI run lately". An idle repository is a quiet
weekend, not a broken gate, and a monitor that cries on quiet weekends gets
muted - after which it is worth less than nothing, because its silence now
reads as health.

`cancelled` is not a conclusion here. All eight runs this ticket was filed over
concluded `cancelled`, at exactly 48 hours each; treating that as "CI reported"
would make this script read the outage itself as health.

Usage:  python3 scripts/ci_freshness.py            # reads GITHUB_TOKEN, GITHUB_REPOSITORY
Prints the verdict as JSON on stdout and appends it to $GITHUB_OUTPUT.

Standard library only, on purpose: this runs on a GitHub-hosted runner with no
`uv sync` in front of it, so the watchdog shares no dependency, no cache and no
runner with the thing it watches.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# A whole run has never taken more than about 25 minutes, and the e2e job caps
# itself at 40. Anything still waiting after two hours is not a slow build.
STUCK_QUEUE_HOURS = 2.0

# The backstop window. Long enough that a weekend of no pushes cannot trip it,
# short enough to beat the 48-hour queue timeout that used to be the only
# thing marking these runs as anything at all.
STALE_COMPLETION_HOURS = 36.0

# Statuses that mean GitHub has accepted the run but no verdict exists yet.
PENDING_STATUSES = frozenset(
    {"queued", "in_progress", "waiting", "pending", "requested", "action_required"}
)

# A run that executed and produced an answer. Everything else - `cancelled`,
# `skipped`, `stale`, `neutral` - left the question unanswered.
REAL_CONCLUSIONS = frozenset({"success", "failure", "timed_out"})

# The events that actually gate a merge. A `workflow_dispatch` proves a human
# poked CI and the runners answered; a `schedule` proves a timer fired. Neither
# proves a push or a pull request still produces a run, and on 2026-08-26 that
# distinction was the whole outage: triggering had stopped while the runners
# stayed healthy, and four manual runs kept this script reporting
# "CI last reported 20.8h ago ... success" the entire time.
GATING_EVENTS = frozenset({"push", "pull_request"})

# How long a head commit may sit without any run before that is a finding.
# Generous on purpose - it must clear the normal lag between a push and the
# run appearing, or every push alarms for its first minute.
UNANSWERED_WORK_HOURS = 2.0

WORKFLOW = "ci.yml"
RUNS_TO_INSPECT = 30

# Bounded: this costs one API call per open pull request, and an unbounded
# loop in a watchdog is its own outage.
OPEN_PRS_TO_INSPECT = 20


@dataclass(frozen=True)
class Thresholds:
    """The three tuning knobs, together so `assess` keeps a readable signature."""

    stuck_queue: float = STUCK_QUEUE_HOURS
    stale_completion: float = STALE_COMPLETION_HOURS
    unanswered_work: float = UNANSWERED_WORK_HOURS


@dataclass(frozen=True)
class Verdict:
    """What to say, and whether to raise the alarm about it."""

    stale: bool
    headline: str
    details: list[str]


def parse_time(value: str) -> datetime:
    """Parse the ISO-8601 Zulu timestamps the Actions API returns."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def hours_since(value: str, now: datetime) -> float:
    return (now - parse_time(value)).total_seconds() / 3600.0


def describe(entry: dict[str, Any], now: datetime, field: str) -> str:
    age = hours_since(str(entry[field]), now)
    state = entry.get("conclusion") or entry.get("status")
    return (
        f"Run #{entry.get('run_number')} on `{entry.get('head_branch')}` - "
        f"{state}, {age:.1f}h old - {entry.get('html_url')}"
    )


def describe_head(head: dict[str, Any], now: datetime) -> str:
    age = hours_since(str(head["pushed_at"]), now)
    return (
        f"{head.get('label')} at `{str(head.get('sha'))[:7]}` - pushed {age:.1f}h ago, "
        f"no ci.yml run was ever created for it"
    )


def assess(
    runs: list[dict[str, Any]],
    now: datetime,
    heads: list[dict[str, Any]] | None = None,
    thresholds: Thresholds = Thresholds(),
) -> Verdict:
    """Judge a list of workflow runs, newest-first order not required.

    `heads` is the work CI is expected to answer for - open pull request heads
    and the tip of the default branch. It is judged separately from `runs`
    because the failure it catches leaves *no run to look at*: on 2026-08-26
    pushes and pull requests stopped producing runs entirely, so both of the
    original checks read the run list and saw an ordinary idle repository.
    """
    stuck = [
        entry
        for entry in runs
        if entry.get("status") in PENDING_STATUSES
        and hours_since(str(entry["created_at"]), now) > thresholds.stuck_queue
    ]
    if stuck:
        stuck.sort(key=lambda entry: parse_time(str(entry["created_at"])))
        headline = (
            f"{len(stuck)} CI run(s) have been queued or running for over "
            f"{thresholds.stuck_queue:g}h with no verdict"
        )
        return Verdict(
            stale=True,
            headline=headline,
            details=[describe(entry, now, "created_at") for entry in stuck],
        )

    # Work that CI never even opened a ticket on. Bounded at both ends: newer
    # than `thresholds.unanswered_work` has not had its chance yet, and older than
    # `thresholds.stale_completion` may simply have scrolled out of the run window
    # this script fetches - judging that would alarm about every long-open PR.
    # Gating runs only, and that is load-bearing rather than tidy: on
    # 2026-08-26 the dispatched runs sat on the very head commits whose pull
    # requests had no check. Counting them here would have let a manual run
    # mask the missing one - the same masking, one layer down.
    gating = [entry for entry in runs if entry.get("event") in GATING_EVENTS]
    seen_shas = {str(entry.get("head_sha")) for entry in gating}
    unanswered = [
        entry
        for entry in (heads or [])
        if thresholds.unanswered_work
        < hours_since(str(entry["pushed_at"]), now)
        <= thresholds.stale_completion
        and str(entry["sha"]) not in seen_shas
    ]
    if unanswered:
        unanswered.sort(key=lambda entry: parse_time(str(entry["pushed_at"])))
        return Verdict(
            stale=True,
            headline=(
                f"{len(unanswered)} commit(s) have no CI run at all - "
                f"pushes and pull requests are not triggering CI"
            ),
            details=[describe_head(entry, now) for entry in unanswered[:5]],
        )

    reported = [
        entry
        for entry in gating
        if entry.get("status") == "completed" and entry.get("conclusion") in REAL_CONCLUSIONS
    ]
    newest = max(reported, key=lambda entry: parse_time(str(entry["updated_at"])), default=None)

    # A run only counts as "CI was asked something" once it is old enough to
    # have answered - otherwise a push thirty seconds ago reads as silence.
    # `updated_at` rather than `created_at` is what puts it in the window: the
    # runs this ticket was filed over were created days before GitHub finally
    # cancelled them, and cancellation is the last thing CI ever said about
    # them. Keyed on creation, they would fall out of the window and the
    # outage would look like an idle repository.
    overdue = [
        entry
        for entry in gating
        if hours_since(str(entry["created_at"]), now) > thresholds.stuck_queue
        and hours_since(str(entry["updated_at"]), now) <= thresholds.stale_completion
    ]
    answered_recently = (
        newest is not None
        and hours_since(str(newest["updated_at"]), now) <= thresholds.stale_completion
    )

    if overdue and not answered_recently:
        overdue.sort(key=lambda entry: parse_time(str(entry["created_at"])))
        return Verdict(
            stale=True,
            headline=(
                f"{len(overdue)} CI run(s) moved in the last {thresholds.stale_completion:g}h "
                f"but no CI run has reported a conclusion in that time"
            ),
            details=[describe(entry, now, "created_at") for entry in overdue[:5]],
        )

    if newest is None:
        return Verdict(
            stale=False,
            headline="No CI runs to judge; nothing has been asked of CI.",
            details=[],
        )

    age = hours_since(str(newest["updated_at"]), now)
    return Verdict(
        stale=False,
        headline=(
            f"CI last reported {age:.1f}h ago: run #{newest.get('run_number')} "
            f"concluded {newest.get('conclusion')}."
        ),
        details=[],
    )


def api(path: str, token: str) -> Any:  # noqa: ANN401
    """GET one GitHub API path and return the decoded body.

    ANN401: the Actions and REST endpoints this reads return an object in some
    cases and an array in others, so the honest annotation is the loose one.
    """
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    # S310: the URL is built from a literal https prefix two lines up, so
    # there is no scheme for a caller to smuggle a `file:` through.
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def fetch_runs(repo: str, token: str) -> list[dict[str, Any]]:
    """Read the most recent ci.yml runs, in every status."""
    payload = api(
        f"/repos/{repo}/actions/workflows/{WORKFLOW}/runs?per_page={RUNS_TO_INSPECT}", token
    )
    return list(payload.get("workflow_runs", []))


def fetch_heads(repo: str, token: str) -> list[dict[str, Any]]:
    """Read the work CI is expected to answer for: main's tip and open PR heads.

    The commit date is read from the commit itself rather than taken from the
    pull request's `updated_at`, which also moves when somebody comments. A
    comment must not look like a fresh push - that would reset the clock on a
    head that has been waiting for a check all along, and, worse, make an old
    untouched head look recent enough to judge after its run has scrolled out
    of the window this script fetches.
    """
    heads: list[dict[str, Any]] = []

    def commit_date(sha: str) -> str | None:
        try:
            commit = api(f"/repos/{repo}/commits/{sha}", token)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            # A fork head the base repo cannot see, or a transient API error.
            # Skipping loses one check; raising loses the whole watchdog.
            return None
        return str(commit.get("commit", {}).get("committer", {}).get("date") or "") or None

    default_branch = str(api(f"/repos/{repo}", token).get("default_branch", "main"))
    for commit in api(f"/repos/{repo}/commits?sha={default_branch}&per_page=1", token):
        date = commit.get("commit", {}).get("committer", {}).get("date")
        if date:
            heads.append({"sha": commit["sha"], "label": default_branch, "pushed_at": date})

    for pull in api(f"/repos/{repo}/pulls?state=open&per_page={OPEN_PRS_TO_INSPECT}", token):
        # A draft is not asking to be merged, so a missing check on one is not
        # yet a broken gate.
        if pull.get("draft"):
            continue
        sha = str(pull.get("head", {}).get("sha", ""))
        date = commit_date(sha) if sha else None
        if date:
            heads.append({"sha": sha, "label": f"PR #{pull.get('number')}", "pushed_at": date})

    return heads


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "onvo-ai/harborbox")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        sys.stderr.write("GITHUB_TOKEN is not set\n")
        return 1

    verdict = assess(
        fetch_runs(repo, token),
        now=datetime.now(UTC),
        heads=fetch_heads(repo, token),
    )
    body = {"stale": verdict.stale, "headline": verdict.headline, "details": verdict.details}
    sys.stdout.write(json.dumps(body, indent=2) + "\n")

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:  # noqa: PTH123
            handle.write(f"stale={'true' if verdict.stale else 'false'}\n")
            handle.write(f"verdict<<EOF\n{json.dumps(body)}\nEOF\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
