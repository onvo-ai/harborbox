"""Time the idle ladder's transitions against a live stack. `pytest -m e2e`.

The ladder (`running -> paused_memory -> paused_cold`) shipped with its
decision logic unit-tested and its latency claim unmeasured: freezing and
unfreezing need a container runtime, and the machine the rest of the
benchmarks ran on had no Docker daemon. This suite closes that gap by timing
each transition against the real Compose stack, where OpenSandbox and Docker
both exist.

What the ladder is betting, in one line: freezing is cheap in both directions
and snapshotting is expensive in both, so spend the cheap pair early and defer
the expensive pair to the sandbox's own `idle_timeout_seconds`.

The assertions are deliberately *relative*, not absolute. A shared 2-core CI
runner cannot support a claim like "unfreeze takes under a second" without
being flaky, and a threshold loose enough not to flake would not be testing
anything. What has to hold for the tier to be worth its memory is that an
unfreeze beats a snapshot restore **on the same machine in the same run**, so
that is what is asserted. The measured numbers are printed either way, so a
regression in absolute terms is visible in the job log even when the relative
assertion still passes.
"""

from __future__ import annotations

import time
from http import HTTPStatus
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from live_client import Sandbox, SandboxClient

# Small enough to snapshot quickly on a 2-core runner, big enough to be a real
# container. `idle_timeout_seconds=0` exempts the sandbox from
# `plan_suspensions` entirely, so the scheduler cannot freeze it underneath a
# measurement -- `hot_pause_idle_seconds` defaults to 60s and these tests run
# for longer than that.
TEMPLATE = "onvo-lite"
MEMORY_MB = 256
CPU = 0.5

PROBE_PATH = "/tmp/ladder-probe.txt"  # noqa: S108 - the sandbox's own tmpfs
PROBE_BODY = "written before the pause"
# Three tries with 1s/2s backoff. Enough for a busy runner to free the
# capacity the previous test's sandbox just released; short enough that a
# genuinely broken start still fails the suite rather than hanging it.
START_ATTEMPTS = 3


def _timed(label: str, action: Callable[[], object]) -> float:
    started = time.perf_counter()
    action()
    elapsed = time.perf_counter() - started
    print(f"  {label:<28} {elapsed * 1000:8.1f} ms")  # noqa: T201 - the point
    return elapsed


@pytest.fixture
def sandbox(client: SandboxClient) -> Iterator[Sandbox]:
    """Build a sandbox that is actually `running`, which the ladder's top rung needs.

    `create` returns a `created` row, not a container -- starting is lazy. Pause
    a `created` sandbox and `plan_pause` correctly sends it straight to
    `paused_cold` whatever `memory` said, because there is nothing to freeze.
    Writing a file is the cheapest way to force the start, and every test here
    needs the probe file anyway.
    """
    live = client.sandboxes.create(
        template=TEMPLATE,
        memory_mb=MEMORY_MB,
        cpu=CPU,
        idle_timeout_seconds=0,
    )
    try:
        _start(live)
        yield live
    finally:
        live.kill()


def _start(live: Sandbox) -> None:
    """Force the lazy start, retrying a 503.

    Each test here creates its own sandbox and the runner is 2 cores / 3.7 GB
    with the whole Compose stack already on it, so the third start in a row can
    lose the race and come back `503 sandbox failed to start or become ready`.
    That is the box being busy, not the ladder being broken, and a caller would
    retry it too.
    """
    last: httpx.HTTPStatusError | None = None
    for attempt in range(START_ATTEMPTS):
        try:
            live.files.write(PROBE_PATH, PROBE_BODY)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != HTTPStatus.SERVICE_UNAVAILABLE:
                raise
            last = exc
            time.sleep(2**attempt)
        else:
            assert live.refresh().status == "running"
            return
    message = f"sandbox never started after {START_ATTEMPTS} attempts: {last}"
    raise AssertionError(message)


@pytest.mark.e2e
def test_freezing_and_unfreezing_beats_snapshot_and_restore(sandbox: Sandbox) -> None:
    """The tier's whole justification, measured both ways on one machine.

    A frozen sandbox keeps its entire memory reservation, so it is only worth
    holding if resuming it is materially cheaper than rebuilding from a
    snapshot. If this ever fails, the hot tier is spending live capacity for
    nothing and `hot_pause_idle_seconds=0` is the better default.
    """
    print(f"\npause ladder, {TEMPLATE} {MEMORY_MB}MB/{CPU}cpu:")  # noqa: T201

    freeze = _timed("hot -> warm  (freeze)", lambda: sandbox.pause(memory=True))
    assert sandbox.refresh().status == "paused_memory"

    thaw = _timed("warm -> hot   (unfreeze)", sandbox.resume)
    assert sandbox.refresh().status == "running"

    snapshot = _timed("hot -> cold  (snapshot)", lambda: sandbox.pause(memory=False))
    assert sandbox.refresh().status == "paused_cold"

    restore = _timed("cold -> hot  (restore)", sandbox.resume)
    assert sandbox.refresh().status == "running"

    assert thaw < restore, (
        f"unfreeze took {thaw:.3f}s and snapshot restore took {restore:.3f}s. "
        f"The hot tier holds a sandbox's full memory reservation to buy a "
        f"faster resume; if it does not buy one, it is pure cost."
    )
    # Not a latency claim -- just that freezing is not doing snapshot-shaped
    # work. Snapshotting writes the container filesystem to an image; freezing
    # writes one cgroup control file.
    assert freeze < snapshot, (
        f"freeze took {freeze:.3f}s and snapshot took {snapshot:.3f}s; freezing "
        f"is supposed to be the cheap rung."
    )


@pytest.mark.e2e
def test_a_cold_pause_preserves_the_filesystem(sandbox: Sandbox) -> None:
    """Cold is not destructive, which is what makes killing a paused sandbox wasteful.

    A caller that treats any non-`running` status as dead, kills the sandbox and
    creates a fresh one pays for re-uploading everything -- and cannot get it
    back, because `kill()` deletes the snapshot along with the container.
    """
    sandbox.pause(memory=False)
    assert sandbox.refresh().status == "paused_cold"

    sandbox.resume()
    assert sandbox.refresh().status == "running"

    assert sandbox.files.read(PROBE_PATH) == PROBE_BODY


@pytest.mark.e2e
def test_a_frozen_sandbox_can_still_go_cold(sandbox: Sandbox) -> None:
    """The one ladder transition with no equivalent in the pre-ladder code.

    `_suspend` sends an already-frozen sandbox down to cold on its own idle
    timeout, which means `create_snapshot()` runs against a container whose
    processes are frozen -- there is no thaw first. Whether that snapshots
    cleanly is a property of the runtime, not of anything this repo can decide,
    so it is worth an explicit test rather than an assumption. It is not: the
    first live run answered with `[SNAPSHOT::INVALID_SOURCE_STATE] Snapshot can
    only be created from a Running sandbox`, so the transition now thaws first
    (see `PausePlan.thaw_first`).
    """
    sandbox.pause(memory=True)
    assert sandbox.refresh().status == "paused_memory"

    cool = _timed("warm -> cold  (snapshot)", lambda: sandbox.pause(memory=False))
    assert sandbox.refresh().status == "paused_cold", (
        "a frozen sandbox asked to go cold stayed frozen, so it is still "
        "holding its full memory reservation"
    )
    assert cool > 0

    sandbox.resume()
    assert sandbox.refresh().status == "running"
    assert sandbox.files.read(PROBE_PATH) == PROBE_BODY
