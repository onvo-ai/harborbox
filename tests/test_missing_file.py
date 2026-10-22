"""A path that does not exist is the caller's 404, not the sandbox's 500.

Every file call used to route an execd error through `_raise_runtime_error`,
which calls the sandbox unavailable. Nothing in the API caught that, so asking
for a file that was not there answered 500 Internal Server Error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from opensandbox.exceptions import SandboxApiException, SandboxError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from harborbox import scheduler as scheduler_module
from harborbox.api import app
from harborbox.config import Settings
from harborbox.db import Base, get_session
from harborbox.errors import SandboxFileNotFoundError, SandboxUnavailableError
from harborbox.models import Sandbox
from harborbox.opensandbox_runtime import FILE_NOT_FOUND_CODE, OpenSandboxRuntime
from harborbox.security import require_api_key

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

Sessions = async_sessionmaker[AsyncSession]

MISSING = "/workspace/nope.txt"


def sandbox_row(**overrides: Any) -> Sandbox:  # noqa: ANN401
    values: dict[str, Any] = {
        "id": "sbx-files",
        "status": "running",
        "container_id": "osb-files",
        "container_name": None,
        "agent_token": "test-token",
        "memory_mb": 256,
        "cpu": 0.5,
        "pids_limit": 128,
        "idle_timeout_seconds": 300,
        "metadata_": {"template": "base"},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "last_activity_at": datetime.now(UTC),
    }
    values.update(overrides)
    return Sandbox(**values)


def execd_error(code: str) -> SandboxApiException:
    """Build what the SDK raises for an execd 404, as seen on a live stack."""
    return SandboxApiException(
        f"API error: HTTP 404: [{code}] file not found",
        status_code=404,
        error=SandboxError(code, "file not found"),
    )


class FailingFiles:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def read_bytes(self, *_: object) -> bytes:
        raise self.error

    async def list_directory(self, *_: object) -> list[object]:
        raise self.error

    async def get_file_info(self, *_: object) -> dict[str, object]:
        raise self.error


def runtime_whose_files_raise(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> OpenSandboxRuntime:
    runtime = OpenSandboxRuntime(Settings())
    handle = SimpleNamespace(files=FailingFiles(error))

    async def get_handle(*_: object, **__: object) -> SimpleNamespace:
        return handle

    monkeypatch.setattr(runtime, "_get_handle", get_handle)
    return runtime


def file_calls(
    runtime: OpenSandboxRuntime,
) -> list[Callable[[], Awaitable[object]]]:
    sandbox = sandbox_row(container_id=None)
    return [
        lambda: runtime.read_file(sandbox, MISSING),
        lambda: runtime.list_files(sandbox, MISSING),
        lambda: runtime.remove_file(sandbox, MISSING),
    ]


@pytest.mark.asyncio
async def test_execds_file_not_found_becomes_a_missing_file_for_every_file_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = runtime_whose_files_raise(monkeypatch, execd_error(FILE_NOT_FOUND_CODE))

    for call in file_calls(runtime):
        with pytest.raises(SandboxFileNotFoundError, match=MISSING):
            await call()
    await runtime.close()


@pytest.mark.asyncio
async def test_a_404_that_is_not_a_missing_file_still_means_the_sandbox_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the server proxy, a vanished sandbox is a 404 too.

    That is why the match is on execd's code and not on the status.
    """
    runtime = runtime_whose_files_raise(monkeypatch, execd_error("SANDBOX_NOT_FOUND"))

    for call in file_calls(runtime):
        with pytest.raises(SandboxUnavailableError):
            await call()
    await runtime.close()


class MissingFileRuntime:
    """Just enough runtime for the file endpoints on a running sandbox."""

    async def read_file(self, _sandbox: Sandbox, path: str) -> None:
        raise SandboxFileNotFoundError(path)

    async def list_files(self, _sandbox: Sandbox, path: str) -> None:
        raise SandboxFileNotFoundError(path)

    async def remove_file(self, _sandbox: Sandbox, path: str) -> None:
        raise SandboxFileNotFoundError(path)


@pytest.fixture
async def sessions() -> AsyncIterator[Sessions]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def client(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    settings = Settings()
    runtime = MissingFileRuntime()
    app.state.settings = settings
    app.state.runtime = runtime
    app.state.scheduler = scheduler_module.Scheduler(settings, runtime)  # type: ignore[arg-type]

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


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_a_missing_path_answers_404_and_leaves_the_sandbox_running(
    client: httpx.AsyncClient, sessions: Sessions, method: str
) -> None:
    async with sessions() as session:
        session.add(sandbox_row())
        await session.commit()

    response = await client.request(
        method, "/v1/sandboxes/sbx-files/files", params={"path": MISSING}
    )

    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
    assert MISSING in response.json()["detail"]
    async with sessions() as session:
        sandbox = await session.get(Sandbox, "sbx-files")
        assert sandbox is not None
        assert sandbox.status == "running"


@pytest.mark.asyncio
async def test_listing_a_missing_directory_answers_404(
    client: httpx.AsyncClient, sessions: Sessions
) -> None:
    async with sessions() as session:
        session.add(sandbox_row())
        await session.commit()

    response = await client.get(
        "/v1/sandboxes/sbx-files/files/list", params={"path": MISSING}
    )

    assert response.status_code == HTTPStatus.NOT_FOUND, response.text
