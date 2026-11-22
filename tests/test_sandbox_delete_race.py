"""DELETE must not leave behind a container that a start in flight creates.

DELETE killed what the runtime knew of and then wrote `killed`. A start whose
container came up between those two steps saved `running` with its container
id, and DELETE's write then replaced both with `killed` and no container. The
container ran on with no row pointing at it and no capacity counted for it.

This replays the path the TLA+ model in `docs/state-machines/models/H1/`
found, one step per model action. SQLite and a fake runtime.
"""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from harborbox import scheduler as scheduler_module
from harborbox.api import app
from harborbox.config import Settings
from harborbox.db import Base, get_session
from harborbox.errors import SandboxUnavailableError
from harborbox.models import Sandbox
from harborbox.runtime_protocol import StartedSandbox
from harborbox.scheduler import Scheduler
from harborbox.security import require_api_key

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

Sessions = async_sessionmaker[AsyncSession]

STEP_TIMEOUT_SECONDS = 10


class StartingRuntime:
    """One container, created by a start the test holds open.

    Like `OpenSandboxRuntime`, a started sandbox's handle is cached, and
    `kill` removes what the cache or the row's container id points at.
    `during_kill` runs once inside `kill`, after it has decided what to
    remove: the gap between DELETE's kill and DELETE's write.
    """

    def __init__(self) -> None:
        self.container: str | None = None
        self.cached = False
        self.release_start = asyncio.Event()
        self.during_kill: Callable[[], Awaitable[None]] | None = None

    async def start_sandbox(self, sandbox: Sandbox) -> StartedSandbox:  # noqa: ARG002
        await self.release_start.wait()
        self.container = "c-1"
        self.cached = True
        return StartedSandbox(id="c-1", name="c-1")

    async def wait_until_ready(self, sandbox: Sandbox) -> None:
        if self.container is None or sandbox.container_id != self.container:
            message = "the sandbox has no container"
            raise SandboxUnavailableError(message)

    async def container_status(self, sandbox: Sandbox) -> str | None:
        if not sandbox.container_id:
            return None
        return "running" if sandbox.container_id == self.container else "exited"

    async def kill(self, sandbox: Sandbox) -> None:
        if self.cached or (sandbox.container_id and sandbox.container_id == self.container):
            self.container = None
            self.cached = False
        hook, self.during_kill = self.during_kill, None
        if hook is not None:
            await hook()


@pytest.fixture
async def sessions(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Sessions]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler_module, "session_factory", factory)
    yield factory
    await engine.dispose()


async def test_a_delete_during_a_start_leaves_no_container_behind(
    sessions: Sessions,
) -> None:
    runtime = StartingRuntime()
    scheduler = Scheduler(Settings(max_parallel_cpu=8), runtime)  # type: ignore[arg-type]
    app.state.settings = scheduler.settings
    app.state.runtime = runtime
    app.state.scheduler = scheduler

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as opened:
            yield opened

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[require_api_key] = lambda: None
    sandbox: dict[str, Any] = {
        "id": "sbx-1",
        "status": "starting",
        "agent_token": "token",
        "memory_mb": 512,
        "cpu": 0.5,
        "pids_limit": 128,
        "idle_timeout_seconds": 300,
        "metadata_": {"template": "base"},
    }
    async with sessions() as session:
        session.add(Sandbox(**sandbox))
        await session.commit()

    # A start is in flight: OpenSandbox has not created the container yet.
    start = asyncio.create_task(scheduler._ensure_running("sbx-1"))
    await asyncio.sleep(0)

    async def start_comes_up() -> None:
        # 3. OpenSandbox creates the container.
        runtime.release_start.set()
        # 4-5. The start reads the row and saves `running`.
        await asyncio.gather(start, return_exceptions=True)

    runtime.during_kill = start_comes_up
    try:
        async with (
            asyncio.timeout(STEP_TIMEOUT_SECONDS),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://harborbox"
            ) as client,
        ):
            # 1. The caller deletes the sandbox; 2. DELETE kills what it knows
            # of; 6. DELETE saves `killed`.
            response = await client.delete("/v1/sandboxes/sbx-1")
            await asyncio.gather(start, return_exceptions=True)
    finally:
        app.dependency_overrides.clear()

    async with sessions() as session:
        stored = await session.get(Sandbox, "sbx-1")
    assert stored is not None
    assert response.status_code == HTTPStatus.NO_CONTENT
    assert stored.status == "killed"
    assert runtime.container is None
