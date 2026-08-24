from __future__ import annotations

import time
from http import HTTPStatus
from typing import Any, Self

import httpx

from live_client.models import Sandbox

# Must stay above the server's own `lazy_start_wait_timeout_seconds`
# (src/harborbox/config.py), and `test_live_client_retry.py` fails if it ever
# stops being. The two used to cross -- 30s here against 60s there -- which
# made a whole server response path unobservable from this client: a lazy
# start that ran longer than 30s raised `httpx.ReadTimeout` locally, so the
# server's own "still starting, retry me" 503 could never arrive as an HTTP
# response at all (DEV-1996).
#
# Raised here rather than lowered there on purpose. The server's 60s is sized
# against a real cold start -- container create plus health check -- so cutting
# it would turn slow-but-successful starts into failures on a loaded runner,
# in the same path Onvo Lite uses in production. This number is a test
# client's patience, and costs nothing to spend.
SERVER_LAZY_START_WAIT_TIMEOUT_SECONDS = 60.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = SERVER_LAZY_START_WAIT_TIMEOUT_SECONDS + 30.0

# How long to keep honouring `Retry-After` before giving up. Three lazy-start
# waits: a container that is not up by then is not merely slow.
DEFAULT_RETRY_BUDGET_SECONDS = 180.0


class SandboxError(Exception):
    """An HTTP error from Harborbox, carrying the body that says which one it is.

    `httpx.HTTPStatusError` reports the status line and the URL and drops the
    response body, which is exactly where Harborbox says *what* went wrong.
    A fixture dying on a lazy-start 503 therefore reported only

        Server error '503 Service Unavailable' for url '.../files'

    -- the same text whether the sandbox was still starting or its start had
    failed outright (DEV-1996). `code` and `detail` keep both.
    """

    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self.code = error_code(response)
        self.detail = error_message(response)
        request = response.request
        super().__init__(
            f"Harborbox {response.status_code} for {request.method} {request.url}: "
            f"code={self.code} detail={self.detail}"
        )


def _detail(response: httpx.Response) -> Any:  # noqa: ANN401 - an arbitrary JSON body
    """Unwrap the `detail` FastAPI puts an HTTPException in, or return the raw text."""
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict) and "detail" in payload:
        return payload["detail"]
    return payload


def error_code(response: httpx.Response) -> str | None:
    """Read the machine-readable error code, for the errors that carry one.

    Returns `None` for the ones that do not: not every error in the API is
    structured, and a caller matching on a code must see that difference
    rather than a string that happens not to match.
    """
    detail = _detail(response)
    if isinstance(detail, dict):
        code = detail.get("code")
        return str(code) if code is not None else None
    return None


def error_message(response: httpx.Response) -> str:
    detail = _detail(response)
    if isinstance(detail, dict) and "message" in detail:
        return str(detail["message"])
    return str(detail)


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Read how long the server asked the caller to wait, if it asked at all.

    The presence of the header is the retryable/terminal signal -- see
    `sandbox_starting_error` and `sandbox_start_failed_error` in the API,
    which return the same 503 status and differ by exactly this. Only the
    delta-seconds form is read: it is the only form Harborbox sends.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return max(seconds, 0.0)


class Sandboxes:
    def __init__(self, client: SandboxClient) -> None:
        self._client = client

    def create(
        self,
        *,
        template: str,
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
                    "template": template,
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
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        retry_budget_seconds: float = DEFAULT_RETRY_BUDGET_SECONDS,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key},
            timeout=request_timeout,
        )
        self._retry_budget_seconds = retry_budget_seconds
        self.sandboxes = Sandboxes(self)

    # Forwards arbitrary httpx.request kwargs (json, params, headers, ...) and
    # httpx.Response.json() itself is typed to return Any, so both are genuine.
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:  # noqa: ANN401
        """Send one request, honouring `Retry-After` until the budget runs out.

        The API documents a retryable failure on the lazy-start path -- a 503
        that says the container is still coming up -- and this client had no
        code that honoured it: a bare `raise_for_status()` turned the whole
        contract into a fixture ERROR (DEV-1996).

        Keyed on the header rather than on the status code, because the header
        *is* the server's answer to "should I retry this". That covers the
        capacity 429s from `ensure_ready` and `resume_sandbox` with the same
        four lines, and it cannot mistake a terminal 503 for a retryable one:
        `sandbox_start_failed_error` deliberately sends no `Retry-After`.

        Safe on a PUT as well as a GET: both retryable responses are raised by
        the admission and lazy-start checks that run *before* the operation
        itself, so a retried request has not half-run anything.
        """
        deadline = time.monotonic() + self._retry_budget_seconds
        while True:
            response = self._http.request(method, path, **kwargs)
            if not response.is_error:
                break
            delay = retry_after_seconds(response)
            if delay is None or time.monotonic() + delay > deadline:
                raise SandboxError(response)
            time.sleep(delay)
        if response.status_code == HTTPStatus.NO_CONTENT:
            return None
        return response.json()

    def capacity(self) -> dict[str, Any]:
        return dict(self._request("GET", "/v1/capacity"))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
