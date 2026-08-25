"""Decide whether harborbox CI is still reporting a verdict, and say so if not.

DEV-1971: for four days no job in this repository was picked up by a runner.
Every run sat queued until GitHub's 48-hour queue timeout cancelled it, and
four PRs merged unvalidated in the meantime. Nothing alerted, because nothing
had gone *red* - a never-run check renders as neutral, not as a red X. The
signal to watch for is not failure, it is silence.

So this asks two questions, and deliberately not a third:

  1. Is any run stuck - queued or in progress far longer than a real run takes?
     That is DEV-1971's exact signature, and it is visible within hours rather
     than the four days it actually took.
  2. Was work asked of CI recently that CI never answered? A backstop for a
     failure that does not leave a single run visibly stuck.

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

WORKFLOW = "ci.yml"
RUNS_TO_INSPECT = 30


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


def assess(
    runs: list[dict[str, Any]],
    now: datetime,
    stuck_queue_hours: float = STUCK_QUEUE_HOURS,
    stale_completion_hours: float = STALE_COMPLETION_HOURS,
) -> Verdict:
    """Judge a list of workflow runs, newest-first order not required."""
    stuck = [
        entry
        for entry in runs
        if entry.get("status") in PENDING_STATUSES
        and hours_since(str(entry["created_at"]), now) > stuck_queue_hours
    ]
    if stuck:
        stuck.sort(key=lambda entry: parse_time(str(entry["created_at"])))
        headline = (
            f"{len(stuck)} CI run(s) have been queued or running for over "
            f"{stuck_queue_hours:g}h with no verdict"
        )
        return Verdict(
            stale=True,
            headline=headline,
            details=[describe(entry, now, "created_at") for entry in stuck],
        )

    reported = [
        entry
        for entry in runs
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
        for entry in runs
        if hours_since(str(entry["created_at"]), now) > stuck_queue_hours
        and hours_since(str(entry["updated_at"]), now) <= stale_completion_hours
    ]
    answered_recently = (
        newest is not None and hours_since(str(newest["updated_at"]), now) <= stale_completion_hours
    )

    if overdue and not answered_recently:
        overdue.sort(key=lambda entry: parse_time(str(entry["created_at"])))
        return Verdict(
            stale=True,
            headline=(
                f"{len(overdue)} CI run(s) moved in the last {stale_completion_hours:g}h "
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


def fetch_runs(repo: str, token: str) -> list[dict[str, Any]]:
    """Read the most recent ci.yml runs, in every status."""
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW}"
        f"/runs?per_page={RUNS_TO_INSPECT}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    # S310: the URL is built from a literal https prefix two lines up, so
    # there is no scheme for a caller to smuggle a `file:` through.
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        payload = json.load(response)
    return list(payload.get("workflow_runs", []))


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "onvo-ai/harborbox")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        sys.stderr.write("GITHUB_TOKEN is not set\n")
        return 1

    verdict = assess(fetch_runs(repo, token), now=datetime.now(UTC))
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
