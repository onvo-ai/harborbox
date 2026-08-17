from __future__ import annotations

import asyncio
import base64
import io
import json
import subprocess
import tarfile
from datetime import UTC, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn, Self

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from harborbox import build_contexts
from harborbox import scheduler as scheduler_module
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

RAW_DOCKERFILE = "FROM debian:bookworm-slim\nRUN apt-get update\n"

# Settings()'s default warm pool size for the statically registered base.
BASE_WARM_POOL = 1


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
    *, dockerfile: str | None = None, **overrides: Any  # noqa: ANN401
) -> SandboxTemplate:
    settings = Settings()
    spec = validate_template_spec(
        settings, dockerfile=dockerfile or RAW_DOCKERFILE
    )
    values: dict[str, Any] = {
        "name": spec.name,
        "base": "",
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
    resolved = await resolve_template(session, Settings(), "base")

    assert resolved.image == "harborbox-sandbox-base:local"
    assert (resolved.memory_mb, resolved.cpu) == (512, 1.0)
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
    assert resolved.image == template.image
    # The registry's override wins over the base template's static sizing.
    assert (resolved.memory_mb, resolved.cpu) == (512, 1.0)


async def test_unknown_templates_are_rejected(session: AsyncSession) -> None:
    settings = Settings()

    with pytest.raises(UnknownTemplateError):
        await resolve_template(session, settings, "not-a-template")
    # Well-formed, but never registered.
    with pytest.raises(UnknownTemplateError):
        await resolve_template(session, settings, "custom-a1b2c3d4e5f6")


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
    payload = {"dockerfile": RAW_DOCKERFILE}

    first = await client.post("/v1/templates", json=payload)
    second = await client.post("/v1/templates", json=payload)
    # A byte-different Dockerfile is a different image, even trivially so.
    third = await client.post(
        "/v1/templates", json={"dockerfile": RAW_DOCKERFILE + "RUN true\n"}
    )

    assert (first.status_code, second.status_code, third.status_code) == (201, 200, 201)
    name = first.json()["name"]
    assert second.json()["name"] == name
    assert third.json()["name"] != name
    assert name == f"custom-{first.json()['spec_hash']}"
    assert first.json()["image"] == f"harborbox-sandbox-{name}:local"
    assert first.json()["status"] == "building"
    assert first.json()["error"] is None
    assert builder.scheduled == [name, third.json()["name"]]



async def test_a_failed_template_is_rebuilt(
    client: httpx.AsyncClient, session: AsyncSession, builder: FakeTemplateBuilder
) -> None:
    session.add(derived_row(status="failed", error="E: Unable to locate package"))
    await session.commit()

    response = await client.post("/v1/templates", json={"dockerfile": RAW_DOCKERFILE})

    assert response.status_code == HTTPStatus.CREATED
    assert response.json()["status"] == "building"
    assert response.json()["error"] is None
    assert builder.scheduled == [response.json()["name"]]



async def test_templates_are_listed_and_fetched(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    template = derived_row()
    session.add(template)
    await session.commit()

    listed = await client.get("/v1/templates")
    fetched = await client.get(f"/v1/templates/{template.name}")
    static = await client.get("/v1/templates/base")
    missing = await client.get("/v1/templates/custom-000000000000")

    assert [item["name"] for item in listed.json()["templates"]] == [
        "base",
        template.name,
    ]
    assert fetched.status_code == HTTPStatus.OK
    assert fetched.json()["spec_hash"] == template.spec_hash
    assert static.json()["warm_pool"] == BASE_WARM_POOL
    assert missing.status_code == HTTPStatus.NOT_FOUND


async def test_deleting_a_template_refuses_static_names(
    client: httpx.AsyncClient, session: AsyncSession, builder: FakeTemplateBuilder
) -> None:
    template = derived_row()
    session.add(template)
    await session.commit()

    static = await client.delete("/v1/templates/base")
    derived = await client.delete(f"/v1/templates/{template.name}")

    assert static.status_code == HTTPStatus.CONFLICT
    assert derived.status_code == HTTPStatus.NO_CONTENT
    assert builder.removed == [template.image]
    assert (await client.get(f"/v1/templates/{template.name}")).status_code == HTTPStatus.NOT_FOUND


async def test_creating_a_sandbox_resolves_against_the_registry(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    used_before = utc_now() - timedelta(days=1)
    template = derived_row(last_used_at=used_before)
    session.add(template)
    await session.commit()

    created = await client.post("/v1/sandboxes", json={"template": template.name})
    unknown = await client.post(
        "/v1/sandboxes", json={"template": "custom-000000000000"}
    )

    # derived_row()'s default sizing.
    default_memory_mb = 512
    default_cpu = 1.0
    assert created.status_code == HTTPStatus.CREATED
    assert created.json()["memory_mb"] == default_memory_mb
    assert created.json()["cpu"] == default_cpu
    assert created.json()["metadata"]["template"] == template.name
    assert created.json()["metadata"]["template_spec_hash"] == template.spec_hash
    assert unknown.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

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

    assert response.status_code == HTTPStatus.CONFLICT
    assert "building" in response.json()["detail"]


def test_a_failed_build_records_the_log_tail_not_a_traceback() -> None:
    # Real BuildKit output for a package that does not resolve.
    output = (
        "#4 [1/2] FROM docker.io/library/harborbox-sandbox-base:local\n"
        "#4 CACHED\n"
        "\n"
        "#5 [2/2] RUN apt-get install -y nosuchpackage-xyz\n"
        "#5 0.113 E: Unable to locate package nosuchpackage-xyz\n"
        "#5 ERROR: process did not complete successfully: exit code: 100\n"
        "ERROR: failed to build: failed to solve: exit code: 100"
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


def record_invocations(
    monkeypatch: pytest.MonkeyPatch, calls: list[list[str]]
) -> None:
    """Capture argv instead of shelling out, and report success."""

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(builder_module.subprocess, "run", fake_run)


REGISTRY_UNREACHABLE = "registry unreachable"

BUILDER_SETTINGS = {
    "builder_address": "tcp://builder:1234",
    "registry_push_endpoint": "registry:5000",
    "registry_pull_endpoint": "127.0.0.1:5050",
}


def test_a_configured_builder_pushes_to_the_registry_instead_of_a_local_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    record_invocations(monkeypatch, calls)
    builder = builder_module.TemplateBuilder(Settings(**BUILDER_SETTINGS))

    builder._build_sync(
        "FROM scratch\n", "registry:5000/harborbox-sandbox-custom-a1b2c3d4e5f6:local"
    )

    argv = calls[0]
    joined = " ".join(argv)
    assert argv[0].endswith("buildctl")
    assert "docker" not in argv[0]
    assert "--addr" in argv
    assert argv[argv.index("--addr") + 1] == "tcp://builder:1234"
    assert (
        "type=image,"
        "name=registry:5000/harborbox-sandbox-custom-a1b2c3d4e5f6:local,"
        "push=true,registry.insecure=true" in joined
    )


def test_the_build_context_handed_to_buildkit_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No context means a generated Dockerfile cannot COPY off the build host.

    `docker build -` got this for free by sending no context at all. buildctl
    has no equivalent, so the guarantee now rests on the directory actually
    being empty -- which is worth asserting rather than assuming.
    """
    contexts: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        local = [argv[index + 1] for index, item in enumerate(argv) if item == "--local"]
        context = next(v.removeprefix("context=") for v in local if v.startswith("context="))
        contexts.append(sorted(Path(context).iterdir()))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(builder_module.subprocess, "run", fake_run)
    builder = builder_module.TemplateBuilder(Settings(**BUILDER_SETTINGS))

    builder._build_sync("FROM scratch\nCOPY . /x\n", "registry:5000/x:local")

    assert contexts == [[]]


def test_the_dockerfile_reaches_buildkit_through_its_own_local_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        local = [argv[index + 1] for index, item in enumerate(argv) if item == "--local"]
        directory = next(
            v.removeprefix("dockerfile=") for v in local if v.startswith("dockerfile=")
        )
        seen.append((Path(directory) / "Dockerfile").read_text())
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(builder_module.subprocess, "run", fake_run)
    builder = builder_module.TemplateBuilder(Settings(**BUILDER_SETTINGS))

    builder._build_sync("FROM scratch\nRUN true\n", "registry:5000/x:local")

    assert seen == ["FROM scratch\nRUN true\n"]


def test_without_a_builder_the_local_daemon_path_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The builder is opt-in; a deployment with no registry still builds locally."""
    calls: list[list[str]] = []
    record_invocations(monkeypatch, calls)
    builder = builder_module.TemplateBuilder(Settings())

    builder._build_sync("FROM scratch\n", "harborbox-sandbox-base:local")

    argv = calls[0]
    assert argv[0].endswith("docker")
    assert argv[1] == "build"
    assert argv[-1] == "-"


async def test_a_build_targets_the_push_reference_not_the_stored_pull_one(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row stores how the daemon pulls; the build has to use how BuildKit pushes.

    Pushing to the pull reference would send the image to whatever
    `127.0.0.1:5050` means inside the builder's own network namespace, which is
    not the registry.
    """
    settings = Settings(**BUILDER_SETTINGS)
    template = derived_row(
        status="building", image=settings.image_for_template("custom-a1b2c3d4e5f6")
    )
    template.name = "custom-a1b2c3d4e5f6"
    async with sessions() as session:
        session.add(template)
        await session.commit()

    monkeypatch.setattr(builder_module, "session_factory", sessions)
    calls: list[list[str]] = []
    record_invocations(monkeypatch, calls)
    builder = builder_module.TemplateBuilder(settings)

    await builder._build("custom-a1b2c3d4e5f6")

    joined = " ".join(calls[0])
    assert "name=registry:5000/harborbox-sandbox-custom-a1b2c3d4e5f6:local" in joined
    assert "127.0.0.1:5050" not in joined
    async with sessions() as session:
        rebuilt = await session.get(SandboxTemplate, "custom-a1b2c3d4e5f6")
        assert rebuilt is not None
        assert rebuilt.status == "ready"
        # The row keeps the pull reference: it is what reaches opensandbox.
        assert rebuilt.image.startswith("127.0.0.1:5050/")



async def test_removing_an_image_deletes_the_manifest_from_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no Docker socket, `docker image rm` reclaims nothing.

    The image lives in the registry, so the sweep has to delete it there: read
    the tag's digest, then delete by digest, which is the only form the
    registry API accepts.

    Over the *push* endpoint, not the pull one. The pull endpoint is loopback
    on the Docker host; inside the API container that address is the API's own
    loopback, where nothing is listening.
    """
    requests: list[tuple[str, str]] = []

    class FakeResponse:
        status_code = HTTPStatus.OK
        headers: ClassVar[dict[str, str]] = {"Docker-Content-Digest": "sha256:beef"}

        def raise_for_status(self) -> None:
            return

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            requests.append(("auth", str(kwargs.get("auth"))))

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return

        async def request(self, method: str, url: str, **_kwargs: object) -> FakeResponse:
            requests.append((method, url))
            return FakeResponse()

    monkeypatch.setattr(builder_module.httpx, "AsyncClient", FakeClient)
    docker_calls: list[list[str]] = []
    record_invocations(monkeypatch, docker_calls)
    settings = Settings(
        **BUILDER_SETTINGS,
        registry_username="harborbox",
        registry_password="s3cret",  # noqa: S106 - a fixture, not a credential
    )

    # Exactly what `SandboxTemplate.image` holds -- `derived_template_image`
    # writes it unqualified, and both the GC sweep and DELETE /v1/templates
    # hand that column straight to `remove_image`.
    await builder_module.TemplateBuilder(settings).remove_image(
        Settings().derived_template_image("custom-a1b2c3d4e5f6")
    )

    assert docker_calls == []
    assert (
        "HEAD",
        "http://registry:5000/v2/harborbox-sandbox-custom-a1b2c3d4e5f6/manifests/local",
    ) in requests
    assert (
        "DELETE",
        "http://registry:5000/v2/harborbox-sandbox-custom-a1b2c3d4e5f6/manifests/sha256:beef",
    ) in requests
    assert ("auth", "('harborbox', 's3cret')") in requests


async def test_a_registry_delete_that_fails_does_not_stall_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retained manifest is wasted storage, not a correctness problem.

    The row is dropped either way, matching what the local-daemon path already
    does when `docker image rm` fails.
    """

    class ExplodingClient:
        def __init__(self, **_kwargs: object) -> None:
            return

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return

        async def request(self, *_args: object, **_kwargs: object) -> NoReturn:
            raise httpx.ConnectError(REGISTRY_UNREACHABLE)

    monkeypatch.setattr(builder_module.httpx, "AsyncClient", ExplodingClient)
    settings = Settings(**BUILDER_SETTINGS)

    await builder_module.TemplateBuilder(settings).remove_image("127.0.0.1:5050/x:local")


def test_the_build_supplies_registry_credentials_to_buildctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The push is authenticated from the *client's* docker config, not the daemon's.

    buildkitd has no credential store of its own: the client forwards auth
    over the session. Without a config the push reaches the registry
    unauthenticated and fails on a `HEAD /v2/.../blobs/...`, long after the
    build itself succeeded.
    """
    seen: list[dict[str, Any]] = []

    def capture(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        config = Path(kwargs["env"]["DOCKER_CONFIG"]) / "config.json"
        seen.append(json.loads(config.read_text()))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(builder_module.subprocess, "run", capture)
    settings = Settings(
        **BUILDER_SETTINGS,
        registry_username="harborbox",
        registry_password="s3cret",  # noqa: S106 - a fixture, not a credential
    )

    builder_module.TemplateBuilder(settings)._build_sync(
        "FROM scratch\n", "registry:5000/x:local"
    )

    encoded = base64.b64encode(b"harborbox:s3cret").decode()
    assert seen == [{"auths": {"registry:5000": {"auth": encoded}}}]


def test_the_credentials_do_not_outlive_the_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config holds a plaintext password, so it goes when the build does."""
    paths: list[Path] = []

    def capture(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        paths.append(Path(kwargs["env"]["DOCKER_CONFIG"]) / "config.json")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(builder_module.subprocess, "run", capture)
    settings = Settings(
        **BUILDER_SETTINGS,
        registry_username="harborbox",
        registry_password="s3cret",  # noqa: S106 - a fixture, not a credential
    )

    builder_module.TemplateBuilder(settings)._build_sync(
        "FROM scratch\n", "registry:5000/x:local"
    )

    assert not paths[0].exists()


def test_a_registry_without_credentials_gets_no_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unauthenticated registry is a valid deployment, not a missing setting."""
    envs: list[dict[str, str]] = []

    def capture(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        envs.append(dict(kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(builder_module.subprocess, "run", capture)

    builder_module.TemplateBuilder(Settings(**BUILDER_SETTINGS))._build_sync(
        "FROM scratch\n", "registry:5000/x:local"
    )

    assert "DOCKER_CONFIG" not in envs[0]


def test_a_missing_docker_cli_is_reported_as_a_build_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", "docker")

    monkeypatch.setattr(builder_module.subprocess, "run", missing)
    builder = builder_module.TemplateBuilder(Settings())

    with pytest.raises(builder_module.TemplateBuildError, match="docker CLI"):
        builder._build_sync("FROM scratch\n", "image:local")


async def test_a_build_interrupted_by_a_restart_becomes_rebuildable(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    unused = derived_row(dockerfile="FROM debian:12\nRUN a\n", last_used_at=stale)
    in_use = derived_row(dockerfile="FROM debian:12\nRUN b\n", last_used_at=stale)
    recent = derived_row(dockerfile="FROM debian:12\nRUN c\n")
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


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        # What SandboxTemplate.image actually holds.
        (
            "harborbox-sandbox-custom-a1b2c3d4e5f6:local",
            ("harborbox-sandbox-custom-a1b2c3d4e5f6", "local"),
        ),
        # A pull reference, host carrying a port.
        (
            "127.0.0.1:5050/harborbox-sandbox-base:local",
            ("harborbox-sandbox-base", "local"),
        ),
        # A push reference, host is a bare service name with a port.
        (
            "registry:5000/harborbox-sandbox-relaydeck:2026.08.03",
            ("harborbox-sandbox-relaydeck", "2026.08.03"),
        ),
        # A dotted host, no port.
        ("ghcr.io/acme/sandbox:v1", ("acme/sandbox", "v1")),
        # No host: a two-segment repository, which must survive intact.
        ("acme/sandbox:v1", ("acme/sandbox", "v1")),
    ],
)
def test_a_reference_splits_into_repository_and_tag(
    reference: str, expected: tuple[str, str]
) -> None:
    """The registry API needs the repository path; the host is configuration.

    Telling a host from a first path segment is the one ambiguous case, and it
    decides whether `acme/sandbox` keeps both segments or loses one.
    """
    assert builder_module._split_reference(reference) == expected




async def test_a_raw_template_resolves_from_its_row(session: AsyncSession) -> None:
    """`custom-<hash>` has no base to fall back on, so the row is the only source.

    `base_of_derived_template` deliberately does not match the custom namespace,
    which is exactly why resolution has to recognise it separately instead of
    returning None and 404ing a template that exists.
    """
    template = derived_row()
    session.add(template)
    await session.commit()

    resolved = await resolve_template(session, Settings(), template.name)

    assert resolved.name.startswith("custom-")
    assert resolved.derived is True
    assert resolved.image == template.image
    assert (resolved.memory_mb, resolved.cpu) == (512, 1.0)


def test_a_custom_template_resolves_to_an_image_name() -> None:
    settings = Settings()
    name = "custom-a1b2c3d4e5f6"

    assert settings.is_known_template_name(name) is True
    assert settings.image_for_template(name) == f"harborbox-sandbox-{name}:local"


@pytest.mark.parametrize(
    "name",
    [
        "custom-a1b2c3d4e5",  # too short
        "custom-A1B2C3D4E5F6",  # not lowercase hex
        "custom",  # bare namespace
        "customa1b2c3d4e5f6",  # no separator
    ],
)
def test_malformed_custom_names_are_not_treated_as_templates(name: str) -> None:
    assert Settings().is_known_template_name(name) is False


@pytest.fixture
async def raw_client(
    sessions: Sessions, builder: FakeTemplateBuilder
) -> AsyncIterator[httpx.AsyncClient]:
    """Build a client whose deployment allows caller-supplied Dockerfiles."""
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
    app.state.settings = Settings()


async def test_posting_a_dockerfile_creates_a_custom_template(
    raw_client: httpx.AsyncClient, builder: FakeTemplateBuilder
) -> None:
    payload = {"dockerfile": "FROM debian:bookworm-slim\nRUN apt-get update\n"}

    created = await raw_client.post("/v1/templates", json=payload)
    again = await raw_client.post("/v1/templates", json=payload)

    assert created.status_code == HTTPStatus.CREATED
    assert again.status_code == HTTPStatus.OK
    name = created.json()["name"]
    assert name.startswith("custom-")
    assert again.json()["name"] == name
    assert created.json()["status"] == "building"
    # Idempotent: the second identical POST must not start a second build.
    assert builder.scheduled == [name]



async def test_a_sandbox_can_be_created_on_a_custom_template(
    raw_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """The point of the whole feature: run something on your own image."""
    template = derived_row()
    session.add(template)
    await session.commit()

    response = await raw_client.post("/v1/sandboxes", json={"template": template.name})

    assert response.status_code == HTTPStatus.CREATED


async def test_a_raw_build_sends_the_callers_dockerfile_plus_the_contract(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build has to use the stored Dockerfile, not re-render from packages."""
    settings = Settings(**BUILDER_SETTINGS)
    template = derived_row(status="building")
    async with sessions() as session:
        session.add(template)
        await session.commit()

    monkeypatch.setattr(builder_module, "session_factory", sessions)
    seen: list[str] = []

    def capture(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        local = [argv[index + 1] for index, item in enumerate(argv) if item == "--local"]
        directory = next(
            v.removeprefix("dockerfile=") for v in local if v.startswith("dockerfile=")
        )
        seen.append((Path(directory) / "Dockerfile").read_text())
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(builder_module.subprocess, "run", capture)

    await builder_module.TemplateBuilder(settings)._build(template.name)

    assert seen[0].startswith(RAW_DOCKERFILE)
    assert seen[0].rstrip().endswith("USER 10001:10001")
    async with sessions() as session:
        built = await session.get(SandboxTemplate, template.name)
        assert built is not None
        assert built.status == "ready", built.error


async def test_a_build_with_a_context_unpacks_it_for_buildkit(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """COPY only works if the context directory actually holds the files.

    This is the one case where the context is deliberately *not* empty, so the
    no-COPY-off-the-build-host guarantee now rests on it holding exactly what
    the caller uploaded and nothing else.
    """
    store = build_contexts.BuildContextStore(Settings(), root=tmp_path / "store")
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        info = tarfile.TarInfo("app.py")
        body = b"print('hello')"
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
    digest = store.save(payload.getvalue())

    settings = Settings(**BUILDER_SETTINGS)
    spec = validate_template_spec(
        settings, dockerfile="FROM debian:12\nCOPY app.py /app.py\n", context=digest
    )
    template = derived_row(
        name=spec.name, spec_hash=spec.spec_hash, spec=spec.as_json(), status="building"
    )
    async with sessions() as session:
        session.add(template)
        await session.commit()

    monkeypatch.setattr(builder_module, "session_factory", sessions)
    contents: list[list[str]] = []

    def capture(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        local = [argv[index + 1] for index, item in enumerate(argv) if item == "--local"]
        context = next(v.removeprefix("context=") for v in local if v.startswith("context="))
        contents.append(sorted(p.name for p in Path(context).iterdir()))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(builder_module.subprocess, "run", capture)

    await builder_module.TemplateBuilder(settings, store)._build(spec.name)

    assert contents == [["app.py"]]


async def test_a_build_whose_context_has_gone_fails_readably(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A swept context must not surface as a mystery COPY failure."""
    store = build_contexts.BuildContextStore(Settings(), root=tmp_path / "store")
    settings = Settings(**BUILDER_SETTINGS)
    spec = validate_template_spec(
        settings,
        dockerfile="FROM debian:12\nCOPY app.py /app.py\n",
        context="sha256:" + "0" * 64,
    )
    template = derived_row(
        name=spec.name, spec_hash=spec.spec_hash, spec=spec.as_json(), status="building"
    )
    async with sessions() as session:
        session.add(template)
        await session.commit()

    monkeypatch.setattr(builder_module, "session_factory", sessions)
    await builder_module.TemplateBuilder(settings, store)._build(spec.name)

    async with sessions() as session:
        failed = await session.get(SandboxTemplate, spec.name)
        assert failed is not None
        assert failed.status == "failed"
        assert "build context not found" in (failed.error or "")


async def test_concurrent_builds_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """An arbitrary RUN can burn a core for minutes, so builds have to queue.

    Before raw Dockerfiles this mattered less: an allowlisted apt install is
    bounded work. Now a caller can post ten expensive builds and, unbounded,
    every one of them would start at once.
    """
    limit = 2
    settings = Settings(**BUILDER_SETTINGS, template_max_concurrent_builds=limit)
    builder = builder_module.TemplateBuilder(settings)
    running = 0
    peak = 0
    release = asyncio.Event()

    async def fake_build(_name: str) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1

    monkeypatch.setattr(builder, "_build", fake_build)
    for index in range(6):
        builder.schedule_build(f"custom-{index:012x}")

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*list(builder._tasks), return_exceptions=True)

    assert peak <= limit
