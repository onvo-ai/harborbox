from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from jupyter_client import AsyncKernelClient, AsyncKernelManager


@dataclass
class OutputBudget:
    limit: int
    used: int = 0
    truncated: bool = False

    def take(self, text: str) -> str:
        encoded = text.encode("utf-8", errors="replace")
        remaining = max(0, self.limit - self.used)
        if len(encoded) <= remaining:
            self.used += len(encoded)
            return text
        self.truncated = True
        self.used = self.limit
        return encoded[:remaining].decode("utf-8", errors="ignore")


@dataclass
class KernelExecution:
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    truncated: bool = False


class KernelSession:
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.manager = AsyncKernelManager(kernel_name="python3")
        self.client: AsyncKernelClient | None = None
        self.lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._started = False

    async def ensure_started(self) -> None:
        """Starts the kernel on first use, at most once.

        Starting it eagerly cost every sandbox 3.5s before it would answer a
        single request — measured as the gap between uvicorn's "Waiting for
        application startup" and "Application startup complete". That is paid by
        every sandbox, including the majority that only ever run `/v1/commands`
        and never touch the kernel at all.

        Separate lock from `self.lock`: that one serialises `execute`, and
        `execute` calls this, so sharing it would deadlock on first use.
        """
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            await self.start()
            self._started = True

    async def start(self) -> None:
        await self.manager.start_kernel(cwd=self.workspace)
        client = self.manager.client()
        client.start_channels()
        await client.wait_for_ready(timeout=20)
        self.client = client

    async def stop(self) -> None:
        if self.client is not None:
            self.client.stop_channels()
        if self.manager.has_kernel:
            await self.manager.shutdown_kernel(now=True)

    async def execute(
        self,
        code: str,
        *,
        env: dict[str, str],
        timeout_seconds: int,
        max_output_bytes: int,
    ) -> KernelExecution:
        if self.client is None:
            raise RuntimeError("kernel is not running")
        async with self.lock:
            return await self._execute_locked(
                code,
                env=env,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
            )

    async def _execute_locked(
        self,
        code: str,
        *,
        env: dict[str, str],
        timeout_seconds: int,
        max_output_bytes: int,
    ) -> KernelExecution:
        assert self.client is not None
        if env:
            env_setup = (
                "import os as __harborbox_os\n"
                f"__harborbox_os.environ.update({json.dumps(env)})\n"
            )
            code = env_setup + code

        execution = KernelExecution()
        budget = OutputBudget(max_output_bytes)
        message_id = self.client.execute(
            code,
            allow_stdin=False,
            stop_on_error=True,
        )
        deadline = monotonic() + timeout_seconds

        try:
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError
                message = await self.client.get_iopub_msg(timeout=remaining)
                if message.get("parent_header", {}).get("msg_id") != message_id:
                    continue
                message_type = message.get("msg_type")
                content = message.get("content", {})
                if message_type == "stream":
                    text = budget.take(str(content.get("text", "")))
                    target = (
                        execution.stderr
                        if content.get("name") == "stderr"
                        else execution.stdout
                    )
                    if text:
                        target.append(text)
                elif message_type in {"execute_result", "display_data"}:
                    execution.results.append(self._result(content.get("data", {}), budget))
                elif message_type == "error":
                    execution.error = {
                        "name": str(content.get("ename", "ExecutionError")),
                        "value": str(content.get("evalue", "")),
                        "traceback": [
                            budget.take(str(line))
                            for line in content.get("traceback", [])
                            if budget.used < budget.limit
                        ],
                    }
                elif (
                    message_type == "status"
                    and content.get("execution_state") == "idle"
                ):
                    break
        except TimeoutError:
            await self.manager.interrupt_kernel()
            execution.error = {
                "name": "TimeoutError",
                "value": f"execution exceeded {timeout_seconds} seconds",
                "traceback": [],
            }

        execution.truncated = budget.truncated
        return execution

    @staticmethod
    def _result(data: dict[str, Any], budget: OutputBudget) -> dict[str, Any]:
        result: dict[str, Any] = {"data": {}}
        mime_mapping = {
            "text/plain": "text",
            "application/json": "json",
            "text/html": "html",
            "image/png": "png",
            "image/jpeg": "jpeg",
            "image/svg+xml": "svg",
        }
        for mime, value in data.items():
            key = mime_mapping.get(mime)
            if isinstance(value, str):
                bounded: Any = budget.take(value)
            else:
                serialized = json.dumps(value)
                bounded = value if budget.take(serialized) == serialized else None
            if key is not None:
                result[key] = bounded
            elif bounded is not None:
                result["data"][mime] = bounded
        return result

