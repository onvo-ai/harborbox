"""Verify that a sandbox OOM is contained and reported. Selected with `pytest -m e2e`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from live_client import SandboxClient

# Allocates well past the sandbox's 128 MB limit. Runs through /commands
# because POST /v1/sandboxes/{id}/executions no longer exists.
OOM_COMMAND = "/opt/venv/bin/python -c 'payload = bytearray(384 * 1024 * 1024)'"

# How long a caller should ever have to wait to learn that a memory-hungry
# script died from an OOM kill. Deliberately tight, not generous: before the
# OpenSandboxRuntime fixes on this branch (see task-21-fix-report.md), this
# exact scenario hung until the execution's own timeout before surfacing
# anything at all -- tens of seconds of silence for a kernel that was
# already dead. A kernel death that severs the streaming response should be
# visible almost immediately. This bound exists specifically to fail the
# test (via TimeoutError, not an assertion) if that hang ever regresses, not
# to give the system room to be slow.
OOM_FAILURE_BOUND_SECONDS = 15


@pytest.mark.e2e
def test_oom_is_contained_and_reported(client: SandboxClient) -> None:
    sandbox = client.sandboxes.create(template="onvo-lite", memory_mb=128, cpu=1)
    try:
        execution = sandbox.commands.run(
            OOM_COMMAND,
            wait=False,
        )
        # A hang here (TimeoutError) is itself the regression this test
        # exists to catch -- see OOM_FAILURE_BOUND_SECONDS above.
        execution.wait(timeout=OOM_FAILURE_BOUND_SECONDS)

        # Reported: the execution must fail, not hang (caught above) and
        # not silently succeed with a script that never actually ran to
        # completion.
        assert execution.status == "failed"
        assert execution.error is not None
        assert execution.error.name, "error.name must not be empty"
        assert execution.error.value, "error.value must not be empty"

        # Deliberately NOT asserted: execution.error.name == "MemoryLimitExceeded".
        #
        # The kernel process is killed by the Linux OOM killer, which severs
        # the streaming HTTP response execd was sending back mid-body. That
        # surfaces to the OpenSandbox SDK as a transport-level error
        # (`httpx.RemoteProtocolError`, "peer closed connection without
        # sending complete message body"), wrapped as a generic
        # `SandboxException` with no OOM-specific code or field anywhere in
        # it. A genuine network fault mid-stream would raise the identical
        # exception. There is nothing observable from the client -- not the
        # exception, not OpenSandbox's `get_sandbox_info` (the container and
        # its `execd` process both survive; only the kernel inside them
        # dies, so container-level status reports `running` throughout) --
        # that distinguishes "this was an OOM" from "this was a network
        # blip." Matching on the transport-error text to force a
        # `MemoryLimitExceeded` label would just relocate the exact mistake
        # already found and removed once in this codebase (a dead substring
        # match against exception text that could never actually fire) onto
        # different text, and it would mislabel real transport faults as
        # memory errors. See `task-21-fix-report.md`'s "Fix round 2" section
        # for the full investigation.
        #
        # `harborbox.scheduler.ERROR_NAME_MEMORY_LIMIT_EXCEEDED` and
        # `OpenSandboxRuntime._detect_memory_exceeded` are still correct and
        # still kept, not dead code to be deleted: both defects found in
        # them were real (the dead substring match, and a
        # `state == "running"` short-circuit that could never fire once
        # OOM-killed-but-still-running was understood correctly) and the
        # fixes are genuine improvements for any path that *can* observe a
        # live status change. This specific scenario -- a severed stream
        # with the container still reporting `running` -- just never
        # reaches that code with anything to detect.

        # Contained, part one: a second, unrelated sandbox created right
        # after must work normally. This is the actual containment claim --
        # not merely that the API process is still up
        # (test_api_stays_healthy_after_oom covers that separately) but that
        # the scheduler's capacity accounting and admission are unaffected,
        # i.e. the blast radius was the one sandbox that OOM'd, not the host.
        control = client.sandboxes.create(template="onvo-lite", memory_mb=128, cpu=1)
        try:
            # Was `run_code("1 + 1")` until the Jupyter kernel and the endpoint
            # it served were removed; a command is what a caller has now, and
            # the claim being made here is about the scheduler, not about how
            # the work is expressed.
            control_execution = control.commands.run(
                "/opt/venv/bin/python -c 'print(1 + 1)'", wait=True, wait_timeout=30
            )
            assert control_execution.status == "succeeded", control_execution.error
            assert "".join(control_execution.logs.stdout).strip() == "2"
        finally:
            control.kill()
    finally:
        sandbox.kill()


@pytest.mark.e2e
def test_api_stays_healthy_after_oom(client: SandboxClient) -> None:
    sandbox = client.sandboxes.create(template="onvo-lite", memory_mb=128, cpu=1)
    try:
        execution = sandbox.commands.run(
            OOM_COMMAND,
            wait=False,
        )
        execution.wait(timeout=60)

        health = httpx.get("http://localhost:8000/health", timeout=5)
        health.raise_for_status()
        assert health.json() == {"status": "ok"}
    finally:
        sandbox.kill()
