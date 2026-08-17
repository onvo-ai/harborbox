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


@pytest.mark.e2e
def test_oom_is_contained_and_reported(client: SandboxClient) -> None:
    sandbox = client.sandboxes.create(template="onvo-lite", memory_mb=128, cpu=1)
    try:
        execution = sandbox.commands.run(
            OOM_COMMAND,
            wait=False,
        )
        execution.wait(timeout=60)
        assert execution.status == "failed"
        assert execution.error is not None
        assert execution.error.name == "MemoryLimitExceeded", execution.error
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
