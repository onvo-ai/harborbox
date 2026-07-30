from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from harborbox.admission import can_admit
from harborbox.config import Settings, get_settings
from harborbox.db import create_schema, get_session, session_factory
from harborbox.models import Execution, Sandbox, utc_now
from harborbox.presenters import execution_response
from harborbox.runtime import DockerRuntime, SandboxUnavailable
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
    SandboxCreate,
    SandboxResponse,
    SandboxUpdate,
)
from harborbox.security import require_api_key


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    await create_schema()
    runtime = DockerRuntime(settings)
    scheduler = Scheduler(settings, runtime)
    app.state.settings = settings
    app.state.runtime = runtime
    app.state.scheduler = scheduler
    await scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
        await runtime.close()


app = FastAPI(
    title="Harborbox API",
    version="0.1.0",
    description="Resource-aware, self-hosted Python sandboxes",
    lifespan=lifespan,
)

authenticated = [Depends(require_api_key)]


def settings_from(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def runtime_from(request: Request) -> DockerRuntime:
    return request.app.state.runtime  # type: ignore[no-any-return]


def scheduler_from(request: Request) -> Scheduler:
    return request.app.state.scheduler  # type: ignore[no-any-return]


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
    memory_mb = body.memory_mb or settings.default_sandbox_memory_mb
    cpu = body.cpu or settings.default_sandbox_cpu
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
        metadata_=body.metadata,
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
        return sandbox
    except SandboxUnavailable as exc:
        sandbox.status = "failed"
        await session.commit()
        raise HTTPException(status_code=503, detail=str(exc)) from exc


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
        sandbox_budget_mb=capacity.sandbox_budget_mb,
        reserved_memory_mb=capacity.reserved_memory_mb,
        available_reservation_mb=capacity.available_reservation_mb,
        host_available_memory_mb=capacity.host_available_memory_mb,
        running_sandboxes=int(running_sandboxes or 0),
        running_executions=int(running_executions or 0),
        queued_executions=int(queued_executions or 0),
    )
