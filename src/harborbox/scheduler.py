from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import replace

from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from harborbox.admission import Capacity, can_admit, reserve_memory
from harborbox.config import Settings
from harborbox.db import session_factory
from harborbox.execution_secrets import open_environment, scrub_environment
from harborbox.models import Execution, Sandbox, SandboxTemplate, utc_now
from harborbox.opensandbox_compat import expiration
from harborbox.runtime import SandboxMemoryExceeded, SandboxUnavailable
from harborbox.runtime_protocol import SandboxRuntime
from harborbox.schemas import (
    AgentCommandRequest,
    AgentExecutionRequest,
    AgentProcessRequest,
)
from harborbox.template_builder import TemplateBuilder

logger = logging.getLogger(__name__)

ACTIVE_EXECUTION_STATES = ("admitted", "starting", "running")
# Memory is genuinely held by a pooled container, so it is reserved here. CPU is
# deliberately *not*: `capacity()` sums cpu over ("starting", "running") only, so
# an idle pool cannot eat the max_parallel_cpu budget and starve real executions
# — the failure mode that made every widget wait on `waiting_for: cpu`.
RESERVED_SANDBOX_STATES = (
    "starting",
    "running",
    "paused_memory",
    "pooling",
    "pooled",
)
TERMINAL_EXECUTION_STATES = ("succeeded", "failed", "cancelled")


def may_start_execution(
    *,
    cancel_requested: bool,
    execution_status: str,
    sandbox_status: str,
) -> bool:
    """Whether an admitted execution should actually be started.

    Admission and start are separate transactions, so everything admission
    checked can have changed in between — most importantly the sandbox can have
    been deleted. Starting one anyway creates a container nobody owns: the caller
    already holds its 204 and will never delete again, so it lives until the idle
    reaper, holding CPU and a memory reservation the scheduler still counts.
    """
    if cancel_requested:
        return False
    if execution_status in TERMINAL_EXECUTION_STATES:
        return False
    return sandbox_status not in {"killed", "failed"}


def has_sandbox_execution_slot(
    *,
    kind: str,
    active_count: int,
    active_code: bool,
    limit: int,
) -> bool:
    if active_count >= limit or active_code:
        return False
    return kind in {"command", "process"} or active_count == 0


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        runtime: SandboxRuntime,
        template_builder: TemplateBuilder | None = None,
    ) -> None:
        self.settings = settings
        self.runtime = runtime
        self.template_builder = template_builder
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._reaper_task: asyncio.Task[None] | None = None
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._sandbox_start_locks: dict[str, asyncio.Lock] = {}
        self._last_template_sweep: float | None = None

    async def start(self) -> None:
        await self._recover_interrupted_jobs()
        self._loop_task = asyncio.create_task(self._scheduler_loop())
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def stop(self) -> None:
        self._stop.set()
        tasks = [
            task
            for task in (self._loop_task, self._reaper_task)
            if task
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._running_tasks:
            await asyncio.gather(*self._running_tasks.values(), return_exceptions=True)

    async def capacity(self) -> Capacity:
        total_memory_mb = await self.runtime.total_memory_mb()
        host_available_memory_mb = await self.runtime.available_memory_mb()
        warm_pool = self.runtime.warm_pool_reservation()
        reserve_mb = reserve_memory(
            total_memory_mb,
            self.settings.host_memory_reserve_percent,
            self.settings.host_memory_reserve_min_mb,
        )
        max_cpu = self.settings.max_parallel_cpu or float(max(1, os.cpu_count() or 1))

        async with session_factory() as session:
            reserved_memory = await session.scalar(
                select(func.coalesce(func.sum(Sandbox.memory_mb), 0)).where(
                    Sandbox.status.in_(RESERVED_SANDBOX_STATES)
                )
            )
            reserved_cpu = await session.scalar(
                select(func.coalesce(func.sum(Sandbox.cpu), 0.0)).where(
                    Sandbox.status.in_(("starting", "running"))
                )
            )

        return Capacity(
            total_memory_mb=total_memory_mb,
            reserve_memory_mb=reserve_mb,
            platform_memory_reserve_mb=self.settings.platform_memory_reserve_mb,
            reserved_memory_mb=int(reserved_memory or 0) + warm_pool.memory_mb,
            host_available_memory_mb=host_available_memory_mb,
            reserved_cpu=float(reserved_cpu or 0.0) + warm_pool.cpu,
            max_parallel_cpu=max_cpu,
            configured_sandbox_budget_mb=(
                self.settings.sandbox_memory_budget_mb
            ),
            warm_pool_reserved_memory_mb=warm_pool.memory_mb,
            warm_pool_reserved_cpu=warm_pool.cpu,
            warm_pool_target_sandboxes=warm_pool.sandboxes,
        )

    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._admit_available_jobs()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler iteration failed")
            await asyncio.sleep(self.settings.scheduler_poll_seconds)

    async def _admit_available_jobs(self) -> None:
        capacity = await self.capacity()
        now = utc_now()
        admitted_ids: list[str] = []
        async with session_factory() as session:
            # FOR UPDATE so admission serialises against anything else that
            # touches these rows — in particular DELETE /v1/sandboxes, which
            # cancels queued executions. Without the lock both sides read, then
            # both write, and the later commit silently wins.
            result = await session.execute(
                select(Execution)
                .options(selectinload(Execution.sandbox))
                .where(Execution.status == "queued")
                .order_by(Execution.created_at, Execution.id)
                .limit(self.settings.scheduler_scan_limit)
                .with_for_update(of=Execution)
            )
            queued = list(result.scalars())

            active_rows = (
                await session.execute(
                    select(Execution.sandbox_id, Execution.kind, func.count())
                    .where(Execution.status.in_(ACTIVE_EXECUTION_STATES))
                    .group_by(Execution.sandbox_id, Execution.kind)
                )
            ).all()
            active_counts: dict[str, int] = {}
            active_code_sandboxes: set[str] = set()
            for sandbox_id, kind, count in active_rows:
                active_counts[sandbox_id] = active_counts.get(sandbox_id, 0) + int(
                    count
                )
                if kind == "code":
                    active_code_sandboxes.add(sandbox_id)

            for index, execution in enumerate(queued):
                sandbox = execution.sandbox
                if execution.cancel_requested:
                    execution.status = "cancelled"
                    execution.finished_at = now
                    continue
                if sandbox.status in {"killed", "failed"}:
                    execution.status = "failed"
                    execution.finished_at = now
                    execution.error = {
                        "name": "SandboxUnavailable",
                        "value": f"sandbox is {sandbox.status}",
                        "traceback": [],
                    }
                    continue
                active_count = active_counts.get(sandbox.id, 0)
                if not has_sandbox_execution_slot(
                    kind=execution.kind,
                    active_count=active_count,
                    active_code=sandbox.id in active_code_sandboxes,
                    limit=self.settings.max_concurrent_executions_per_sandbox,
                ):
                    continue

                already_reserved = sandbox.status in RESERVED_SANDBOX_STATES
                incremental_memory = 0 if already_reserved else sandbox.memory_mb
                incremental_cpu = (
                    0.0 if sandbox.status in {"running", "starting"} else sandbox.cpu
                )
                decision = can_admit(
                    capacity,
                    incremental_memory_mb=incremental_memory,
                    incremental_cpu=incremental_cpu,
                    emergency_available_memory_mb=(
                        self.settings.emergency_available_memory_mb
                    ),
                )
                if not decision.admitted:
                    waited = (now - execution.created_at).total_seconds()
                    if index == 0 and waited >= self.settings.queue_aging_seconds:
                        break
                    continue

                execution.status = "admitted"
                execution.admitted_at = now
                if not already_reserved:
                    sandbox.status = "starting"
                admitted_ids.append(execution.id)
                active_counts[sandbox.id] = active_count + 1
                if execution.kind == "code":
                    active_code_sandboxes.add(sandbox.id)
                capacity = replace(
                    capacity,
                    reserved_memory_mb=(
                        capacity.reserved_memory_mb + incremental_memory
                    ),
                    reserved_cpu=capacity.reserved_cpu + incremental_cpu,
                    host_available_memory_mb=(
                        capacity.host_available_memory_mb - incremental_memory
                    ),
                )
            await session.commit()

        for execution_id in admitted_ids:
            task = asyncio.create_task(self._run_execution(execution_id))
            self._running_tasks[execution_id] = task
            task.add_done_callback(self._done_callback(execution_id))

    def _done_callback(
        self, execution_id: str
    ) -> Callable[[asyncio.Task[None]], None]:
        def callback(task: asyncio.Task[None]) -> None:
            self._task_done(execution_id, task)

        return callback

    def _task_done(self, execution_id: str, task: asyncio.Task[None]) -> None:
        self._running_tasks.pop(execution_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "execution task crashed: %r",
                task.exception(),
                extra={"execution_id": execution_id},
            )

    async def _run_execution(self, execution_id: str) -> None:
        try:
            async with session_factory() as session:
                execution = await session.scalar(
                    select(Execution)
                    .options(selectinload(Execution.sandbox))
                    .where(Execution.id == execution_id)
                )
                if execution is None:
                    return
                sandbox = execution.sandbox
                # Re-validate rather than trust admission — see may_start_execution.
                if not may_start_execution(
                    cancel_requested=execution.cancel_requested,
                    execution_status=execution.status,
                    sandbox_status=sandbox.status,
                ):
                    return
                execution.status = "starting"
                if sandbox.status in {"created", "paused_cold"}:
                    sandbox.status = "starting"
                await session.commit()

            await self._ensure_running(sandbox.id)

            async with session_factory() as session:
                execution = await session.scalar(
                    select(Execution)
                    .options(selectinload(Execution.sandbox))
                    .where(Execution.id == execution_id)
                )
                if execution is None:
                    return
                execution.status = "running"
                execution.started_at = utc_now()
                execution.sandbox.last_activity_at = utc_now()
                await session.commit()
                sandbox = execution.sandbox

            environment = open_environment(
                self.settings,
                execution.environment,
            )
            if execution.kind == "code":
                response = await self.runtime.execute_code(
                    sandbox,
                    AgentExecutionRequest(
                        code=execution.code or "",
                        timeout_seconds=execution.timeout_seconds,
                        max_output_bytes=self.settings.max_output_bytes,
                        env=environment,
                    ),
                )
            elif execution.kind == "command":
                response = await self.runtime.execute_command(
                    sandbox,
                    AgentCommandRequest(
                        command=execution.command or "",
                        timeout_seconds=execution.timeout_seconds,
                        max_output_bytes=self.settings.max_output_bytes,
                        env=environment,
                        cwd=execution.cwd,
                    ),
                )
            else:
                process_spec = json.loads(execution.command or "{}")
                response = await self.runtime.execute_process(
                    sandbox,
                    AgentProcessRequest(
                        executable=process_spec["executable"],
                        args=process_spec.get("args", []),
                        stdin=process_spec.get("stdin"),
                        timeout_seconds=execution.timeout_seconds,
                        max_output_bytes=self.settings.max_output_bytes,
                        env=environment,
                        cwd=execution.cwd,
                    ),
                )

            async with session_factory() as session:
                execution = await session.get(Execution, execution_id)
                if execution is None:
                    return
                execution.finished_at = utc_now()
                execution.environment = scrub_environment(execution.environment)
                execution.result = {
                    "logs": response.logs.model_dump(mode="json"),
                    "results": [
                        result.model_dump(mode="json", by_alias=True)
                        for result in response.results
                    ],
                    "exit_code": response.exit_code,
                }
                if response.error is not None:
                    execution.status = "failed"
                    execution.error = response.error.model_dump(mode="json")
                elif response.exit_code not in (None, 0):
                    execution.status = "failed"
                    execution.error = {
                        "name": "CommandFailed",
                        "value": f"command exited with status {response.exit_code}",
                        "traceback": [],
                    }
                else:
                    execution.status = "succeeded"
                current_sandbox = await session.get(Sandbox, execution.sandbox_id)
                if current_sandbox is not None:
                    current_sandbox.last_activity_at = utc_now()
                await session.commit()
        except SandboxMemoryExceeded as exc:
            await self._fail_execution(execution_id, "MemoryLimitExceeded", str(exc))
        except SandboxUnavailable as exc:
            await self._fail_execution(execution_id, "SandboxUnavailable", str(exc))
        except Exception as exc:
            logger.exception("execution failed", extra={"execution_id": execution_id})
            await self._fail_execution(execution_id, type(exc).__name__, str(exc))

    async def _ensure_running(self, sandbox_id: str) -> None:
        lock = self._sandbox_start_locks.setdefault(sandbox_id, asyncio.Lock())
        async with lock:
            await self._ensure_running_locked(sandbox_id)

    async def _ensure_running_locked(self, sandbox_id: str) -> None:
        async with session_factory() as session:
            sandbox = await session.get(Sandbox, sandbox_id)
            if sandbox is None:
                message = "sandbox does not exist"
                raise SandboxUnavailable(message)
            previous_status = sandbox.status

        if previous_status == "paused_memory":
            started = await self.runtime.resume(sandbox)
        elif previous_status in {"created", "starting", "paused_cold"}:
            started = await self.runtime.start_sandbox(sandbox)
        elif previous_status == "running":
            started = None
        else:
            message = f"sandbox is {previous_status}"
            raise SandboxUnavailable(message)
        runtime_metadata = dict(sandbox.metadata_)

        async with session_factory() as session:
            sandbox = await session.get(Sandbox, sandbox_id)
            if sandbox is None:
                message = "sandbox does not exist"
                raise SandboxUnavailable(message)
            # The container exists now, so a sandbox killed while it was starting
            # must have its container removed here. Writing `running` over
            # `killed` is the write that used to strand it: DELETE's own
            # runtime.kill() found no container to remove, having run before this
            # one was created.
            if sandbox.status in {"killed", "failed"}:
                if started is not None:
                    sandbox.container_id = started.id
                    sandbox.container_name = started.name
                    await session.commit()
                    await self.runtime.kill(sandbox)
                    async with session_factory() as cleanup:
                        current = await cleanup.get(Sandbox, sandbox_id)
                        if current is not None:
                            current.container_id = None
                            current.container_name = None
                            await cleanup.commit()
                message = f"sandbox is {sandbox.status}"
                raise SandboxUnavailable(message)
            if started is not None:
                sandbox.container_id = started.id
                sandbox.container_name = started.name
                sandbox.metadata_ = runtime_metadata
            sandbox.status = "running"
            sandbox.last_activity_at = utc_now()
            await session.commit()
            await session.refresh(sandbox)
            ready_sandbox = sandbox

        await self.runtime.wait_until_ready(ready_sandbox)

    async def _fail_execution(self, execution_id: str, name: str, value: str) -> None:
        async with session_factory() as session:
            execution = await session.get(Execution, execution_id)
            if execution is None or execution.status in TERMINAL_EXECUTION_STATES:
                return
            execution.status = "failed"
            execution.finished_at = utc_now()
            execution.environment = scrub_environment(execution.environment)
            execution.error = {"name": name, "value": value, "traceback": []}
            sandbox = await session.get(Sandbox, execution.sandbox_id)
            if sandbox is not None:
                container_status = await self.runtime.container_status(sandbox)
                if container_status in {None, "dead", "exited"}:
                    sandbox.status = "failed"
            await session.commit()

    async def _reaper_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._terminate_expired_sandboxes()
                await self._cold_pause_idle_sandboxes()
                await self._sweep_unused_templates()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("idle sandbox reaper failed")
            await asyncio.sleep(self.settings.reaper_poll_seconds)

    async def _sweep_unused_templates(self) -> None:
        """Reclaim derived template images on the reaper's existing cadence.

        The reaper polls every second; the sweep is rate-limited to its own,
        much longer interval rather than given a loop of its own.
        """
        if self.template_builder is None or not self.settings.template_gc_enabled:
            return
        now = asyncio.get_running_loop().time()
        if (
            self._last_template_sweep is not None
            and now - self._last_template_sweep
            < self.settings.template_gc_interval_seconds
        ):
            return
        self._last_template_sweep = now
        await self.template_builder.collect_unused_templates()

    async def _terminate_expired_sandboxes(self) -> None:
        now = utc_now()
        async with session_factory() as session:
            sandboxes = list(
                (
                    await session.scalars(
                        select(Sandbox).where(
                            Sandbox.status.not_in(("killed", "failed")),
                            Sandbox.id.not_in(
                                select(Execution.sandbox_id).where(
                                    Execution.status.in_(ACTIVE_EXECUTION_STATES)
                                )
                            ),
                        )
                    )
                ).all()
            )
        for sandbox in sandboxes:
            expires_at = expiration(sandbox)
            if expires_at is None or expires_at > now:
                continue
            await self.runtime.kill(sandbox)
            async with session_factory() as session:
                current = await session.get(Sandbox, sandbox.id)
                if current is not None and current.status not in {"killed", "failed"}:
                    current.status = "killed"
                    current.container_id = None
                    current.container_name = None
                    current.metadata_ = dict(sandbox.metadata_)
                    await session.commit()

    async def _cold_pause_idle_sandboxes(self) -> None:
        now = utc_now()
        async with session_factory() as session:
            result = await session.execute(
                select(Sandbox).where(
                    Sandbox.status == "running",
                    Sandbox.id.not_in(
                        select(Execution.sandbox_id).where(
                            Execution.status.in_(ACTIVE_EXECUTION_STATES)
                        )
                    ),
                )
            )
            sandboxes = list(result.scalars())

        for sandbox in sandboxes:
            if sandbox.idle_timeout_seconds == 0:
                continue
            idle_for = (now - sandbox.last_activity_at).total_seconds()
            if idle_for < sandbox.idle_timeout_seconds:
                continue
            await self.runtime.pause(sandbox, memory=False)
            async with session_factory() as session:
                current = await session.get(Sandbox, sandbox.id)
                if current is not None and current.status == "running":
                    current.status = "paused_cold"
                    current.container_id = None
                    current.container_name = None
                    current.metadata_ = dict(sandbox.metadata_)
                    await session.commit()

    async def _recover_interrupted_jobs(self) -> None:
        async with session_factory() as session:
            await session.execute(
                update(Execution)
                .where(Execution.status.in_(ACTIVE_EXECUTION_STATES))
                .values(
                    status="failed",
                    finished_at=utc_now(),
                    error={
                        "name": "ControlPlaneRestarted",
                        "value": "control plane restarted while execution was active",
                        "traceback": [],
                    },
                )
            )
            # An image build cancelled mid-flight cannot reliably record its own
            # failure, so a `building` row surviving a restart is stale. Failing
            # it here makes it re-buildable: POST /v1/templates retries failures.
            await session.execute(
                update(SandboxTemplate)
                .where(SandboxTemplate.status == "building")
                .values(
                    status="failed",
                    error="control plane restarted while the image build was in flight",
                    updated_at=utc_now(),
                )
            )
            starting_sandboxes = list(
                (
                    await session.scalars(
                        select(Sandbox).where(Sandbox.status == "starting")
                    )
                ).all()
            )
            for sandbox in starting_sandboxes:
                container_status = await self.runtime.container_status(sandbox)
                if container_status == "running":
                    sandbox.status = "running"
                elif sandbox.container_id:
                    sandbox.status = "paused_cold"
                else:
                    sandbox.status = "created"
            await session.commit()
