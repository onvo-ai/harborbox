"""The e2e suite's HTTP client against the lazy-start 503 contract (DEV-1996).

`tests/e2e_pause_ladder.py`'s `sandbox` fixture died in setup roughly one run
in six, on the `files.write` that deliberately forces a lazy start. Two things
made that unfixable rather than merely intermittent.

The client had no 503 handling at all -- a bare `raise_for_status()` -- so the
"still starting, retry me" 503 the API documents had no caller that honoured
it. And the two timeouts crossed: 30s here against the server's 60s, so a
start slower than 30s raised `httpx.ReadTimeout` locally and the server's own
response never arrived. These tests hold both fixed.
"""

from __future__ import annotations

import httpx
import pytest
from live_client.client import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    SERVER_LAZY_START_WAIT_TIMEOUT_SECONDS,
    SandboxClient,
    SandboxError,
)

from harborbox.api import SANDBOX_START_FAILED_CODE, SANDBOX_STARTING_CODE
from harborbox.config import Settings

# Named so the "retry until it works" tests can assert an exact call count
# without a bare integer sitting in the assertion.
ATTEMPTS_BEFORE_READY = 3
ATTEMPTS_BEFORE_ADMITTED = 2


def client_on(transport: httpx.MockTransport, **kwargs: float) -> SandboxClient:
    live = SandboxClient(api_key="test-key", **kwargs)
    live._http = httpx.Client(  # the seam a MockTransport needs
        base_url="http://harborbox",
        headers={"X-API-Key": "test-key"},
        transport=transport,
    )
    return live


def test_the_client_timeout_stays_above_the_server_start_budget() -> None:
    """The inversion that made a whole server branch unobservable.

    Read this as one assertion in two halves: the client's patience must
    exceed the server's, and the number it is measured against must still be
    the server's actual default. Change `lazy_start_wait_timeout_seconds` in
    config.py without changing the client and this fails, which is the point
    -- the previous crossing was invisible precisely because nothing compared
    them.
    """
    assert Settings().lazy_start_wait_timeout_seconds == SERVER_LAZY_START_WAIT_TIMEOUT_SECONDS
    assert DEFAULT_REQUEST_TIMEOUT_SECONDS > SERVER_LAZY_START_WAIT_TIMEOUT_SECONDS


def test_a_still_starting_503_is_retried_until_it_succeeds() -> None:
    """The contract `ensure_ready` promises, now with a client that honours it."""
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < ATTEMPTS_BEFORE_READY:
            return httpx.Response(
                503,
                json={"detail": {"code": SANDBOX_STARTING_CODE, "message": "still starting"}},
                headers={"Retry-After": "0"},
            )
        return httpx.Response(200, json={"path": "/workspace/probe", "content": "ok"})

    with client_on(httpx.MockTransport(handler)) as live:
        assert live._request("PUT", "/v1/sandboxes/sbx-1/files") == {
            "path": "/workspace/probe",
            "content": "ok",
        }
    assert len(attempts) == ATTEMPTS_BEFORE_READY


def test_a_failed_start_503_is_not_retried() -> None:
    """The other branch, and the reason a bare status code is not enough.

    Same 503, opposite meaning. No `Retry-After`, so the client stops on the
    first response instead of spending its whole budget re-reading a `failed`
    row.
    """
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(
            503,
            json={"detail": {"code": SANDBOX_START_FAILED_CODE, "message": "upstream boom"}},
        )

    with client_on(httpx.MockTransport(handler)) as live, pytest.raises(SandboxError) as caught:
        live._request("PUT", "/v1/sandboxes/sbx-2/files")

    assert attempts == [1]
    assert caught.value.status_code == httpx.codes.SERVICE_UNAVAILABLE
    assert caught.value.code == SANDBOX_START_FAILED_CODE
    assert caught.value.detail == "upstream boom"
    # The evidence the fixture ERROR never carried: which 503 this was.
    assert SANDBOX_START_FAILED_CODE in str(caught.value)
    assert "upstream boom" in str(caught.value)


def test_the_retry_budget_bounds_a_server_that_never_becomes_ready() -> None:
    """A sandbox that is *always* "still starting" must still end the test."""
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(
            503,
            json={"detail": {"code": SANDBOX_STARTING_CODE, "message": "still starting"}},
            headers={"Retry-After": "0"},
        )

    transport = httpx.MockTransport(handler)
    with client_on(transport, retry_budget_seconds=0.05) as live, pytest.raises(SandboxError):
        live._request("PUT", "/v1/sandboxes/sbx-3/files")

    assert attempts


def test_a_capacity_429_is_retried_on_the_same_header() -> None:
    """`Retry-After` is the whole rule, so the capacity 429s come along free.

    `ensure_ready` and `resume_sandbox` both already send `Retry-After: 1` on
    a capacity refusal. Keying on the header rather than on 503 means the
    client honours those too without a second code path.
    """
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < ATTEMPTS_BEFORE_ADMITTED:
            return httpx.Response(
                429,
                json={"detail": "insufficient memory capacity"},
                headers={"Retry-After": "0"},
            )
        return httpx.Response(204)

    with client_on(httpx.MockTransport(handler)) as live:
        assert live._request("POST", "/v1/sandboxes/sbx-4/resume") is None
    assert len(attempts) == ATTEMPTS_BEFORE_ADMITTED


def test_an_unstructured_error_body_still_reaches_the_caller() -> None:
    """Not every error in the API is structured, and a plain body must survive.

    `code` is `None` rather than a string that happens not to match any known
    code, so a caller branching on it sees the difference.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="template onvo-lite does not exist")

    with client_on(httpx.MockTransport(handler)) as live, pytest.raises(SandboxError) as caught:
        live._request("POST", "/v1/sandboxes")

    assert caught.value.code is None
    assert "template onvo-lite does not exist" in str(caught.value)
