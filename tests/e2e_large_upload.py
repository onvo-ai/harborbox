from __future__ import annotations

import os
from collections.abc import Iterator

import httpx

from harborbox_sdk import SandboxClient


def upload_chunks(megabytes: int) -> Iterator[bytes]:
    chunk = b"x" * (1024 * 1024)
    for _ in range(megabytes):
        yield chunk


def main() -> None:
    base_url = os.environ.get("HARBORBOX_BASE_URL", "http://127.0.0.1:8000")
    api_key = os.environ.get("HARBORBOX_API_KEY", "local-test-key")
    size_mb = int(os.environ.get("HARBORBOX_LARGE_UPLOAD_MB", "100"))
    sdk = SandboxClient(base_url, api_key=api_key)
    sandbox = sdk.sandboxes.create(
        memory_mb=768,
        cpu=1,
        idle_timeout_seconds=120,
        metadata={"test": "large-streaming-upload"},
    )
    try:
        sandbox.commands.run("true", wait_timeout=60).wait(raise_on_error=True)
        with httpx.Client(
            base_url=base_url,
            headers={"X-API-Key": api_key},
            timeout=180,
        ) as client:
            response = client.put(
                f"/v1/sandboxes/{sandbox.id}/files/content",
                params={"path": "/tmp/large.bin"},
                content=upload_chunks(size_mb),
                headers={"Content-Type": "application/octet-stream"},
            )
            response.raise_for_status()
            assert response.json()["size"] == size_mb * 1024 * 1024
        stat = sandbox.commands.run("stat -c%s /tmp/large.bin", wait_timeout=30)
        stat.wait(raise_on_error=True)
        assert int("".join(stat.logs.stdout).strip()) == size_mb * 1024 * 1024
        print("large streaming upload ok:", {"megabytes": size_mb})
    finally:
        sandbox.kill()
        sdk.close()


if __name__ == "__main__":
    main()
