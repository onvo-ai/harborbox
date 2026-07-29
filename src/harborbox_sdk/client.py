from __future__ import annotations

from typing import Any

import httpx

from harborbox_sdk.models import Sandbox


class Sandboxes:
    def __init__(self, client: SandboxClient) -> None:
        self._client = client

    def create(
        self,
        *,
        memory_mb: int | None = None,
        cpu: float | None = None,
        idle_timeout_seconds: int | None = None,
        metadata: dict[str, str] | None = None,
    ) -> Sandbox:
        return Sandbox(
            self._client,
            self._client._request(
                "POST",
                "/v1/sandboxes",
                json={
                    "memory_mb": memory_mb,
                    "cpu": cpu,
                    "idle_timeout_seconds": idle_timeout_seconds,
                    "metadata": metadata or {},
                },
            ),
        )

    def get(self, sandbox_id: str) -> Sandbox:
        return Sandbox(
            self._client,
            self._client._request("GET", f"/v1/sandboxes/{sandbox_id}"),
        )

    def list(self) -> list[Sandbox]:
        return [
            Sandbox(self._client, payload)
            for payload in self._client._request("GET", "/v1/sandboxes")
        ]


class SandboxClient:
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        *,
        api_key: str,
        request_timeout: float = 30.0,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key},
            timeout=request_timeout,
        )
        self.sandboxes = Sandboxes(self)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, path, **kwargs)
        response.raise_for_status()
        if response.status_code == 204:
            return None
        return response.json()

    def capacity(self) -> dict[str, Any]:
        return dict(self._request("GET", "/v1/capacity"))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SandboxClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

