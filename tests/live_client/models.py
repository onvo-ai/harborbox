from __future__ import annotations

import base64
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from live_client.client import SandboxClient

TERMINAL_STATES = {"succeeded", "failed", "cancelled"}

# Bounds for `Execution.wait`'s fallback polling. It starts tight so a result
# that lands just after submission is picked up almost at once, and backs off so
# a long execution is not polled hundreds of times to no purpose.
MIN_POLL_INTERVAL = 0.02
MAX_POLL_INTERVAL = 1.0

# Extra seconds allowed on an inline-wait HTTP request, over the execution
# budget the server is being asked to wait for. Without it the client's own
# request timeout would fire first and turn a working inline wait into a
# spurious connection error.
INLINE_WAIT_HTTP_MARGIN = 15.0

# Assumed server-side execution budget when the caller names no timeout. Matches
# `Settings.default_execution_timeout_seconds`; only used to size the client's
# own HTTP timeout, so drift costs a fallback to polling, never a wrong result.
DEFAULT_INLINE_WAIT_SECONDS = 30.0


def _inline_wait_options(
    *, wait: bool, wait_timeout: float | None, timeout: int | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split inline-wait settings into request-body fields and httpx kwargs.

    The body half asks the server to hold the connection; the httpx half widens
    this one request's timeout so the client does not give up while the server
    is still legitimately waiting. Kept separate from the body so
    getting only one of the two halves right cannot be a silent bug.
    """
    body: dict[str, Any] = {"wait": wait}
    if not wait:
        return body, {}
    body["wait_timeout_seconds"] = math.ceil(wait_timeout) if wait_timeout else None
    budget = wait_timeout or timeout or DEFAULT_INLINE_WAIT_SECONDS
    return body, {"timeout": budget + INLINE_WAIT_HTTP_MARGIN}


@dataclass(frozen=True)
class Logs:
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True)
class ExecutionResult:
    text: str | None = None
    json: Any | None = None
    html: str | None = None
    png: str | None = None
    jpeg: str | None = None
    svg: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionError:
    name: str
    value: str
    traceback: list[str] = field(default_factory=list)


class Execution:
    def __init__(self, client: SandboxClient, payload: dict[str, Any]) -> None:
        self._client = client
        self._apply(payload)

    def _apply(self, payload: dict[str, Any]) -> None:
        self.id = str(payload["id"])
        self.sandbox_id = str(payload["sandbox_id"])
        self.kind = str(payload["kind"])
        self.status = str(payload["status"])
        self.queue_position = payload.get("queue_position")
        self.waiting_for = payload.get("waiting_for")
        logs = payload.get("logs") or {}
        self.logs = Logs(
            stdout=list(logs.get("stdout", [])),
            stderr=list(logs.get("stderr", [])),
            truncated=bool(logs.get("truncated", False)),
        )
        self.results = [
            ExecutionResult(
                text=item.get("text"),
                json=item.get("json"),
                html=item.get("html"),
                png=item.get("png"),
                jpeg=item.get("jpeg"),
                svg=item.get("svg"),
                data=item.get("data") or {},
            )
            for item in payload.get("results", [])
        ]
        error = payload.get("error")
        self.error = (
            ExecutionError(
                name=error["name"],
                value=error["value"],
                traceback=list(error.get("traceback", [])),
            )
            if error
            else None
        )
        self.exit_code = payload.get("exit_code")
        self.queued_ms = payload.get("queued_ms")
        self.execution_ms = payload.get("execution_ms")
        self.created_at = payload.get("created_at")
        self.admitted_at = payload.get("admitted_at")
        self.started_at = payload.get("started_at")
        self.finished_at = payload.get("finished_at")

    @property
    def text(self) -> str | None:
        for result in reversed(self.results):
            if result.text is not None:
                return result.text
        return None

    def refresh(self) -> Execution:
        self._apply(self._client._request("GET", f"/v1/executions/{self.id}"))
        return self

    def wait(
        self,
        timeout: float | None = None,
        *,
        poll_interval: float | None = None,
        raise_on_error: bool = False,
    ) -> Execution:
        """Poll until this execution finishes.

        This is the fallback path now: `commands.run` asks the server to hold
        the connection instead, and only land here when the
        execution outlives the server's willingness to wait. It stays public
        because a caller who submitted with `wait=False` still needs it.

        The interval backs off from `MIN_POLL_INTERVAL` to `MAX_POLL_INTERVAL`
        rather than sitting at a flat 200 ms, so a result that arrives just
        after submission is seen almost immediately while a long execution is
        not polled hundreds of times. Passing `poll_interval` pins it flat, as
        before.
        """
        deadline = time.monotonic() + timeout if timeout is not None else None
        interval = poll_interval if poll_interval is not None else MIN_POLL_INTERVAL
        while self.status not in TERMINAL_STATES:
            if deadline is not None and time.monotonic() >= deadline:
                timeout_message = f"execution {self.id} did not finish in time"
                raise TimeoutError(timeout_message)
            time.sleep(interval)
            if poll_interval is None:
                interval = min(interval * 2, MAX_POLL_INTERVAL)
            self.refresh()
        if raise_on_error and self.status != "succeeded":
            message = self.error.value if self.error else self.status
            raise RuntimeError(message)
        return self

    def cancel(self) -> Execution:
        self._apply(
            self._client._request("POST", f"/v1/executions/{self.id}/cancel")
        )
        return self


class Commands:
    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def run(  # noqa: PLR0913 - a public SDK convenience wrapper; each keyword is
        # independently useful to callers (`sandbox.commands.run("ls")` stays the
        # common case), and bundling them into an options object would make that
        # call site worse, not better.
        self,
        command: str,
        *,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        wait: bool = True,
        wait_timeout: float | None = None,
    ) -> Execution:
        body, request_options = _inline_wait_options(
            wait=wait, wait_timeout=wait_timeout, timeout=timeout
        )
        execution = Execution(
            self._sandbox._client,
            self._sandbox._client._request(
                "POST",
                f"/v1/sandboxes/{self._sandbox.id}/commands",
                json={
                    "command": command,
                    "timeout_seconds": timeout,
                    "env": env or {},
                    "cwd": cwd,
                    **body,
                },
                **request_options,
            ),
        )
        if not wait or execution.status in TERMINAL_STATES:
            return execution
        return execution.wait(wait_timeout)


class Files:
    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def read(self, path: str) -> str:
        payload = self._sandbox._client._request(
            "GET",
            f"/v1/sandboxes/{self._sandbox.id}/files",
            params={"path": path},
        )
        if payload["encoding"] == "base64":
            return base64.b64decode(payload["content"]).decode("utf-8")
        return str(payload["content"])

    def write(self, path: str, content: str) -> str:
        payload = self._sandbox._client._request(
            "PUT",
            f"/v1/sandboxes/{self._sandbox.id}/files",
            json={"path": path, "content": content, "encoding": "utf-8"},
        )
        return str(payload["content"])

    def write_bytes(self, path: str, content: bytes) -> int:
        payload = self._sandbox._client._request(
            "PUT",
            f"/v1/sandboxes/{self._sandbox.id}/files/content",
            params={"path": path},
            content=content,
            headers={"Content-Type": "application/octet-stream"},
        )
        return int(payload["size"])

    def list(self, path: str = ".") -> list[dict[str, Any]]:
        payload = self._sandbox._client._request(
            "GET",
            f"/v1/sandboxes/{self._sandbox.id}/files/list",
            params={"path": path},
        )
        return list(payload["entries"])

    def remove(self, path: str) -> None:
        self._sandbox._client._request(
            "DELETE",
            f"/v1/sandboxes/{self._sandbox.id}/files",
            params={"path": path},
        )


class Sandbox:
    def __init__(self, client: SandboxClient, payload: dict[str, Any]) -> None:
        self._client = client
        self._apply(payload)
        self.commands = Commands(self)
        self.files = Files(self)

    def _apply(self, payload: dict[str, Any]) -> None:
        self.id = str(payload["id"])
        self.status = str(payload["status"])
        self.memory_mb = int(payload["memory_mb"])
        self.cpu = float(payload["cpu"])
        self.idle_timeout_seconds = int(payload["idle_timeout_seconds"])
        self.metadata = dict(payload.get("metadata", {}))

    def refresh(self) -> Sandbox:
        self._apply(self._client._request("GET", f"/v1/sandboxes/{self.id}"))
        return self

    def set_timeout(self, timeout_ms: int) -> Sandbox:
        if timeout_ms < 0:
            message = "timeout_ms must be non-negative"
            raise ValueError(message)
        self._apply(
            self._client._request(
                "PATCH",
                f"/v1/sandboxes/{self.id}",
                json={"idle_timeout_seconds": math.ceil(timeout_ms / 1000)},
            )
        )
        return self

    def touch(self) -> Sandbox:
        self._apply(
            self._client._request(
                "PATCH",
                f"/v1/sandboxes/{self.id}",
                json={},
            )
        )
        return self

    def pause(self, *, memory: bool = True) -> Sandbox:
        self._apply(
            self._client._request(
                "POST",
                f"/v1/sandboxes/{self.id}/pause",
                json={"memory": memory},
            )
        )
        return self

    def resume(self) -> Sandbox:
        self._apply(
            self._client._request("POST", f"/v1/sandboxes/{self.id}/resume")
        )
        return self

    def kill(self) -> None:
        self._client._request("DELETE", f"/v1/sandboxes/{self.id}")
        self.status = "killed"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.kill()
