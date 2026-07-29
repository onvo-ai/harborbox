"""Verify that a sandbox OOM is contained and reported."""

from __future__ import annotations

import os

import httpx

from harborbox_sdk import SandboxClient


def main() -> None:
    api_key = os.environ["HARBORBOX_API_KEY"]
    with SandboxClient(api_key=api_key) as client:
        sandbox = client.sandboxes.create(memory_mb=128, cpu=1)
        try:
            execution = sandbox.run_code(
                "payload = bytearray(384 * 1024 * 1024)",
                wait=False,
            )
            execution.wait(timeout=60)
            assert execution.status == "failed"
            assert execution.error is not None
            assert execution.error.name == "MemoryLimitExceeded", execution.error

            health = httpx.get("http://localhost:8000/health", timeout=5)
            health.raise_for_status()
            assert health.json() == {"status": "ok"}
            print(
                "oom containment ok:",
                {"status": execution.status, "error": execution.error.name},
            )
        finally:
            sandbox.kill()


if __name__ == "__main__":
    main()

