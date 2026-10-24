"""The sandbox lifecycle and OpenSandbox-compatibility routes, over HTTP.

`test_lazy_start.py` and `test_start_timeouts.py` cover how a sandbox *starts*;
`test_template_registry.py` covers which template it starts *from*. What sat
untested between them is everything a caller does to a sandbox once it exists:
pause it, resume it, kill it, list it, and the OpenSandbox-shaped surface Onvo
reaches it through. Each of those carries a rule a refactor could quietly drop
-- a pause refused while an execution is live, a kill that cancels the queue
behind it, a resume that releases its row when the runtime cannot start it --
so these tests state the rule, not just the status code.

Driven in-process through `httpx.ASGITransport` against SQLite, with a fake
runtime, the same harness the lazy-start tests use. No live stack.
"""

from __future__ import annotations

import io
import tarfile
from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from harborbox.api import SANDBOX_START_FAILED_CODE, SANDBOX_STARTING_CODE, app
from harborbox.build_contexts import BuildContextStore
from harborbox.config import Settings
from harborbox.db import Base, get_session
from harborbox.errors import SandboxStartTimeoutError, SandboxUnavailableError
from harborbox.models import Execution, Sandbox, utc_now
from harborbox.opensandbox_compat import INTERNAL_PREFIX
from harborbox.runtime_protocol import StartedSandbox, WarmPoolReservation
from harborbox.scheduler import Scheduler
from harborbox.security import require_api_key

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

Sessions = async_sessionmaker[AsyncSession]

# What the OpenSandbox limits "1Gi" and "500m" must come out as.
ONE_GIB_IN_MB = 1024
HALF_A_CPU = 0.5


class FakeRuntime:
    """Records every lifecycle call the routes make, and can be told to fail.

    Only the subset of `SandboxRuntime` these routes reach. `available_mb` is
    what admission reads as free host memory, so lowering it is how a test
    makes a resume wait for capacity.
    """

    def __init__(self, *, available_mb: int = 8192) -> None:
        self.available_mb = available_mb
        self.resume_error: Exception | None = None
        self.calls: list[tuple[str, str]] = []

    async def total_memory_mb(self) -> int:
        return 8192

    async def available_memory_mb(self) -> int:
        return self.available_mb

    def warm_pool_reservation(self) -> WarmPoolReservation:
        return WarmPoolReservation()

    async def resume(self, sandbox: Sandbox) -> StartedSandbox:
        self.calls.append(("resume", sandbox.id))
        if self.resume_error is not None:
            raise self.resume_error
        return StartedSandbox(id=f"c-{sandbox.id}", name=f"n-{sandbox.id}")

    async def wait_until_ready(self, sandbox: Sandbox) -> None:
        self.calls.append(("ready", sandbox.id))

    async def pause(self, sandbox: Sandbox, *, memory: bool) -> None:
        self.calls.append(("pause-memory" if memory else "pause-cold", sandbox.id))

    async def kill(self, sandbox: Sandbox) -> None:
        self.calls.append(("kill", sandbox.id))


def sandbox_row(**overrides: Any) -> Sandbox:  # noqa: ANN401
    values: dict[str, Any] = {
        "id": "sbx-1",
        "status": "running",
        "container_id": "c-sbx-1",
        "container_name": "n-sbx-1",
        "agent_token": "token",
        "memory_mb": 512,
        "cpu": 0.5,
        "pids_limit": 128,
        "idle_timeout_seconds": 300,
        "metadata_": {"template": "base"},
    }
    values.update(overrides)
    return Sandbox(**values)


def execution_row(**overrides: Any) -> Execution:  # noqa: ANN401
    values: dict[str, Any] = {
        "id": "exe-1",
        "sandbox_id": "sbx-1",
        "kind": "command",
        "status": "queued",
        "command": "true",
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return Execution(**values)


def gzipped_tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.fixture
async def sessions() -> AsyncIterator[Sessions]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.fixture
def settings() -> Settings:
    # A fixed CPU budget, so admission does not depend on the test machine.
    return Settings(max_parallel_cpu=8)


@pytest.fixture
async def client(
    sessions: Sessions, runtime: FakeRuntime, settings: Settings, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.settings = settings
    app.state.runtime = runtime
    app.state.scheduler = Scheduler(settings, runtime)  # type: ignore[arg-type]
    app.state.context_store = BuildContextStore(settings, root=tmp_path / "contexts")

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as opened:
            yield opened

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[require_api_key] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://harborbox"
    ) as opened:
        yield opened
    app.dependency_overrides.clear()


async def add(sessions: Sessions, *rows: Sandbox | Execution) -> None:
    async with sessions() as session:
        session.add_all(rows)
        await session.commit()


async def stored(sessions: Sessions, sandbox_id: str = "sbx-1") -> Sandbox:
    async with sessions() as session:
        sandbox = await session.get(Sandbox, sandbox_id)
    assert sandbox is not None
    return sandbox


# --- unauthenticated surface --------------------------------------------------


async def test_health_answers_ok_and_the_root_points_at_the_docs(
    client: httpx.AsyncClient,
) -> None:
    health = await client.get("/health")
    root = await client.get("/")

    assert health.json() == {"status": "ok"}
    assert root.status_code == HTTPStatus.TEMPORARY_REDIRECT
    assert root.headers["location"] == "/docs"


# --- build contexts -----------------------------------------------------------


async def test_a_build_context_is_stored_once_under_its_digest(
    client: httpx.AsyncClient,
) -> None:
    archive = gzipped_tar({"requirements.txt": b"pandas\n"})

    first = await client.post("/v1/build-contexts", content=archive)
    again = await client.post("/v1/build-contexts", content=archive)

    assert first.status_code == HTTPStatus.CREATED
    assert first.json()["digest"].startswith("sha256:")
    assert first.json()["bytes"] == len(archive)
    assert again.json()["digest"] == first.json()["digest"]


async def test_an_empty_build_context_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/build-contexts", content=b"")

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["detail"] == "build context is empty"


async def test_a_build_context_over_the_limit_is_refused_before_it_is_parsed(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    settings.template_max_context_bytes = 1024

    response = await client.post("/v1/build-contexts", content=b"x" * 1025)

    assert response.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert "1024 bytes" in response.json()["detail"]


async def test_a_build_context_that_is_not_a_tarball_is_refused_readably(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/v1/build-contexts", content=b"not a tarball")

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "not a gzipped tar archive" in response.json()["detail"]


# --- template and sandbox sizing limits ---------------------------------------


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"memory_mb": 4097}, "memory_mb cannot exceed 4096"),
        ({"cpu": 4.5}, "cpu cannot exceed 4.0"),
    ],
)
async def test_a_template_cannot_be_sized_past_the_per_sandbox_ceiling(
    client: httpx.AsyncClient, body: dict[str, Any], detail: str
) -> None:
    response = await client.post(
        "/v1/templates", json={"dockerfile": "FROM python:3.12-slim\n", **body}
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["detail"] == detail


async def test_an_invalid_dockerfile_is_refused_with_the_reason(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/v1/templates", json={"dockerfile": "RUN true\n"})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["detail"]


async def test_deleting_a_template_that_does_not_exist_is_not_found(
    client: httpx.AsyncClient,
) -> None:
    response = await client.delete("/v1/templates/custom-000000000000")

    assert response.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"memory_mb": 4097}, "memory_mb cannot exceed 4096"),
        ({"cpu": 4.5}, "cpu cannot exceed 4.0"),
    ],
)
async def test_a_sandbox_cannot_be_sized_past_the_per_sandbox_ceiling(
    client: httpx.AsyncClient, body: dict[str, Any], detail: str
) -> None:
    response = await client.post("/v1/sandboxes", json={"template": "base", **body})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["detail"] == detail


# --- reading sandboxes --------------------------------------------------------


async def test_listing_sandboxes_leaves_out_the_killed_ones(
    client: httpx.AsyncClient, sessions: Sessions
) -> None:
    await add(
        sessions,
        sandbox_row(id="sbx-live"),
        sandbox_row(id="sbx-dead", status="killed", container_id=None),
    )

    response = await client.get("/v1/sandboxes")

    assert [item["id"] for item in response.json()] == ["sbx-live"]


async def test_a_sandbox_is_fetched_by_id_and_a_missing_one_is_not_found(
    client: httpx.AsyncClient, sessions: Sessions
) -> None:
    await add(sessions, sandbox_row())

    found = await client.get("/v1/sandboxes/sbx-1")
    missing = await client.get("/v1/sandboxes/sbx-nope")

    assert found.json()["status"] == "running"
    assert missing.status_code == HTTPStatus.NOT_FOUND
    assert missing.json()["detail"] == "sandbox not found"


async def test_a_dead_sandbox_cannot_be_updated(
    client: httpx.AsyncClient, sessions: Sessions
) -> None:
    await add(sessions, sandbox_row(status="killed", container_id=None))

    response = await client.patch("/v1/sandboxes/sbx-1", json={"idle_timeout_seconds": 5})

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["detail"] == "cannot update killed"


# --- pause --------------------------------------------------------------------


async def test_a_sandbox_with_a_live_execution_cannot_be_paused(
    client: httpx.AsyncClient, sessions: Sessions, runtime: FakeRuntime
) -> None:
    await add(sessions, sandbox_row(), execution_row(status="running"))

    response = await client.post("/v1/sandboxes/sbx-1/pause", json={})

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["detail"] == "sandbox has an active execution"
    assert runtime.calls == []


async def test_a_running_sandbox_pauses_in_memory_and_keeps_its_container(
    client: httpx.AsyncClient, sessions: Sessions, runtime: FakeRuntime
) -> None:
    await add(sessions, sandbox_row())

    response = await client.post("/v1/sandboxes/sbx-1/pause", json={"memory": True})

    assert response.json()["status"] == "paused_memory"
    assert runtime.calls == [("pause-memory", "sbx-1")]
    assert (await stored(sessions)).container_id == "c-sbx-1"


async def test_a_cold_pause_from_memory_thaws_first_and_releases_the_container(
    client: httpx.AsyncClient, sessions: Sessions, runtime: FakeRuntime
) -> None:
    """The ladder's second rung: a frozen sandbox is thawed, then paused cold.

    Skipping the thaw left a frozen sandbox holding its whole memory
    reservation while the caller was told the cold pause had happened.
    """
    await add(sessions, sandbox_row(status="paused_memory"))

    response = await client.post("/v1/sandboxes/sbx-1/pause", json={"memory": False})

    assert response.json()["status"] == "paused_cold"
    assert runtime.calls == [("resume", "sbx-1"), ("pause-cold", "sbx-1")]
    sandbox = await stored(sessions)
    assert sandbox.container_id is None
    assert sandbox.container_name is None


async def test_a_dead_sandbox_cannot_be_paused(
    client: httpx.AsyncClient, sessions: Sessions
) -> None:
    await add(sessions, sandbox_row(status="failed"))

    response = await client.post("/v1/sandboxes/sbx-1/pause", json={})

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["detail"] == "cannot pause failed"


# --- resume -------------------------------------------------------------------


async def test_resuming_a_running_sandbox_is_a_no_op(
    client: httpx.AsyncClient, sessions: Sessions, runtime: FakeRuntime
) -> None:
    await add(sessions, sandbox_row())

    response = await client.post("/v1/sandboxes/sbx-1/resume")

    assert response.json()["status"] == "running"
    assert runtime.calls == []


async def test_a_killed_sandbox_cannot_be_resumed(
    client: httpx.AsyncClient, sessions: Sessions
) -> None:
    await add(sessions, sandbox_row(status="killed", container_id=None))

    response = await client.post("/v1/sandboxes/sbx-1/resume")

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["detail"] == "cannot resume killed"


async def test_a_cold_sandbox_resumes_onto_a_new_container(
    client: httpx.AsyncClient, sessions: Sessions, runtime: FakeRuntime
) -> None:
    await add(sessions, sandbox_row(status="paused_cold", container_id=None))

    response = await client.post("/v1/sandboxes/sbx-1/resume")

    assert response.json()["status"] == "running"
    assert runtime.calls == [("resume", "sbx-1"), ("ready", "sbx-1")]
    sandbox = await stored(sessions)
    assert sandbox.container_id == "c-sbx-1"
    assert sandbox.last_activity_at is not None


async def test_a_resume_without_memory_headroom_waits_rather_than_starting(
    client: httpx.AsyncClient, sessions: Sessions, runtime: FakeRuntime
) -> None:
    runtime.available_mb = 900  # 900 - 512 leaves less than the 512 MB emergency floor
    await add(sessions, sandbox_row(status="paused_cold", container_id=None))

    response = await client.post("/v1/sandboxes/sbx-1/resume")

    assert response.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert response.json()["detail"] == "insufficient memory capacity"
    assert response.headers["retry-after"] == "1"
    assert runtime.calls == []
    assert (await stored(sessions)).status == "paused_cold"


async def test_a_memory_paused_sandbox_resumes_on_the_reservation_it_already_holds(
    client: httpx.AsyncClient, sessions: Sessions, runtime: FakeRuntime
) -> None:
    """Its memory is still reserved, so it is not counted a second time."""
    runtime.available_mb = 900
    await add(sessions, sandbox_row(status="paused_memory"))

    response = await client.post("/v1/sandboxes/sbx-1/resume")

    assert response.json()["status"] == "running"


async def test_a_resume_the_runtime_cannot_start_marks_the_sandbox_failed(
    client: httpx.AsyncClient, sessions: Sessions, runtime: FakeRuntime
) -> None:
    runtime.resume_error = SandboxUnavailableError("snapshot is gone")
    await add(sessions, sandbox_row(status="paused_cold", container_id=None))

    response = await client.post("/v1/sandboxes/sbx-1/resume")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["detail"]["code"] == SANDBOX_START_FAILED_CODE
    assert "retry-after" not in response.headers
    assert (await stored(sessions)).status == "failed"


async def test_a_slow_resume_leaves_the_sandbox_retryable(
    client: httpx.AsyncClient, sessions: Sessions, runtime: FakeRuntime
) -> None:
    runtime.resume_error = SandboxStartTimeoutError("still booting")
    await add(sessions, sandbox_row(status="paused_cold", container_id=None))

    response = await client.post("/v1/sandboxes/sbx-1/resume")

    assert response.json()["detail"]["code"] == SANDBOX_STARTING_CODE
    assert response.headers["retry-after"] == "1"
    assert (await stored(sessions)).status == "starting"


# --- kill ---------------------------------------------------------------------


async def test_killing_a_sandbox_cancels_its_queue_and_releases_its_container(
    client: httpx.AsyncClient, sessions: Sessions, runtime: FakeRuntime
) -> None:
    """Queued work is cancelled durably, and work already finished is left alone.

    `cancel_requested` is the flag the scheduler re-checks, so a concurrent
    scheduler pass cannot write `admitted` back over the cancellation.
    """
    await add(
        sessions,
        sandbox_row(),
        execution_row(id="exe-queued", status="queued"),
        execution_row(id="exe-done", status="succeeded"),
    )

    response = await client.delete("/v1/sandboxes/sbx-1")

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert runtime.calls == [("kill", "sbx-1")]
    sandbox = await stored(sessions)
    assert sandbox.status == "killed"
    assert sandbox.container_id is None
    async with sessions() as session:
        executions = {
            row.id: row for row in await session.scalars(select(Execution))
        }
    assert executions["exe-queued"].status == "cancelled"
    assert executions["exe-queued"].cancel_requested is True
    assert executions["exe-queued"].finished_at is not None
    assert executions["exe-done"].status == "succeeded"


async def test_killing_a_sandbox_that_does_not_exist_is_not_found(
    client: httpx.AsyncClient, runtime: FakeRuntime
) -> None:
    response = await client.delete("/v1/sandboxes/sbx-nope")

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert runtime.calls == []


# --- OpenSandbox compatibility ------------------------------------------------


async def test_an_opensandbox_create_starts_the_sandbox_and_answers_in_its_shape(
    client: httpx.AsyncClient, sessions: Sessions, runtime: FakeRuntime
) -> None:
    response = await client.post(
        "/opensandbox/v1/sandboxes",
        json={
            "extensions": {"templateRef": "base"},
            "resourceLimits": {"memory": "1Gi", "cpu": "500m"},
            "entrypoint": ["python"],
            "metadata": {"team": "lite"},
            "timeout": 600,
        },
    )

    assert response.status_code == HTTPStatus.ACCEPTED
    body = response.json()
    assert body["status"]["state"] == "Running"
    assert body["metadata"]["team"] == "lite"
    assert "template" not in body["metadata"]
    assert body["entrypoint"] == ["python"]
    assert body["expiresAt"] is not None
    assert [call for call, _ in runtime.calls] == ["resume", "ready"]
    sandbox = await stored(sessions, body["id"])
    assert sandbox.memory_mb == ONE_GIB_IN_MB
    assert sandbox.cpu == HALF_A_CPU


async def test_an_opensandbox_create_without_limits_takes_the_deployment_defaults(
    client: httpx.AsyncClient, sessions: Sessions, settings: Settings
) -> None:
    response = await client.post(
        "/opensandbox/v1/sandboxes", json={"extensions": {"templateRef": "base"}}
    )

    sandbox = await stored(sessions, response.json()["id"])
    assert sandbox.memory_mb == settings.default_sandbox_memory_mb
    assert sandbox.cpu == settings.default_sandbox_cpu


async def test_an_opensandbox_create_with_an_unparseable_limit_is_refused(
    client: httpx.AsyncClient, sessions: Sessions
) -> None:
    response = await client.post(
        "/opensandbox/v1/sandboxes",
        json={"extensions": {"templateRef": "base"}, "resourceLimits": {"memory": "lots"}},
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["detail"]["code"] == "INVALID_REQUEST"
    async with sessions() as session:
        assert list(await session.scalars(select(Sandbox))) == []


async def test_an_opensandbox_create_that_cannot_start_leaves_no_row_behind(
    client: httpx.AsyncClient, sessions: Sessions, runtime: FakeRuntime
) -> None:
    """A sandbox the caller was refused must not linger as `created`.

    It would otherwise hold its place in the list -- and, once something
    lazily started it, its memory -- for a create that answered with an error.
    """
    runtime.available_mb = 900

    response = await client.post(
        "/opensandbox/v1/sandboxes", json={"extensions": {"templateRef": "base"}}
    )

    assert response.status_code == HTTPStatus.TOO_MANY_REQUESTS
    async with sessions() as session:
        assert list(await session.scalars(select(Sandbox))) == []


async def test_the_opensandbox_list_filters_by_state_and_pages(
    client: httpx.AsyncClient, sessions: Sessions
) -> None:
    now = utc_now()
    await add(
        sessions,
        *(
            sandbox_row(id=f"sbx-run-{index}", created_at=now - timedelta(minutes=index))
            for index in range(3)
        ),
        sandbox_row(id="sbx-paused", status="paused_cold", container_id=None),
    )

    first = await client.get(
        "/opensandbox/v1/sandboxes", params={"state": "running", "pageSize": 2}
    )
    second = await client.get(
        "/opensandbox/v1/sandboxes",
        params={"state": "RUNNING", "pageSize": 2, "page": 2},
    )

    assert [item["id"] for item in first.json()["items"]] == ["sbx-run-0", "sbx-run-1"]
    assert first.json()["pagination"] == {
        "page": 1,
        "pageSize": 2,
        "totalItems": 3,
        "totalPages": 2,
        "hasNextPage": True,
    }
    assert [item["id"] for item in second.json()["items"]] == ["sbx-run-2"]
    assert second.json()["pagination"]["hasNextPage"] is False


async def test_the_opensandbox_list_without_a_filter_returns_every_state(
    client: httpx.AsyncClient, sessions: Sessions
) -> None:
    rows = [sandbox_row(), sandbox_row(id="sbx-2", status="killed")]
    await add(sessions, *rows)

    response = await client.get("/opensandbox/v1/sandboxes")

    assert response.json()["pagination"]["totalItems"] == len(rows)


async def test_an_opensandbox_get_hides_harborbox_metadata(
    client: httpx.AsyncClient, sessions: Sessions
) -> None:
    await add(
        sessions,
        sandbox_row(metadata_={"template": "base", "team": "lite", f"{INTERNAL_PREFIX}image": ""}),
    )

    response = await client.get("/opensandbox/v1/sandboxes/sbx-1")

    assert response.json()["metadata"] == {"team": "lite"}
    assert response.json()["status"]["state"] == "Running"


async def test_an_opensandbox_delete_kills_the_sandbox(
    client: httpx.AsyncClient, sessions: Sessions, runtime: FakeRuntime
) -> None:
    await add(sessions, sandbox_row())

    response = await client.delete("/opensandbox/v1/sandboxes/sbx-1")

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert runtime.calls == [("kill", "sbx-1")]
    assert (await stored(sessions)).status == "killed"


async def test_an_opensandbox_pause_is_a_memory_pause_and_resume_brings_it_back(
    client: httpx.AsyncClient, sessions: Sessions, runtime: FakeRuntime
) -> None:
    await add(sessions, sandbox_row())

    paused = await client.post("/opensandbox/v1/sandboxes/sbx-1/pause")
    resumed = await client.post("/opensandbox/v1/sandboxes/sbx-1/resume")

    assert paused.status_code == HTTPStatus.ACCEPTED
    assert paused.json()["status"]["state"] == "Paused"
    assert paused.json()["status"]["reason"] == "memory_paused"
    assert resumed.json()["status"]["state"] == "Running"
    assert runtime.calls == [
        ("pause-memory", "sbx-1"),
        ("resume", "sbx-1"),
        ("ready", "sbx-1"),
    ]


async def test_renewing_an_expiration_stores_it_in_utc(
    client: httpx.AsyncClient, sessions: Sessions
) -> None:
    await add(sessions, sandbox_row())
    later = (utc_now() + timedelta(hours=1)).astimezone()

    response = await client.post(
        "/opensandbox/v1/sandboxes/sbx-1/renew-expiration",
        json={"expiresAt": later.isoformat()},
    )

    assert response.status_code == HTTPStatus.OK
    stored_value = (await stored(sessions)).metadata_[f"{INTERNAL_PREFIX}expires_at"]
    assert stored_value.endswith("+00:00")
    fetched = await client.get("/opensandbox/v1/sandboxes/sbx-1")
    assert fetched.json()["expiresAt"] is not None


async def test_an_expiration_in_the_past_is_refused(
    client: httpx.AsyncClient, sessions: Sessions
) -> None:
    await add(sessions, sandbox_row())
    earlier = utc_now() - timedelta(minutes=1)

    response = await client.post(
        "/opensandbox/v1/sandboxes/sbx-1/renew-expiration",
        json={"expiresAt": earlier.isoformat()},
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["detail"]["message"] == "expiresAt must be in the future"
    assert f"{INTERNAL_PREFIX}expires_at" not in (await stored(sessions)).metadata_


async def test_patching_metadata_sets_and_removes_keys(
    client: httpx.AsyncClient, sessions: Sessions
) -> None:
    await add(sessions, sandbox_row(metadata_={"template": "base", "old": "1", "keep": "2"}))

    response = await client.patch(
        "/opensandbox/v1/sandboxes/sbx-1/metadata", json={"old": None, "new": "3"}
    )

    assert response.json()["metadata"] == {"keep": "2", "new": "3"}
    assert (await stored(sessions)).metadata_["template"] == "base"


async def test_harborbox_metadata_keys_cannot_be_patched(
    client: httpx.AsyncClient, sessions: Sessions
) -> None:
    """The internal prefix holds the expiry and the image; a caller must not forge them."""
    await add(sessions, sandbox_row())

    response = await client.patch(
        "/opensandbox/v1/sandboxes/sbx-1/metadata",
        json={f"{INTERNAL_PREFIX}expires_at": "2099-01-01T00:00:00+00:00"},
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "reserved" in response.json()["detail"]["message"]
    assert f"{INTERNAL_PREFIX}expires_at" not in (await stored(sessions)).metadata_
