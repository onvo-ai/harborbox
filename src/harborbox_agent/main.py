from __future__ import annotations

import asyncio
import hmac
import os
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from harborbox_agent.files import (
    FileTooLargeError,
    UnsafePathError,
    list_files,
    read_file,
    remove_file,
    write_file,
    write_file_stream,
)
from harborbox_agent.kernel import KernelSession
from harborbox_agent.output import OutputBudget

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

WORKSPACE = Path(os.environ.get("HARBORBOX_WORKSPACE", "/workspace"))
AGENT_TOKEN = os.environ.get("HARBORBOX_AGENT_TOKEN", "")
MAX_UPLOAD_BYTES = int(os.environ.get("HARBORBOX_MAX_UPLOAD_BYTES", "157286400"))


class ExecuteRequest(BaseModel):
    code: str
    timeout_seconds: int = Field(ge=1)
    max_output_bytes: int = Field(ge=1024)
    env: dict[str, str] = Field(default_factory=dict)


class CommandRequest(BaseModel):
    command: str
    timeout_seconds: int = Field(ge=1)
    max_output_bytes: int = Field(ge=1024)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


class ProcessRequest(BaseModel):
    executable: str = Field(min_length=1, max_length=1024)
    args: list[str] = Field(default_factory=list, max_length=256)
    stdin: str | None = None
    timeout_seconds: int = Field(ge=1)
    max_output_bytes: int = Field(ge=1024)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


class FileWriteRequest(BaseModel):
    path: str
    content: str
    encoding: str = "utf-8"


async def authenticate(x_sandbox_token: str | None = Header(default=None)) -> None:
    if (
        not AGENT_TOKEN
        or x_sandbox_token is None
        or not hmac.compare_digest(x_sandbox_token, AGENT_TOKEN)
    ):
        raise HTTPException(status_code=401, detail="invalid sandbox token")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    # Constructed, not started. See KernelSession.ensure_started — starting the
    # Jupyter kernel here delayed the agent answering anything by ~3.5s, and most
    # sandboxes never use the kernel.
    kernel = KernelSession(str(WORKSPACE))
    app.state.kernel = kernel
    try:
        yield
    finally:
        # Safe on a kernel that was never started: stop() guards on both the
        # client and `manager.has_kernel`, and most sandboxes never start one.
        await kernel.stop()


app = FastAPI(title="Harborbox sandbox agent", lifespan=lifespan)
authenticated = [Depends(authenticate)]


@app.get("/health", dependencies=authenticated)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/execute", dependencies=authenticated)
async def execute(body: ExecuteRequest, request: Request) -> dict[str, Any]:
    kernel: KernelSession = request.app.state.kernel
    await kernel.ensure_started()
    result = await kernel.execute(
        body.code,
        env=body.env,
        timeout_seconds=body.timeout_seconds,
        max_output_bytes=body.max_output_bytes,
    )
    return {
        "logs": {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "truncated": result.truncated,
        },
        "results": result.results,
        "error": result.error,
        "exit_code": None,
    }


async def drain_stream(
    stream: asyncio.StreamReader,
    budget: OutputBudget,
    chunks: list[str],
) -> None:
    while True:
        data = await stream.read(65_536)
        if not data:
            return
        bounded = budget.take(data.decode("utf-8", errors="replace"))
        if bounded:
            chunks.append(bounded)


@app.post("/v1/commands", dependencies=authenticated)
async def command(body: CommandRequest) -> dict[str, Any]:
    cwd = str(WORKSPACE)
    if body.cwd:
        from harborbox_agent.files import safe_path

        cwd = str(safe_path(WORKSPACE, body.cwd))
    environment = {**os.environ, **body.env}
    process = await asyncio.create_subprocess_shell(
        body.command,
        cwd=cwd,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    budget = OutputBudget(body.max_output_bytes)
    stdout: list[str] = []
    stderr: list[str] = []
    readers = [
        asyncio.create_task(drain_stream(process.stdout, budget, stdout)),
        asyncio.create_task(drain_stream(process.stderr, budget, stderr)),
    ]
    error: dict[str, Any] | None = None
    try:
        async with asyncio.timeout(body.timeout_seconds):
            exit_code = await process.wait()
    except TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        exit_code = await process.wait()
        error = {
            "name": "TimeoutError",
            "value": f"command exceeded {body.timeout_seconds} seconds",
            "traceback": [],
        }
    await asyncio.gather(*readers)
    return {
        "logs": {
            "stdout": stdout,
            "stderr": stderr,
            "truncated": budget.truncated,
        },
        "results": [],
        "error": error,
        "exit_code": exit_code,
    }


@app.post("/v1/processes", dependencies=authenticated)
async def process(body: ProcessRequest) -> dict[str, Any]:
    cwd = str(WORKSPACE)
    if body.cwd:
        from harborbox_agent.files import safe_path

        cwd = str(safe_path(WORKSPACE, body.cwd))
    environment = {**os.environ, **body.env}
    child = await asyncio.create_subprocess_exec(
        body.executable,
        *body.args,
        cwd=cwd,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert child.stdin is not None
    assert child.stdout is not None
    assert child.stderr is not None
    budget = OutputBudget(body.max_output_bytes)
    stdout: list[str] = []
    stderr: list[str] = []
    readers = [
        asyncio.create_task(drain_stream(child.stdout, budget, stdout)),
        asyncio.create_task(drain_stream(child.stderr, budget, stderr)),
    ]
    if body.stdin is not None:
        child.stdin.write(body.stdin.encode("utf-8"))
        await child.stdin.drain()
    child.stdin.close()
    await child.stdin.wait_closed()
    error: dict[str, Any] | None = None
    try:
        async with asyncio.timeout(body.timeout_seconds):
            exit_code = await child.wait()
    except TimeoutError:
        os.killpg(child.pid, signal.SIGKILL)
        exit_code = await child.wait()
        error = {
            "name": "TimeoutError",
            "value": f"process exceeded {body.timeout_seconds} seconds",
            "traceback": [],
        }
    await asyncio.gather(*readers)
    return {
        "logs": {
            "stdout": stdout,
            "stderr": stderr,
            "truncated": budget.truncated,
        },
        "results": [],
        "error": error,
        "exit_code": exit_code,
    }


@app.get("/v1/files", dependencies=authenticated)
async def get_file(path: str = Query(...)) -> dict[str, str]:
    try:
        return read_file(WORKSPACE, path)
    except UnsafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="file not found") from exc


@app.put("/v1/files", dependencies=authenticated)
async def put_file(body: FileWriteRequest) -> dict[str, str]:
    if body.encoding not in {"utf-8", "base64"}:
        raise HTTPException(status_code=422, detail="unsupported encoding")
    try:
        return write_file(WORKSPACE, body.path, body.content, body.encoding)
    except UnsafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/v1/files/content", dependencies=authenticated)
async def put_file_content(
    request: Request,
    path: str = Query(...),
) -> dict[str, str | int]:
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file exceeds upload limit")
    try:
        return await write_file_stream(
            WORKSPACE,
            path,
            request.stream(),
            max_bytes=MAX_UPLOAD_BYTES,
        )
    except FileTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except UnsafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/files/list", dependencies=authenticated)
async def get_files(path: str = Query(default=".")) -> dict[str, Any]:
    try:
        return list_files(WORKSPACE, path)
    except UnsafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="directory not found") from exc


@app.delete(
    "/v1/files",
    status_code=204,
    dependencies=authenticated,
)
async def delete_file(path: str = Query(...)) -> Response:
    try:
        remove_file(WORKSPACE, path)
    except UnsafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="file not found") from exc
    return Response(status_code=204)
