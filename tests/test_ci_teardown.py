"""CI must reap the sandboxes it leaves behind, and `compose down` cannot.

This exists because of two containers that sat on the runner for a day.

The e2e job stands up the real stack on a self-hosted runner and tears it down
with `docker compose down -v`. That removes the services this compose project
declares. It does not remove a sandbox container, because a sandbox is not a
compose service: the opensandbox server spawns it directly against the host
Docker socket that `compose.yaml` mounts into it. So a sandbox belongs to no
compose project, and no `down` -- of any project, with any flags -- can reach
it.

The sandboxes also carry `opensandbox.io/manual-cleanup=true`, which is exactly
what it sounds like: opensandbox's own reaper will not collect them. Deleting
one is the creating test's job, via `finally: sandbox.kill()`. That works right
up until a test dies before its `finally`, or the job is cancelled, at which
point the container outlives the stack that knew about it -- and then outlives
the *next* run too, because nothing in the workflow ever looks for it.

Two survived a failing run on 2026-08-25 and were still up 23 hours later, on a
shared CI box already at 87% disk. They leak one failure at a time, which is
slow enough that nobody notices and permanent enough that it never reverses.

The fix is a reap keyed on the label harborbox itself sets
(`opensandbox_runtime.py` puts `harborbox.sandbox_id` in the sandbox metadata,
which becomes a Docker label). The label is the only honest selector here --
see `test_no_step_identifies_a_sandbox_by_not_being_something_else` for what
the alternative did on a shared runner.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"

# The label harborbox attaches to every sandbox it creates. Set in
# `src/harborbox/opensandbox_runtime.py`; asserted end-to-end in
# `tests/test_opensandbox_runtime.py`. If it ever changes, this file and the
# workflow have to change with it, which is the point of naming it once here.
SANDBOX_LABEL = "harborbox.sandbox_id"


@pytest.fixture(scope="module")
def workflow() -> dict:
    # `on:` parses as the boolean True under YAML 1.1, which is a well-known
    # trap in GitHub workflows. Nothing here reads it, so it is left alone
    # rather than worked around.
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def e2e_steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["e2e"]["steps"]


def step_named(steps: list[dict], name: str) -> dict:
    for step in steps:
        if step.get("name") == name:
            return step
    pytest.fail(f"no step named {name!r}; steps are {[s.get('name') for s in steps]}")


def test_teardown_removes_containers_carrying_the_sandbox_label(
    e2e_steps: list[dict],
) -> None:
    """The reap has to select on the label, not on a container name.

    Sandbox container names are opensandbox UUIDs (`sandbox-<uuid>`), which
    carry no marker tying them to this project. Matching that prefix would also
    match any other opensandbox on the box; matching the label matches exactly
    what harborbox created.
    """
    teardown = step_named(e2e_steps, "Tear down")
    script = teardown["run"]

    assert f"label={SANDBOX_LABEL}" in script, (
        "Tear down must select sandbox containers by the harborbox.sandbox_id "
        "label. `docker compose down` cannot reach them: a sandbox is spawned "
        "by opensandbox against the host Docker socket and belongs to no "
        "compose project."
    )
    assert re.search(r"docker\s+rm\s+-f", script), (
        "Tear down finds the sandbox containers but never removes them."
    )


def test_the_reap_runs_even_when_the_job_failed(e2e_steps: list[dict]) -> None:
    """A leak is *only* produced by the unhappy path.

    A passing run has already killed its sandboxes in each test's `finally`.
    Every container this reap will ever remove is one left by a failure or a
    cancellation, so a reap that skips those runs removes nothing, ever.
    """
    teardown = step_named(e2e_steps, "Tear down")
    assert teardown.get("if") == "always()", (
        "Tear down must be if: always() -- the containers it exists to remove "
        "only appear when the job did not pass."
    )


def test_the_reap_cannot_fail_the_job(e2e_steps: list[dict]) -> None:
    """`docker rm -f` with an empty argument list exits non-zero.

    On a green run there is nothing to reap, which is the common case. If that
    turns the teardown red, the first thing anyone does is delete the step.
    """
    # Join backslash continuations first: the reap is written across two lines
    # so the pipeline reads, and the guard lives on the second one.
    script = re.sub(r"\\\n\s*", " ", step_named(e2e_steps, "Tear down")["run"])
    reap = next(line for line in script.splitlines() if f"label={SANDBOX_LABEL}" in line)
    assert "|| true" in reap or "xargs -r" in reap or "if [" in reap, (
        f"the reap must tolerate finding nothing; got: {reap.strip()!r}"
    )


def test_no_step_identifies_a_sandbox_by_not_being_something_else(
    e2e_steps: list[dict],
) -> None:
    """Name-exclusion was wrong on a shared runner, and provably so.

    `Sandbox container logs on failure` used to list every container on the box
    and subtract this project's three services, calling the remainder
    "sandboxes". That reasoning holds only on a runner that runs nothing else.

    The actual runner is build-server, which also hosts `coolify-proxy`,
    `coolify-sentinel` and `buildx_buildkit_trigger0`. Those three matched the
    filter, so a failing e2e run dumped 500 lines of Traefik access logs into
    the job output and called them sandbox logs -- burying the sandbox
    evidence the step was added to surface.

    The label answers the same question without assuming anything about the
    host.
    """
    for step in e2e_steps:
        script = step.get("run", "")
        if "docker ps" not in script:
            continue
        assert "grep -Ev" not in script, (
            f"step {step.get('name')!r} selects containers by exclusion. On a "
            f"shared runner that matches unrelated host containers; filter on "
            f"--filter label={SANDBOX_LABEL} instead."
        )
