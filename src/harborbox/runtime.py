from __future__ import annotations

import asyncio
import contextlib
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any

import docker
import httpx
from docker.errors import APIError, NotFound

from harborbox.runtime_protocol import StartedSandbox, WarmPoolReservation
from harborbox.schemas import (
    AgentCommandRequest,
    AgentExecutionRequest,
    AgentExecutionResponse,
    AgentProcessRequest,
    FileListResponse,
    FileReadResponse,
    FileUploadResponse,
    FileWriteRequest,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from docker.models.containers import Container

    from harborbox.config import Settings
    from harborbox.models import Sandbox


class SandboxRuntimeError(RuntimeError):
    pass


class SandboxUnavailableError(SandboxRuntimeError):
    pass


class SandboxStartTimeoutError(SandboxRuntimeError):
    """A caller-side deadline elapsed while waiting for a lazy start.

    Distinct from `SandboxUnavailableError`: the sandbox is not known to be
    broken, it is simply still starting. The start itself is not aborted --
    see `Scheduler.ensure_sandbox_ready` -- so a retry can reattach to it.
    """


class SandboxMemoryExceededError(SandboxRuntimeError):
    def __init__(self, memory_mb: int) -> None:
        super().__init__(f"sandbox exceeded its {memory_mb} MiB memory limit")


# Container exit code for a process killed by SIGKILL (128 + signal 9), which is
# how the OOM killer terminates a container; the exit code is doubled with
# OOMKilled below because the exit code alone is ambiguous (a process can `exit
# 137` on its own).
_SIGKILL_EXIT_CODE = 137


class DockerRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = docker.DockerClient(**settings.docker_kwargs)
        # Unbounded by default (`timeout=None`) meant any hung agent request —
        # a dead container, a network partition — blocked its caller forever.
        # `_post_agent` explicitly overrides back to `timeout=None` because a
        # code execution can legitimately run up to
        # `max_execution_timeout_seconds`, enforced by the agent itself; using
        # that same figure as the client default bounds every other call
        # (file read/write/list, health checks) to the longest duration this
        # service ever intentionally waits.
        self.http = httpx.AsyncClient(timeout=settings.max_execution_timeout_seconds)

    async def start(self) -> None:
        return None

    def warm_pool_reservation(self) -> WarmPoolReservation:
        return WarmPoolReservation()

    async def close(self) -> None:
        await self.http.aclose()
        await asyncio.to_thread(self.client.close)

    async def total_memory_mb(self) -> int:
        if self.settings.total_memory_mb is not None:
            return self.settings.total_memory_mb
        info = await asyncio.to_thread(self.client.info)
        return int(info["MemTotal"]) // (1024 * 1024)

    async def available_memory_mb(self) -> int:
        def read_mem_available() -> int:
            try:
                with Path("/proc/meminfo").open(encoding="utf-8") as handle:
                    for line in handle:
                        if line.startswith("MemAvailable:"):
                            return int(line.split()[1]) // 1024
            except (OSError, ValueError):
                pass
            return 0

        available = await asyncio.to_thread(read_mem_available)
        if available > 0:
            return available
        return await self.total_memory_mb()

    async def start_sandbox(self, sandbox: Sandbox) -> StartedSandbox:
        return await asyncio.to_thread(self._start_sandbox_sync, sandbox)

    def _start_sandbox_sync(self, sandbox: Sandbox) -> StartedSandbox:
        name = sandbox.container_name or f"harborbox-{sandbox.id}"
        memory_bytes = sandbox.memory_mb * 1024 * 1024
        volume_name = f"harborbox-workspace-{sandbox.id}"
        runtime_options: dict[str, Any] = {}
        template = sandbox.metadata_.get("template")
        try:
            sandbox_image = self.settings.image_for_template(template)
        except KeyError as exc:
            message = f"unknown sandbox template: {template}"
            raise SandboxUnavailableError(message) from exc
        if self.settings.sandbox_runtime:
            runtime_options["runtime"] = self.settings.sandbox_runtime

        try:
            self.client.volumes.get(volume_name)
        except NotFound:
            self.client.volumes.create(
                name=volume_name,
                labels={
                    "harborbox.managed": "true",
                    "harborbox.sandbox_id": sandbox.id,
                },
            )

        if sandbox.container_id:
            try:
                existing = self.client.containers.get(sandbox.container_id)
                existing.reload()
                if existing.status in {"exited", "created"}:
                    existing.start()
                elif existing.status == "paused":
                    existing.unpause()
                self._connect_egress(existing, sandbox)
                return StartedSandbox(existing.id, existing.name)
            except NotFound:
                pass

        container: Any | None = None
        try:
            container = self.client.containers.run(
                sandbox_image,
                detach=True,
                name=name,
                hostname="sandbox",
                network=self.settings.sandbox_network,
                environment={
                    "HARBORBOX_AGENT_TOKEN": sandbox.agent_token,
                    "HARBORBOX_WORKSPACE": "/workspace",
                    "HARBORBOX_MAX_UPLOAD_BYTES": str(
                        self.settings.max_upload_bytes
                    ),
                },
                labels={
                    "harborbox.managed": "true",
                    "harborbox.sandbox_id": sandbox.id,
                    "harborbox.template": template or "default",
                },
                volumes={volume_name: {"bind": "/workspace", "mode": "rw"}},
                user="10001:10001",
                read_only=True,
                tmpfs={
                    # Names the mount point *inside the new container*, not a
                    # path this host process touches -- the docker-py API
                    # requires the container's own /tmp here.
                    "/tmp": (  # noqa: S108
                        f"rw,nosuid,nodev,noexec,size={self.settings.sandbox_tmpfs_mb}m"
                    ),
                    "/run": "rw,nosuid,nodev,noexec,size=16m",
                },
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                pids_limit=sandbox.pids_limit,
                mem_limit=memory_bytes,
                memswap_limit=memory_bytes,
                nano_cpus=int(sandbox.cpu * 1_000_000_000),
                init=True,
                auto_remove=False,
                restart_policy={"Name": "no"},
                **runtime_options,
            )
            self._connect_egress(container, sandbox)
        except APIError as exc:
            if container is not None:
                # Best-effort cleanup of a container that failed to fully
                # start; the original failure (`exc`) is what gets raised
                # and reported either way.
                with contextlib.suppress(APIError):
                    container.remove(force=True)
            raise SandboxUnavailableError(str(exc)) from exc

        return StartedSandbox(container.id, container.name)

    def _connect_egress(self, container: Container, sandbox: Sandbox) -> None:
        """Attaches the egress network, for the sandboxes that asked for it.

        Was unconditional, which made egress an instance-wide setting: turning
        it on for one workload turned it on for every other sandbox the same
        Harborbox serves. That is the wrong granularity — widget code is handed
        its data as files and must stay unable to reach anything, while code
        that has to talk to a customer database obviously cannot.
        """
        if not sandbox.egress:
            return
        network_name = self.settings.sandbox_egress_network
        if not network_name:
            return
        network = self.client.networks.get(network_name)
        network.reload()
        attached = network.attrs.get("Containers") or {}
        if container.id not in attached:
            network.connect(container)

    async def wait_until_ready(self, sandbox: Sandbox) -> None:
        deadline = (
            asyncio.get_running_loop().time()
            + self.settings.sandbox_agent_connect_timeout_seconds
        )
        url = self._agent_url(sandbox, "/health")
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await self.http.get(
                    url,
                    headers=self._agent_headers(sandbox),
                    timeout=1.0,
                )
                if response.status_code == HTTPStatus.OK:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
        await self._raise_container_failure(sandbox)
        message = "sandbox agent did not become ready"
        raise SandboxUnavailableError(message)

    async def execute_code(
        self, sandbox: Sandbox, request: AgentExecutionRequest
    ) -> AgentExecutionResponse:
        return await self._post_agent(sandbox, "/v1/execute", request.model_dump())

    async def execute_command(
        self, sandbox: Sandbox, request: AgentCommandRequest
    ) -> AgentExecutionResponse:
        return await self._post_agent(sandbox, "/v1/commands", request.model_dump())

    async def execute_process(
        self, sandbox: Sandbox, request: AgentProcessRequest
    ) -> AgentExecutionResponse:
        return await self._post_agent(sandbox, "/v1/processes", request.model_dump())

    async def _post_agent(
        self, sandbox: Sandbox, path: str, payload: dict[str, Any]
    ) -> AgentExecutionResponse:
        try:
            response = await self.http.post(
                self._agent_url(sandbox, path),
                json=payload,
                headers=self._agent_headers(sandbox),
                timeout=None,
            )
            response.raise_for_status()
            return AgentExecutionResponse.model_validate(response.json())
        except httpx.HTTPError as exc:
            await self._raise_container_failure(sandbox)
            raise SandboxUnavailableError(str(exc)) from exc

    async def read_file(self, sandbox: Sandbox, path: str) -> FileReadResponse:
        response = await self.http.get(
            self._agent_url(sandbox, "/v1/files"),
            params={"path": path},
            headers=self._agent_headers(sandbox),
        )
        response.raise_for_status()
        return FileReadResponse.model_validate(response.json())

    async def write_file(
        self, sandbox: Sandbox, request: FileWriteRequest
    ) -> FileReadResponse:
        response = await self.http.put(
            self._agent_url(sandbox, "/v1/files"),
            json=request.model_dump(),
            headers=self._agent_headers(sandbox),
        )
        response.raise_for_status()
        return FileReadResponse.model_validate(response.json())

    async def write_file_stream(
        self,
        sandbox: Sandbox,
        path: str,
        content: AsyncIterator[bytes],
    ) -> FileUploadResponse:
        response = await self.http.put(
            self._agent_url(sandbox, "/v1/files/content"),
            params={"path": path},
            content=content,
            headers={
                **self._agent_headers(sandbox),
                "Content-Type": "application/octet-stream",
            },
        )
        response.raise_for_status()
        return FileUploadResponse.model_validate(response.json())

    async def list_files(self, sandbox: Sandbox, path: str) -> FileListResponse:
        response = await self.http.get(
            self._agent_url(sandbox, "/v1/files/list"),
            params={"path": path},
            headers=self._agent_headers(sandbox),
        )
        response.raise_for_status()
        return FileListResponse.model_validate(response.json())

    async def remove_file(self, sandbox: Sandbox, path: str) -> None:
        response = await self.http.delete(
            self._agent_url(sandbox, "/v1/files"),
            params={"path": path},
            headers=self._agent_headers(sandbox),
        )
        response.raise_for_status()

    async def pause(self, sandbox: Sandbox, *, memory: bool) -> None:
        if not sandbox.container_id:
            return
        await asyncio.to_thread(self._pause_sync, sandbox.container_id, memory=memory)

    def _pause_sync(self, container_id: str, *, memory: bool) -> None:
        try:
            container = self.client.containers.get(container_id)
            container.reload()
            if memory:
                if container.status == "running":
                    container.pause()
            else:
                # A cold pause keeps only the named workspace volume. Removing
                # the container returns all sandbox CPU/RAM and avoids keeping
                # stopped-container writable layers around for every tenant.
                container.remove(force=True)
        except NotFound:
            return

    async def resume(self, sandbox: Sandbox) -> StartedSandbox:
        if not sandbox.container_id:
            return await self.start_sandbox(sandbox)
        return await asyncio.to_thread(self._resume_sync, sandbox)

    def _resume_sync(self, sandbox: Sandbox) -> StartedSandbox:
        try:
            container = self.client.containers.get(sandbox.container_id)
            container.reload()
            if container.status == "paused":
                container.unpause()
            elif container.status in {"exited", "created"}:
                container.start()
            return StartedSandbox(container.id, container.name)
        except NotFound:
            return self._start_sandbox_sync(sandbox)

    async def kill(self, sandbox: Sandbox) -> None:
        await asyncio.to_thread(self._kill_sync, sandbox)

    def _kill_sync(self, sandbox: Sandbox) -> None:
        if sandbox.container_id:
            try:
                container = self.client.containers.get(sandbox.container_id)
                container.remove(force=True)
            except NotFound:
                pass
        volume_name = f"harborbox-workspace-{sandbox.id}"
        # Best-effort: the volume may already be gone (NotFound), or Docker
        # may refuse removal because something else still references it
        # (APIError) -- a kill should not fail just because cleanup couldn't
        # finish.
        with contextlib.suppress(NotFound, APIError):
            self.client.volumes.get(volume_name).remove(force=True)

    async def container_status(self, sandbox: Sandbox) -> str | None:
        if not sandbox.container_id:
            return None

        def get_status() -> str | None:
            try:
                container = self.client.containers.get(sandbox.container_id)
                container.reload()
                return str(container.status)
            except NotFound:
                return None

        return await asyncio.to_thread(get_status)

    async def _raise_container_failure(self, sandbox: Sandbox) -> None:
        if not sandbox.container_id:
            return

        def inspect() -> tuple[int, bool] | None:
            try:
                container = self.client.containers.get(sandbox.container_id)
                container.reload()
                state = container.attrs.get("State", {})
                return int(state.get("ExitCode", 0)), bool(state.get("OOMKilled", False))
            except NotFound:
                return None

        state = await asyncio.to_thread(inspect)
        if state and (state[1] or state[0] == _SIGKILL_EXIT_CODE):
            raise SandboxMemoryExceededError(sandbox.memory_mb)

    def _agent_url(self, sandbox: Sandbox, path: str) -> str:
        if not sandbox.container_name:
            message = "sandbox has no running container"
            raise SandboxUnavailableError(message)
        return (
            f"http://{sandbox.container_name}:{self.settings.sandbox_agent_port}{path}"
        )

    @staticmethod
    def _agent_headers(sandbox: Sandbox) -> dict[str, str]:
        return {"X-Sandbox-Token": sandbox.agent_token}
