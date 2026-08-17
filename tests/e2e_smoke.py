"""Runs against a live local Compose stack. Selected with `pytest -m e2e`."""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from live_client import SandboxClient

# Absolute: opensandbox runs commands through its own bootstrap, and the venv
# is only on PATH for the image's entrypoint.
PYTHON = "/opt/venv/bin/python"
READ_STATE = "cat /workspace/state.txt"


@pytest.mark.e2e
def test_two_sandboxes_execute_in_parallel(client: SandboxClient) -> None:
    first = client.sandboxes.create(
        template="onvo-lite", memory_mb=128, cpu=1, idle_timeout_seconds=60
    )
    second = client.sandboxes.create(
        template="onvo-lite", memory_mb=128, cpu=1, idle_timeout_seconds=60
    )
    try:
        started = time.monotonic()
        first_job = first.commands.run(
            f"{PYTHON} -c 'import time; time.sleep(2); print(40 + 2)'",
            wait=False,
        )
        second_job = second.commands.run(
            f"{PYTHON} -c 'import time; time.sleep(2); print(5 * 2)'",
            wait=False,
        )
        first_job.wait(timeout=60, raise_on_error=True)
        second_job.wait(timeout=60, raise_on_error=True)
        elapsed = time.monotonic() - started
        assert "".join(first_job.logs.stdout).strip() == "42", first_job.error
        assert "".join(second_job.logs.stdout).strip() == "10", second_job.error
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

        first.files.write("state.txt", "preserved")
        assert first.files.read("state.txt") == "preserved"

        # A warm pause keeps the container, so the workspace is untouched...
        first.pause(memory=True)
        first.resume()
        assert first.files.read("state.txt") == "preserved"
        warm_state = first.commands.run(READ_STATE, wait=True, wait_timeout=30)
        assert "".join(warm_state.logs.stdout).strip() == "preserved", warm_state.error

        # ...and a cold pause rebuilds the container from a snapshot, which is
        # the harder case: files still have to survive it.
        first.pause(memory=False)
        first.resume()
        assert first.files.read("state.txt") == "preserved"
        cold_state = first.commands.run(READ_STATE, wait=True, wait_timeout=30)
        assert "".join(cold_state.logs.stdout).strip() == "preserved", cold_state.error

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
                "first": "".join(first_job.logs.stdout).strip(),
                "second": "".join(second_job.logs.stdout).strip(),
                "warm_pause_file": "".join(warm_state.logs.stdout).strip(),
                "cold_pause_file": "".join(cold_state.logs.stdout).strip(),
                "reserved_memory_mb": capacity["reserved_memory_mb"],
            },
        )
    finally:
        first.kill()
        second.kill()
