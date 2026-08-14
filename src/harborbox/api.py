from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from datetime import UTC
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func, select, update

from harborbox import __version__
from harborbox.admission import can_admit
from harborbox.config import Settings, get_settings
from harborbox.db import create_schema, get_session, session_factory
from harborbox.execution_secrets import scrub_environment, seal_environment
from harborbox.models import Execution, Sandbox, SandboxTemplate, utc_now
from harborbox.opensandbox_compat import (
    INTERNAL_PREFIX,
    OpenSandboxCreate,
    OpenSandboxListResponse,
    OpenSandboxPagination,
    OpenSandboxRenew,
    OpenSandboxRenewResponse,
    OpenSandboxResponse,
    create_metadata,
    parse_cpu,
    parse_memory_mb,
    response_for,
    template_for,
)
from harborbox.presenters import execution_response
from harborbox.reaper import reaper_loop
from harborbox.runtime import SandboxUnavailable
from harborbox.runtime_factory import create_runtime
from harborbox.scheduler import (
    ACTIVE_EXECUTION_STATES,
    RESERVED_SANDBOX_STATES,
    Scheduler,
    has_sandbox_execution_slot,
)
from harborbox.schemas import (
    CapacityResponse,
    CodeExecutionCreate,
    CommandCreate,
    ExecutionResponse,
    FileListResponse,
    FileReadResponse,
    FileUploadResponse,
    FileWriteRequest,
    HealthResponse,
    PauseRequest,
    ProcessCreate,
    SandboxCreate,
    SandboxResponse,
    SandboxUpdate,
    TemplateCreate,
    TemplateListResponse,
    TemplateResponse,
)
from harborbox.security import require_api_key
from harborbox.template_builder import TemplateBuilder
from harborbox.templates import (
    TemplateNotReady,
    TemplateSpecError,
    UnknownTemplate,
    list_derived_templates,
    mark_template_used,
    resolve_template,
    static_template,
    validate_template_spec,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from harborbox.runtime_protocol import SandboxRuntime


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    await create_schema()
    runtime = create_runtime(settings)
    await runtime.start()
    template_builder = TemplateBuilder(settings)
    scheduler = Scheduler(settings, runtime, template_builder)
    app.state.settings = settings
    app.state.runtime = runtime
    app.state.scheduler = scheduler
    app.state.template_builder = template_builder
    await scheduler.start()

    # Reclaims sandboxes nothing will come back for. Started after the
    # scheduler so a sweep never races an empty runtime on boot.
    reaper_stop = asyncio.Event()
    reaper_task: asyncio.Task[None] | None = None
    if settings.reaper_enabled:
        reaper_task = asyncio.create_task(
            reaper_loop(session_factory, runtime, settings, reaper_stop),
            name="harborbox-reaper",
        )

    try:
        yield
    finally:
        if reaper_task is not None:
            reaper_stop.set()
            reaper_task.cancel()
            await asyncio.gather(reaper_task, return_exceptions=True)
        await scheduler.stop()
        await template_builder.close()
        await runtime.close()


app = FastAPI(
    title="Harborbox API",
    # From the package, not a literal. These drifted: `__init__` said 0.1.0
    # while this said 0.2.0, and since FastAPI's own default is *also* 0.1.0,
    # a version reported over `/openapi.json` could not distinguish "stale
    # deploy" from "nobody set a version". That ambiguity cost a deploy
    # investigation.
    version=__version__,
    description=(
        "Durable, resource-aware admission and orchestration for OpenSandbox"
    ),
    lifespan=lifespan,
)

authenticated = [Depends(require_api_key)]


def settings_from(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def runtime_from(request: Request) -> SandboxRuntime:
    return request.app.state.runtime  # type: ignore[no-any-return]


def scheduler_from(request: Request) -> Scheduler:
    return request.app.state.scheduler  # type: ignore[no-any-return]


def template_builder_from(request: Request) -> TemplateBuilder:
    return request.app.state.template_builder  # type: ignore[no-any-return]


async def get_sandbox_or_404(session: AsyncSession, sandbox_id: str) -> Sandbox:
    sandbox = await session.get(Sandbox, sandbox_id)
    if sandbox is None:
        raise HTTPException(status_code=404, detail="sandbox not found")
    return sandbox


async def get_execution_or_404(
    session: AsyncSession, execution_id: str
) -> Execution:
    execution = await session.get(Execution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="execution not found")
    return execution


def opensandbox_error(detail: str, status_code: int = 422) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": "INVALID_REQUEST", "message": detail},
    )


def require_running(sandbox: Sandbox) -> None:
    if sandbox.status != "running":
        raise HTTPException(
            status_code=409,
            detail=f"sandbox must be running; current status is {sandbox.status}",
        )


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/docs")


def static_template_response(settings: Settings, name: str) -> TemplateResponse:
    resolved = static_template(settings, name)
    return TemplateResponse(
        name=resolved.name,
        base=resolved.base,
        image=resolved.image,
        spec_hash=None,
        status="ready",
        version=settings.template_version,
        memory_mb=resolved.memory_mb,
        cpu=resolved.cpu,
        warm_pool=settings.warm_pool_sizes[name],
    )


def derived_template_response(
    settings: Settings, template: SandboxTemplate
) -> TemplateResponse:
    return TemplateResponse(
        name=template.name,
        base=template.base,
        image=template.image,
        spec_hash=template.spec_hash,
        status=template.status,  # type: ignore[arg-type]
        version=settings.template_version,
        memory_mb=template.memory_mb,
        cpu=template.cpu,
        # Derived templates cold-start by design: a per-team pool would trade the
        # bounded image count this design buys for an unbounded pool count.
        warm_pool=0,
        error=template.error,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


@app.get(
    "/v1/templates",
    response_model=TemplateListResponse,
    dependencies=authenticated,
)
async def list_templates(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TemplateListResponse:
    settings = settings_from(request)
    templates = [
        static_template_response(settings, name) for name in settings.template_images
    ]
    templates.extend(
        derived_template_response(settings, template)
        for template in await list_derived_templates(session)
    )
    return TemplateListResponse(templates=templates)


@app.post(
    "/v1/templates",
    response_model=TemplateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=authenticated,
)
async def create_template(
    body: TemplateCreate,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TemplateResponse:
    settings = settings_from(request)
    try:
        spec = validate_template_spec(
            settings,
            base=body.base,
            apt=body.apt,
            npm=body.npm,
            env=body.env,
        )
    except TemplateSpecError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # A team with no sandbox-backed tools keeps the base template, and with it
    # the base template's warm pool. Nothing is built for it.
    if spec.is_empty:
        response.status_code = status.HTTP_200_OK
        return static_template_response(settings, spec.base)

    default_memory_mb, default_cpu = settings.resources_for_template(spec.base)
    memory_mb = body.memory_mb or default_memory_mb
    cpu = body.cpu or default_cpu
    if memory_mb > settings.max_sandbox_memory_mb:
        raise HTTPException(
            status_code=422,
            detail=f"memory_mb cannot exceed {settings.max_sandbox_memory_mb}",
        )
    if cpu > settings.max_sandbox_cpu:
        raise HTTPException(
            status_code=422,
            detail=f"cpu cannot exceed {settings.max_sandbox_cpu}",
        )

    template = await session.get(SandboxTemplate, spec.name)
    if template is not None and template.status != "failed":
        # `building` counts as known too: the caller polls rather than starting a
        # second build of the identical spec.
        #
        # Sizing here is last-writer-wins between teams that share a package set,
        # because `spec_hash` excludes resource overrides by design. That is
        # acceptable only because it is a default hint: `POST /v1/sandboxes`
        # takes per-sandbox sizing from the request.
        template.memory_mb = memory_mb
        template.cpu = cpu
        template.last_used_at = utc_now()
        await session.commit()
        await session.refresh(template)
        response.status_code = status.HTTP_200_OK
        return derived_template_response(settings, template)

    if template is None:
        template = SandboxTemplate(
            name=spec.name,
            base=spec.base,
            spec_hash=spec.spec_hash,
            spec=spec.as_json(),
            image=settings.derived_template_image(spec.name),
            status="building",
            memory_mb=memory_mb,
            cpu=cpu,
        )
        session.add(template)
    else:
        template.spec = spec.as_json()
        template.image = settings.derived_template_image(spec.name)
        template.status = "building"
        template.error = None
        template.memory_mb = memory_mb
        template.cpu = cpu
        template.last_used_at = utc_now()
    await session.commit()
    await session.refresh(template)

    template_builder_from(request).schedule_build(template.name)
    return derived_template_response(settings, template)


@app.get(
    "/v1/templates/{name}",
    response_model=TemplateResponse,
    dependencies=authenticated,
)
async def get_template(
    name: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TemplateResponse:
    settings = settings_from(request)
    if name in settings.template_images:
        return static_template_response(settings, name)
    template = await session.get(SandboxTemplate, name)
    if template is None:
        raise HTTPException(status_code=404, detail="template not found")
    return derived_template_response(settings, template)


@app.delete(
    "/v1/templates/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=authenticated,
)
async def delete_template(
    name: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    settings = settings_from(request)
    if name in settings.template_images:
        raise HTTPException(
            status_code=409,
            detail=f"{name} is a statically configured template and cannot be deleted",
        )
    template = await session.get(SandboxTemplate, name)
    if template is None:
        raise HTTPException(status_code=404, detail="template not found")
    image = template.image
    await session.delete(template)
    await session.commit()
    await template_builder_from(request).remove_image(image)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/v1/sandboxes",
    response_model=SandboxResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=authenticated,
)
async def create_sandbox(
    body: SandboxCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Sandbox:
    settings = settings_from(request)
    try:
        resolved = await resolve_template(session, settings, body.template)
    except UnknownTemplate as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TemplateNotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # An explicit request value always wins. The template row's sizing is a
    # default hint only: `spec_hash` deliberately excludes resource overrides, so
    # teams sharing a package set share one row and its stored `memory_mb`/`cpu`
    # are last-writer-wins. Per-team sizing has to come from the request.
    memory_mb = body.memory_mb if body.memory_mb is not None else resolved.memory_mb
    cpu = body.cpu if body.cpu is not None else resolved.cpu
    if memory_mb > settings.max_sandbox_memory_mb:
        raise HTTPException(
            status_code=422,
            detail=f"memory_mb cannot exceed {settings.max_sandbox_memory_mb}",
        )
    if cpu > settings.max_sandbox_cpu:
        raise HTTPException(
            status_code=422,
            detail=f"cpu cannot exceed {settings.max_sandbox_cpu}",
        )

    # No pooled-row adoption here on purpose.
    #
    # `main` warmed spare sandboxes as `Sandbox.status == "pooled"` rows and
    # adopted one here when `(memory_mb, cpu, egress)` matched. Templates make
    # that match unsound: those rows carry no template, so the shape they agree
    # on says nothing about which image is inside, and a pooled `onvo-pro`
    # container would be handed to a `relaydeck` caller.
    #
    # 0.2.0 replaces it one layer down with `OpenSandboxWarmPools`, which keys
    # pools by template (`warm_pool.py`) and is reached through
    # `runtime.acquire`. Same latency win, and correct per template rather than
    # only for the default shape.
    metadata = dict(body.metadata)
    metadata["template"] = resolved.name
    metadata["template_version"] = settings.template_version
    if resolved.derived:
        metadata["template_base"] = resolved.base
        metadata["template_spec_hash"] = resolved.spec_hash or ""
    await mark_template_used(session, resolved)

    sandbox = Sandbox(
        id=new_id("sbx"),
        status="created",
        agent_token=secrets.token_urlsafe(32),
        memory_mb=memory_mb,
        cpu=cpu,
        pids_limit=settings.sandbox_pids_limit,
        idle_timeout_seconds=(
            body.idle_timeout_seconds
            if body.idle_timeout_seconds is not None
            else settings.default_idle_timeout_seconds
        ),
        metadata_=metadata,
    )
    session.add(sandbox)
    await session.commit()
    await session.refresh(sandbox)
    return sandbox


@app.get(
    "/v1/sandboxes",
    response_model=list[SandboxResponse],
    dependencies=authenticated,
)
async def list_sandboxes(
    session: AsyncSession = Depends(get_session),
) -> list[Sandbox]:
    result = await session.scalars(
        select(Sandbox)
        .where(Sandbox.status != "killed")
        .order_by(Sandbox.created_at.desc())
    )
    return list(result)


@app.get(
    "/v1/sandboxes/{sandbox_id}",
    response_model=SandboxResponse,
    dependencies=authenticated,
)
async def get_sandbox(
    sandbox_id: str,
    session: AsyncSession = Depends(get_session),
) -> Sandbox:
    return await get_sandbox_or_404(session, sandbox_id)


@app.patch(
    "/v1/sandboxes/{sandbox_id}",
    response_model=SandboxResponse,
    dependencies=authenticated,
)
async def update_sandbox(
    sandbox_id: str,
    body: SandboxUpdate,
    session: AsyncSession = Depends(get_session),
) -> Sandbox:
    sandbox = await get_sandbox_or_404(session, sandbox_id)
    if sandbox.status in {"killed", "failed"}:
        raise HTTPException(status_code=409, detail=f"cannot update {sandbox.status}")
    if body.idle_timeout_seconds is not None:
        sandbox.idle_timeout_seconds = body.idle_timeout_seconds
    sandbox.last_activity_at = utc_now()
    await session.commit()
    await session.refresh(sandbox)
    return sandbox


@app.post(
    "/v1/sandboxes/{sandbox_id}/pause",
    response_model=SandboxResponse,
    dependencies=authenticated,
)
async def pause_sandbox(
    sandbox_id: str,
    body: PauseRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Sandbox:
    sandbox = await get_sandbox_or_404(session, sandbox_id)
    active = await session.scalar(
        select(func.count()).select_from(Execution).where(
            Execution.sandbox_id == sandbox_id,
            Execution.status.in_(ACTIVE_EXECUTION_STATES),
        )
    )
    if active:
        raise HTTPException(status_code=409, detail="sandbox has an active execution")
    if sandbox.status == "created":
        sandbox.status = "paused_cold"
    elif sandbox.status == "running":
        await runtime_from(request).pause(sandbox, memory=body.memory)
        sandbox.status = "paused_memory" if body.memory else "paused_cold"
        if not body.memory:
            sandbox.container_id = None
            sandbox.container_name = None
    elif sandbox.status not in {"paused_memory", "paused_cold"}:
        raise HTTPException(status_code=409, detail=f"cannot pause {sandbox.status}")
    await session.commit()
    await session.refresh(sandbox)
    return sandbox


@app.post(
    "/v1/sandboxes/{sandbox_id}/resume",
    response_model=SandboxResponse,
    dependencies=authenticated,
)
async def resume_sandbox(
    sandbox_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Sandbox:
    sandbox = await get_sandbox_or_404(session, sandbox_id)
    if sandbox.status == "running":
        return sandbox
    if sandbox.status not in {"created", "paused_memory", "paused_cold"}:
        raise HTTPException(status_code=409, detail=f"cannot resume {sandbox.status}")

    scheduler = scheduler_from(request)
    capacity = await scheduler.capacity()
    already_reserved = sandbox.status == "paused_memory"
    decision = can_admit(
        capacity,
        incremental_memory_mb=0 if already_reserved else sandbox.memory_mb,
        incremental_cpu=sandbox.cpu,
        emergency_available_memory_mb=(
            settings_from(request).emergency_available_memory_mb
        ),
    )
    if not decision.admitted:
        raise HTTPException(
            status_code=429,
            detail=f"insufficient {decision.waiting_for} capacity",
            headers={"Retry-After": "1"},
        )

    sandbox.status = "starting"
    await session.commit()
    try:
        started = await runtime_from(request).resume(sandbox)
        sandbox.container_id = started.id
        sandbox.container_name = started.name
        sandbox.status = "running"
        sandbox.last_activity_at = utc_now()
        await session.commit()
        await session.refresh(sandbox)
        await runtime_from(request).wait_until_ready(sandbox)
    except SandboxUnavailable as exc:
        sandbox.status = "failed"
        await session.commit()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    else:
        return sandbox


@app.delete(
    "/v1/sandboxes/{sandbox_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=authenticated,
)
async def kill_sandbox(
    sandbox_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    sandbox = await get_sandbox_or_404(session, sandbox_id)
    # `cancel_requested` as well as the status, because the status alone is not
    # durable against a concurrent scheduler pass: that pass may already hold a
    # snapshot where this execution is `queued`, and committing it would write
    # `admitted` straight over `cancelled` — resurrecting a sandbox the caller has
    # already been told is gone, container and memory reservation included.
    # `cancel_requested` is the flag the scheduler re-checks, so a lost update on
    # `status` still ends with the execution cancelled.
    await session.execute(
        update(Execution)
        .where(
            Execution.sandbox_id == sandbox_id,
            Execution.status == "queued",
        )
        .values(status="cancelled", cancel_requested=True, finished_at=utc_now())
    )
    await runtime_from(request).kill(sandbox)
    sandbox.status = "killed"
    sandbox.container_id = None
    sandbox.container_name = None
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/opensandbox/v1/sandboxes",
    response_model=OpenSandboxResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=authenticated,
    tags=["OpenSandbox compatibility"],
)
async def opensandbox_create(
    body: OpenSandboxCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> OpenSandboxResponse:
    settings = settings_from(request)
    try:
        template = template_for(body, settings)
        memory_mb = (
            parse_memory_mb(body.resource_limits["memory"])
            if "memory" in body.resource_limits
            else settings.default_sandbox_memory_mb
        )
        cpu = (
            parse_cpu(body.resource_limits["cpu"])
            if "cpu" in body.resource_limits
            else settings.default_sandbox_cpu
        )
        metadata = create_metadata(body, template=template)
    except (KeyError, ValueError) as exc:
        raise opensandbox_error(str(exc)) from exc

    sandbox = await create_sandbox(
        SandboxCreate(
            template=template,
            memory_mb=memory_mb,
            cpu=cpu,
            idle_timeout_seconds=settings.default_idle_timeout_seconds,
            metadata=metadata,
        ),
        request,
        session,
    )
    try:
        sandbox = await resume_sandbox(sandbox.id, request, session)
    except HTTPException:
        await session.delete(sandbox)
        await session.commit()
        raise
    return response_for(sandbox, settings)


@app.get(
    "/opensandbox/v1/sandboxes",
    response_model=OpenSandboxListResponse,
    response_model_by_alias=True,
    dependencies=authenticated,
    tags=["OpenSandbox compatibility"],
)
async def opensandbox_list(
    request: Request,
    state: list[str] | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> OpenSandboxListResponse:
    sandboxes = list(
        await session.scalars(select(Sandbox).order_by(Sandbox.created_at.desc()))
    )
    responses = [response_for(item, settings_from(request)) for item in sandboxes]
    if state:
        requested = {item.lower() for item in state}
        responses = [item for item in responses if item.status.state.lower() in requested]
    total = len(responses)
    start = (page - 1) * page_size
    items = responses[start : start + page_size]
    total_pages = (total + page_size - 1) // page_size
    return OpenSandboxListResponse(
        items=items,
        pagination=OpenSandboxPagination(
            page=page,
            pageSize=page_size,
            totalItems=total,
            totalPages=total_pages,
            hasNextPage=page < total_pages,
        ),
    )


@app.get(
    "/opensandbox/v1/sandboxes/{sandbox_id}",
    response_model=OpenSandboxResponse,
    response_model_by_alias=True,
    dependencies=authenticated,
    tags=["OpenSandbox compatibility"],
)
async def opensandbox_get(
    sandbox_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> OpenSandboxResponse:
    return response_for(
        await get_sandbox_or_404(session, sandbox_id), settings_from(request)
    )


@app.delete(
    "/opensandbox/v1/sandboxes/{sandbox_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=authenticated,
    tags=["OpenSandbox compatibility"],
)
async def opensandbox_delete(
    sandbox_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    return await kill_sandbox(sandbox_id, request, session)


@app.post(
    "/opensandbox/v1/sandboxes/{sandbox_id}/pause",
    response_model=OpenSandboxResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=authenticated,
    tags=["OpenSandbox compatibility"],
)
async def opensandbox_pause(
    sandbox_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> OpenSandboxResponse:
    sandbox = await pause_sandbox(
        sandbox_id, PauseRequest(memory=True), request, session
    )
    return response_for(sandbox, settings_from(request))


@app.post(
    "/opensandbox/v1/sandboxes/{sandbox_id}/resume",
    response_model=OpenSandboxResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=authenticated,
    tags=["OpenSandbox compatibility"],
)
async def opensandbox_resume(
    sandbox_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> OpenSandboxResponse:
    sandbox = await resume_sandbox(sandbox_id, request, session)
    return response_for(sandbox, settings_from(request))


@app.post(
    "/opensandbox/v1/sandboxes/{sandbox_id}/renew-expiration",
    response_model=OpenSandboxRenewResponse,
    response_model_by_alias=True,
    dependencies=authenticated,
    tags=["OpenSandbox compatibility"],
)
async def opensandbox_renew(
    sandbox_id: str,
    body: OpenSandboxRenew,
    session: AsyncSession = Depends(get_session),
) -> OpenSandboxRenewResponse:
    sandbox = await get_sandbox_or_404(session, sandbox_id)
    expires_at = body.expires_at.astimezone(UTC)
    if expires_at <= utc_now():
        message = "expiresAt must be in the future"
        raise opensandbox_error(message)
    metadata = dict(sandbox.metadata_)
    metadata[f"{INTERNAL_PREFIX}expires_at"] = expires_at.isoformat()
    sandbox.metadata_ = metadata
    await session.commit()
    return OpenSandboxRenewResponse(expiresAt=expires_at)


@app.patch(
    "/opensandbox/v1/sandboxes/{sandbox_id}/metadata",
    response_model=OpenSandboxResponse,
    response_model_by_alias=True,
    dependencies=authenticated,
    tags=["OpenSandbox compatibility"],
)
async def opensandbox_patch_metadata(
    sandbox_id: str,
    body: dict[str, str | None],
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> OpenSandboxResponse:
    sandbox = await get_sandbox_or_404(session, sandbox_id)
    metadata = dict(sandbox.metadata_)
    for key, value in body.items():
        if key.startswith(INTERNAL_PREFIX):
            message = f"metadata key {key} is reserved"
            raise opensandbox_error(message)
        if value is None:
            metadata.pop(key, None)
        else:
            metadata[key] = value
    sandbox.metadata_ = metadata
    await session.commit()
    await session.refresh(sandbox)
    return response_for(sandbox, settings_from(request))


async def enqueue(
    *,
    sandbox_id: str,
    kind: str,
    code: str | None,
    command: str | None,
    environment: dict[str, str],
    cwd: str | None,
    timeout_seconds: int | None,
    settings: Settings,
    session: AsyncSession,
) -> ExecutionResponse:
    sandbox = await get_sandbox_or_404(session, sandbox_id)
    if sandbox.status in {"killed", "failed"}:
        raise HTTPException(status_code=409, detail=f"sandbox is {sandbox.status}")
    queue_depth = await session.scalar(
        select(func.count()).select_from(Execution).where(Execution.status == "queued")
    )
    if int(queue_depth or 0) >= settings.max_queue_depth:
        raise HTTPException(
            status_code=429,
            detail="execution queue is full",
            headers={"Retry-After": "1"},
        )
    timeout = timeout_seconds or settings.default_execution_timeout_seconds
    if timeout > settings.max_execution_timeout_seconds:
        raise HTTPException(
            status_code=422,
            detail=(
                "timeout_seconds cannot exceed "
                f"{settings.max_execution_timeout_seconds}"
            ),
        )
    payload = code if code is not None else command or ""
    if len(payload.encode("utf-8")) > settings.max_code_bytes:
        raise HTTPException(status_code=413, detail="execution payload is too large")

    execution = Execution(
        id=new_id("exec"),
        sandbox_id=sandbox.id,
        kind=kind,
        status="queued",
        code=code,
        command=command,
        environment=environment,
        cwd=cwd,
        timeout_seconds=timeout,
    )
    session.add(execution)
    await session.commit()
    await session.refresh(execution)
    return await execution_response(session, execution, waiting_for="worker")


@app.post(
    "/v1/sandboxes/{sandbox_id}/executions",
    response_model=ExecutionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=authenticated,
)
async def create_execution(
    sandbox_id: str,
    body: CodeExecutionCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ExecutionResponse:
    return await enqueue(
        sandbox_id=sandbox_id,
        kind="code",
        code=body.code,
        command=None,
        environment=body.env,
        cwd=None,
        timeout_seconds=body.timeout_seconds,
        settings=settings_from(request),
        session=session,
    )


@app.post(
    "/v1/sandboxes/{sandbox_id}/commands",
    response_model=ExecutionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=authenticated,
)
async def create_command(
    sandbox_id: str,
    body: CommandCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ExecutionResponse:
    return await enqueue(
        sandbox_id=sandbox_id,
        kind="command",
        code=None,
        command=body.command,
        environment=body.env,
        cwd=body.cwd,
        timeout_seconds=body.timeout_seconds,
        settings=settings_from(request),
        session=session,
    )


@app.post(
    "/v1/sandboxes/{sandbox_id}/processes",
    response_model=ExecutionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=authenticated,
)
async def create_process(
    sandbox_id: str,
    body: ProcessCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ExecutionResponse:
    settings = settings_from(request)
    process_spec = json.dumps(
        {
            "executable": body.executable,
            "args": body.args,
            "stdin": body.stdin,
        },
        separators=(",", ":"),
    )
    return await enqueue(
        sandbox_id=sandbox_id,
        kind="process",
        code=None,
        command=process_spec,
        environment=seal_environment(settings, body.env, body.secret_env),
        cwd=body.cwd,
        timeout_seconds=body.timeout_seconds,
        settings=settings,
        session=session,
    )


@app.get(
    "/v1/executions/{execution_id}",
    response_model=ExecutionResponse,
    dependencies=authenticated,
)
async def get_execution(
    execution_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ExecutionResponse:
    execution = await get_execution_or_404(session, execution_id)
    waiting_for = None
    if execution.status == "queued":
        sandbox = await get_sandbox_or_404(session, execution.sandbox_id)
        active_rows = (
            await session.execute(
                select(Execution.kind, func.count())
                .where(
                    Execution.sandbox_id == sandbox.id,
                    Execution.status.in_(ACTIVE_EXECUTION_STATES),
                )
                .group_by(Execution.kind)
            )
        ).all()
        active_count = sum(int(count) for _, count in active_rows)
        active_code = any(kind == "code" for kind, _ in active_rows)
        blocked_by_sandbox = not has_sandbox_execution_slot(
            kind=execution.kind,
            active_count=active_count,
            active_code=active_code,
            limit=settings_from(request).max_concurrent_executions_per_sandbox,
        )
        if blocked_by_sandbox:
            waiting_for = "sandbox"
        else:
            capacity = await scheduler_from(request).capacity()
            already_reserved = sandbox.status in RESERVED_SANDBOX_STATES
            decision = can_admit(
                capacity,
                incremental_memory_mb=0 if already_reserved else sandbox.memory_mb,
                incremental_cpu=(
                    0.0
                    if sandbox.status in {"running", "starting"}
                    else sandbox.cpu
                ),
                emergency_available_memory_mb=(
                    settings_from(request).emergency_available_memory_mb
                ),
            )
            waiting_for = decision.waiting_for or "worker"
    return await execution_response(session, execution, waiting_for=waiting_for)


@app.post(
    "/v1/executions/{execution_id}/cancel",
    response_model=ExecutionResponse,
    dependencies=authenticated,
)
async def cancel_execution(
    execution_id: str,
    session: AsyncSession = Depends(get_session),
) -> ExecutionResponse:
    execution = await get_execution_or_404(session, execution_id)
    if execution.status == "queued":
        execution.status = "cancelled"
        execution.cancel_requested = True
        execution.environment = scrub_environment(execution.environment)
        execution.finished_at = utc_now()
        await session.commit()
        await session.refresh(execution)
    elif execution.status not in {"succeeded", "failed", "cancelled"}:
        raise HTTPException(
            status_code=409,
            detail="running execution interruption is not implemented",
        )
    return await execution_response(session, execution)


@app.get(
    "/v1/executions/{execution_id}/events",
    dependencies=authenticated,
)
async def execution_events(execution_id: str) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        previous: str | None = None
        while True:
            async with session_factory() as session:
                execution = await session.get(Execution, execution_id)
                if execution is None:
                    yield 'event: error\ndata: {"detail":"execution not found"}\n\n'
                    return
                response = await execution_response(session, execution)
                payload = response.model_dump_json()
                if payload != previous:
                    yield f"event: execution\ndata: {payload}\n\n"
                    previous = payload
                else:
                    yield ": keepalive\n\n"
                if execution.status in {"succeeded", "failed", "cancelled"}:
                    return
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get(
    "/v1/sandboxes/{sandbox_id}/files",
    response_model=FileReadResponse,
    dependencies=authenticated,
)
async def read_file(
    sandbox_id: str,
    request: Request,
    path: str = Query(...),
    session: AsyncSession = Depends(get_session),
) -> FileReadResponse:
    sandbox = await get_sandbox_or_404(session, sandbox_id)
    require_running(sandbox)
    try:
        return await runtime_from(request).read_file(sandbox, path)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(exc.response.status_code, detail=exc.response.text) from exc


@app.put(
    "/v1/sandboxes/{sandbox_id}/files",
    response_model=FileReadResponse,
    dependencies=authenticated,
)
async def write_file(
    sandbox_id: str,
    body: FileWriteRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> FileReadResponse:
    sandbox = await get_sandbox_or_404(session, sandbox_id)
    require_running(sandbox)
    try:
        return await runtime_from(request).write_file(sandbox, body)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(exc.response.status_code, detail=exc.response.text) from exc


@app.put(
    "/v1/sandboxes/{sandbox_id}/files/content",
    response_model=FileUploadResponse,
    dependencies=authenticated,
)
async def upload_file_content(
    sandbox_id: str,
    request: Request,
    path: str = Query(...),
    session: AsyncSession = Depends(get_session),
) -> FileUploadResponse:
    sandbox = await get_sandbox_or_404(session, sandbox_id)
    require_running(sandbox)
    content_length = request.headers.get("content-length")
    max_bytes = settings_from(request).max_upload_bytes
    if content_length and int(content_length) > max_bytes:
        raise HTTPException(status_code=413, detail="file exceeds upload limit")
    try:
        return await runtime_from(request).write_file_stream(
            sandbox,
            path,
            request.stream(),
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(exc.response.status_code, detail=exc.response.text) from exc


@app.get(
    "/v1/sandboxes/{sandbox_id}/files/list",
    response_model=FileListResponse,
    dependencies=authenticated,
)
async def list_files(
    sandbox_id: str,
    request: Request,
    path: str = Query(default="."),
    session: AsyncSession = Depends(get_session),
) -> FileListResponse:
    sandbox = await get_sandbox_or_404(session, sandbox_id)
    require_running(sandbox)
    try:
        return await runtime_from(request).list_files(sandbox, path)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(exc.response.status_code, detail=exc.response.text) from exc


@app.delete(
    "/v1/sandboxes/{sandbox_id}/files",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=authenticated,
)
async def remove_file(
    sandbox_id: str,
    request: Request,
    path: str = Query(...),
    session: AsyncSession = Depends(get_session),
) -> Response:
    sandbox = await get_sandbox_or_404(session, sandbox_id)
    require_running(sandbox)
    try:
        await runtime_from(request).remove_file(sandbox, path)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(exc.response.status_code, detail=exc.response.text) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/v1/capacity",
    response_model=CapacityResponse,
    dependencies=authenticated,
)
async def get_capacity(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CapacityResponse:
    capacity = await scheduler_from(request).capacity()
    running_sandboxes = await session.scalar(
        select(func.count()).select_from(Sandbox).where(
            Sandbox.status.in_(RESERVED_SANDBOX_STATES)
        )
    )
    running_executions = await session.scalar(
        select(func.count()).select_from(Execution).where(
            Execution.status.in_(ACTIVE_EXECUTION_STATES)
        )
    )
    queued_executions = await session.scalar(
        select(func.count()).select_from(Execution).where(Execution.status == "queued")
    )
    return CapacityResponse(
        total_memory_mb=capacity.total_memory_mb,
        reserve_memory_mb=capacity.reserve_memory_mb,
        configured_sandbox_budget_mb=capacity.configured_sandbox_budget_mb,
        sandbox_budget_mb=capacity.sandbox_budget_mb,
        reserved_memory_mb=capacity.reserved_memory_mb,
        warm_pool_reserved_memory_mb=capacity.warm_pool_reserved_memory_mb,
        warm_pool_reserved_cpu=capacity.warm_pool_reserved_cpu,
        warm_pool_target_sandboxes=capacity.warm_pool_target_sandboxes,
        available_reservation_mb=capacity.available_reservation_mb,
        host_available_memory_mb=capacity.host_available_memory_mb,
        running_sandboxes=int(running_sandboxes or 0),
        running_executions=int(running_executions or 0),
        queued_executions=int(queued_executions or 0),
    )
