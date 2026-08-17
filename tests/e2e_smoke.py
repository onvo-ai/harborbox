"""Runs against a live local Compose stack. Selected with `pytest -m e2e`."""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from harborbox_sdk import SandboxClient


@pytest.mark.e2e
def test_two_sandboxes_run_code_in_parallel(client: SandboxClient) -> None:
    first = client.sandboxes.create(
        template="onvo-lite", memory_mb=128, cpu=1, idle_timeout_seconds=60
    )
    second = client.sandboxes.create(
        template="onvo-lite", memory_mb=128, cpu=1, idle_timeout_seconds=60
    )
    try:
        started = time.monotonic()
        first_job = first.run_code(
            "import time; time.sleep(2); first_value = 40; first_value + 2",
            wait=False,
        )
        second_job = second.run_code(
            "import time; time.sleep(2); second_value = 5; second_value * 2",
            wait=False,
        )
        first_job.wait(timeout=60, raise_on_error=True)
        second_job.wait(timeout=60, raise_on_error=True)
        elapsed = time.monotonic() - started
        assert first_job.text == "42", first_job.error
        assert second_job.text == "10", second_job.error
        assert first_job.started_at
        assert first_job.finished_at
        assert second_job.started_at
        assert second_job.finished_at
        first_started = datetime.fromisoformat(first_job.started_at)
        first_finished = datetime.fromisoformat(first_job.finished_at)
        second_started = datetime.fromisoformat(second_job.started_at)
        second_finished = datetime.fromisoformat(second_job.finished_at)
        assert first_started < second_finished
        assert second_started < first_finished

        # Executions are isolated from one another. This used to assert the
        # opposite -- `first_value` set above was still bound here, because a
        # Jupyter kernel held one namespace per sandbox. Removing the kernel
        # made every execution a fresh forked child, so cross-call state is
        # gone deliberately: the filesystem is now the only thing that carries
        # anything between calls, and the assertions below say so.
        isolated = first.run_code("first_value + 3", wait=True, wait_timeout=30)
        assert isolated.status == "failed"
        assert isolated.error
        assert isolated.error.name == "NameError", isolated.error

        first.files.write("state.txt", "preserved")
        assert first.files.read("state.txt") == "preserved"

        # A warm pause keeps the container, so the workspace is untouched...
        first.pause(memory=True)
        first.resume()
        assert first.files.read("state.txt") == "preserved"
        warm_state = first.run_code("open('state.txt').read()", wait=True, wait_timeout=30)
        assert warm_state.text == "'preserved'", warm_state.error

        # ...and a cold pause rebuilds the container from a snapshot, which is
        # the harder case: files still have to survive it.
        first.pause(memory=False)
        first.resume()
        assert first.files.read("state.txt") == "preserved"
        cold_state = first.run_code("open('state.txt').read()", wait=True, wait_timeout=30)
        assert cold_state.text == "'preserved'", cold_state.error

        min_reserved_memory_mb = 256
        capacity = client.capacity()
        assert capacity["reserved_memory_mb"] >= min_reserved_memory_mb
        # This is the e2e smoke test's pass/fail summary, printed to stdout for
        # whoever runs it locally or reads the CI job log; it is not application
        # logging.
        print(  # noqa: T201
            "e2e ok:",
            {
                "parallel_elapsed_seconds": round(elapsed, 2),
                "first": first_job.text,
                "second": second_job.text,
                "isolated_execution": isolated.error.name,
                "warm_pause_file": warm_state.text,
                "cold_pause_file": cold_state.text,
                "reserved_memory_mb": capacity["reserved_memory_mb"],
            },
        )
    finally:
        first.kill()
        second.kill()
