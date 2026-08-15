from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from harborbox.admission import Capacity, can_admit, reserve_memory
from harborbox.db import session_factory
from harborbox.execution_secrets import open_environment, scrub_environment
from harborbox.models import Execution, Sandbox, SandboxTemplate, utc_now
from harborbox.opensandbox_compat import expiration
from harborbox.runtime import (
    SandboxMemoryExceededError,
    SandboxStartTimeoutError,
    SandboxUnavailableError,
)
from harborbox.schemas import (
    AgentCommandRequest,
    AgentExecutionRequest,
    AgentProcessRequest,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from harborbox.config import Settings
    from harborbox.runtime_protocol import SandboxRuntime
    from harborbox.schemas import AgentExecutionResponse
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

# Public error-taxonomy code written to `execution.error.name` and returned
# to API clients (see presenters.py -> ExecutionResponse.error.name). This is
# a wire contract, not a Python identifier: it is deliberately decoupled from
# the `SandboxUnavailableError` class name so that renaming the exception
# class does not silently change what callers see and match against (a
# previous pass did exactly that; this is the fix). Do not rename this value
# to track a future class rename without a coordinated API change.
ERROR_NAME_SANDBOX_UNAVAILABLE = "SandboxUnavailable"

# Same wire-contract reasoning as ERROR_NAME_SANDBOX_UNAVAILABLE above: this is
# what `execution.error.name` carries over the API, kept as an explicit
# constant rather than a literal scattered across `_run_execution` so the two
# never drift independently of each other again.
ERROR_NAME_MEMORY_LIMIT_EXCEEDED = "MemoryLimitExceeded"

TERMINAL_EXECUTION_STATES = ("succeeded", "failed", "cancelled")


def lazy_start_action(status: str) -> Literal["ready", "start", "unavailable"]:
    """Whether a request needing a running sandbox should proceed, start, or fail.

    Used by API endpoints that touch the runtime directly -- file I/O and the
    status-configuring PATCH -- which used to hard-require `running` and 409
    otherwise. Nothing but `create_execution`/`create_command`/`create_process`
    ever started a sandbox, so a sandbox whose first call was a file write
    (Onvo's own pattern: create, upload, transform) 409'd every time, even
    though `_ensure_running` already knows how to bring one up.

    Mirrors `may_start_execution`'s terminal check: `killed`/`failed`
    sandboxes never come back, so nothing should try to start one. Every
    other non-running status (`created`, `starting`, `paused_cold`,
    `paused_memory`) is exactly what `_ensure_running_locked` handles.
    """
    if status == "running":
        return "ready"
    if status in {"killed", "failed"}:
        return "unavailable"
    return "start"


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


@dataclass
class _ScanState:
    """Mutable bookkeeping threaded through one `_admit_available_jobs` sweep.

    Bundled so `_admit_or_defer` takes one state argument instead of four,
    keeping it under the too-many-arguments threshold.
    """

    capacity: Capacity
    active_counts: dict[str, int] = field(default_factory=dict)
    active_code_sandboxes: set[str] = field(default_factory=set)


def _reject_ineligible(execution: Execution, sandbox: Sandbox, now: datetime) -> bool:
    """Resolve `execution` in place if it should never be admitted.

    Split out of `_admit_available_jobs`'s scan loop to keep that loop's
    branch count under the complexity threshold. Returns True if the
    execution was resolved here (the caller should skip it).
    """
    if execution.cancel_requested:
        execution.status = "cancelled"
        execution.finished_at = now
        return True
    if sandbox.status in {"killed", "failed"}:
        execution.status = "failed"
        execution.finished_at = now
        execution.error = {
            "name": ERROR_NAME_SANDBOX_UNAVAILABLE,
            "value": f"sandbox is {sandbox.status}",
            "traceback": [],
        }
        return True
    return False


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
        # Keyed by sandbox_id, tracks a lazy start triggered outside the
        # execution path (see `ensure_sandbox_ready`) so a second concurrent
        # caller reattaches to the same in-flight start instead of racing
        # `_ensure_running`'s lock to spawn a duplicate task, and so the task
        # is not garbage-collected while nothing else holds a reference to it.
        self._pending_starts: dict[str, asyncio.Task[None]] = {}
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
        if self._pending_starts:
            await asyncio.gather(*self._pending_starts.values(), return_exceptions=True)

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
            state = _ScanState(capacity=await self.capacity())
            for sandbox_id, kind, count in active_rows:
                state.active_counts[sandbox_id] = state.active_counts.get(
                    sandbox_id, 0
                ) + int(count)
                if kind == "code":
                    state.active_code_sandboxes.add(sandbox_id)

            for index, execution in enumerate(queued):
                sandbox = execution.sandbox
                if _reject_ineligible(execution, sandbox, now):
                    continue
                active_count = state.active_counts.get(sandbox.id, 0)
                if not has_sandbox_execution_slot(
                    kind=execution.kind,
                    active_count=active_count,
                    active_code=sandbox.id in state.active_code_sandboxes,
                    limit=self.settings.max_concurrent_executions_per_sandbox,
                ):
                    continue

                admitted, stop = self._admit_or_defer(
                    execution, sandbox, index=index, now=now, state=state
                )
                if admitted:
                    admitted_ids.append(execution.id)
                if stop:
                    break
            await session.commit()

        for execution_id in admitted_ids:
            task = asyncio.create_task(self._run_execution(execution_id))
            self._running_tasks[execution_id] = task
            task.add_done_callback(self._done_callback(execution_id))

    def _admit_or_defer(
        self,
        execution: Execution,
        sandbox: Sandbox,
        *,
        index: int,
        now: datetime,
        state: _ScanState,
    ) -> tuple[bool, bool]:
        """Admit `execution` if there is room under `state.capacity`.

        Split out of `_admit_available_jobs`'s scan loop to keep that loop's
        branch count under the complexity threshold. Mutates `execution`,
        `sandbox` and `state` in place, the same way the inline loop body
        used to. Returns `(admitted, stop)`: whether this execution was
        admitted, and whether the scan should stop (the head of the queue
        aged past `queue_aging_seconds`).
        """
        already_reserved = sandbox.status in RESERVED_SANDBOX_STATES
        incremental_memory = 0 if already_reserved else sandbox.memory_mb
        incremental_cpu = (
            0.0 if sandbox.status in {"running", "starting"} else sandbox.cpu
        )
        decision = can_admit(
            state.capacity,
            incremental_memory_mb=incremental_memory,
            incremental_cpu=incremental_cpu,
            emergency_available_memory_mb=self.settings.emergency_available_memory_mb,
        )
        if not decision.admitted:
            waited = (now - execution.created_at).total_seconds()
            stop = index == 0 and waited >= self.settings.queue_aging_seconds
            return False, stop

        execution.status = "admitted"
        execution.admitted_at = now
        if not already_reserved:
            sandbox.status = "starting"
        state.active_counts[sandbox.id] = state.active_counts.get(sandbox.id, 0) + 1
        if execution.kind == "code":
            state.active_code_sandboxes.add(sandbox.id)
        state.capacity = replace(
            state.capacity,
            reserved_memory_mb=state.capacity.reserved_memory_mb + incremental_memory,
            reserved_cpu=state.capacity.reserved_cpu + incremental_cpu,
            host_available_memory_mb=(
                state.capacity.host_available_memory_mb - incremental_memory
            ),
        )
        return True, False

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
        """Run one admitted execution end to end.

        Split into a slim try/except orchestrator over four private steps
        (`_mark_starting`, `_ensure_running`, `_mark_running`,
        `_execute_request`, `_record_result`) purely to stay under the
        complexity/branches/statements thresholds; the sequence of work and
        the exception handling are unchanged from before the split.
        """
        try:
            sandbox = await self._mark_starting(execution_id)
            if sandbox is None:
                return
            await self._ensure_running(sandbox.id)

            started = await self._mark_running(execution_id)
            if started is None:
                return
            execution, sandbox = started

            environment = open_environment(self.settings, execution.environment)
            response = await self._execute_request(sandbox, execution, environment)
            await self._record_result(execution_id, response)
        except SandboxMemoryExceededError as exc:
            await self._fail_execution(
                execution_id, ERROR_NAME_MEMORY_LIMIT_EXCEEDED, str(exc)
            )
        except SandboxUnavailableError as exc:
            await self._fail_execution(
                execution_id, ERROR_NAME_SANDBOX_UNAVAILABLE, str(exc)
            )
        except Exception as exc:
            logger.exception("execution failed", extra={"execution_id": execution_id})
            await self._fail_execution(execution_id, type(exc).__name__, str(exc))

    async def _mark_starting(self, execution_id: str) -> Sandbox | None:
        """Validate and mark an admitted execution as starting.

        Returns the sandbox to bring up, or None if the execution should not
        run (already resolved, or its sandbox died between admission and
        here — see `may_start_execution`).
        """
        async with session_factory() as session:
            execution = await session.scalar(
                select(Execution)
                .options(selectinload(Execution.sandbox))
                .where(Execution.id == execution_id)
            )
            if execution is None:
                return None
            sandbox = execution.sandbox
            # Re-validate rather than trust admission — see may_start_execution.
            if not may_start_execution(
                cancel_requested=execution.cancel_requested,
                execution_status=execution.status,
                sandbox_status=sandbox.status,
            ):
                return None
            execution.status = "starting"
            if sandbox.status in {"created", "paused_cold"}:
                sandbox.status = "starting"
            await session.commit()
            return sandbox

    async def _mark_running(
        self, execution_id: str
    ) -> tuple[Execution, Sandbox] | None:
        """Mark a started execution as running, once its sandbox is up."""
        async with session_factory() as session:
            execution = await session.scalar(
                select(Execution)
                .options(selectinload(Execution.sandbox))
                .where(Execution.id == execution_id)
            )
            if execution is None:
                return None
            execution.status = "running"
            execution.started_at = utc_now()
            execution.sandbox.last_activity_at = utc_now()
            await session.commit()
            return execution, execution.sandbox

    async def _execute_request(
        self,
        sandbox: Sandbox,
        execution: Execution,
        environment: dict[str, str],
    ) -> AgentExecutionResponse:
        """Dispatch to the runtime call matching `execution.kind`."""
        if execution.kind == "code":
            return await self.runtime.execute_code(
                sandbox,
                AgentExecutionRequest(
                    code=execution.code or "",
                    timeout_seconds=execution.timeout_seconds,
                    max_output_bytes=self.settings.max_output_bytes,
                    env=environment,
                ),
            )
        if execution.kind == "command":
            return await self.runtime.execute_command(
                sandbox,
                AgentCommandRequest(
                    command=execution.command or "",
                    timeout_seconds=execution.timeout_seconds,
                    max_output_bytes=self.settings.max_output_bytes,
                    env=environment,
                    cwd=execution.cwd,
                ),
            )
        process_spec = json.loads(execution.command or "{}")
        return await self.runtime.execute_process(
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

    async def _record_result(
        self, execution_id: str, response: AgentExecutionResponse
    ) -> None:
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

    async def ensure_sandbox_ready(
        self, sandbox_id: str, *, timeout_seconds: float
    ) -> None:
        """Lazily bring `sandbox_id` to `running`, bounded by `timeout_seconds`.

        The entry point for callers outside the execution path -- see
        `lazy_start_action` -- that need a running sandbox right now rather
        than queueing an execution for one. Goes through `_ensure_running`,
        the same per-sandbox lock `_run_execution` uses, so a request that
        lands on a `created` sandbox at the same moment a queued execution
        admits it can never produce two containers for one sandbox: both
        wait on the same lock, and whichever loses re-reads `running` from
        the database and does nothing.

        A second concurrent caller for the same sandbox reattaches to the one
        in-flight task via `_pending_starts` rather than spawning another —
        belt-and-suspenders alongside the lock, and what keeps a start alive
        against garbage collection while it runs in the background (see
        below).

        On timeout, the underlying start is not cancelled: `asyncio.shield`
        keeps `_ensure_running` running in `_pending_starts` after this call
        raises, because aborting it mid-`start_sandbox` would strand a
        container the sandbox row never learns about. The caller gets a
        clear, bounded error and can retry; the retry either finds `running`
        already or reattaches to the same task.
        """
        task = self._pending_starts.get(sandbox_id)
        if task is None or task.done():
            task = asyncio.create_task(self._ensure_running(sandbox_id))
            self._pending_starts[sandbox_id] = task
            task.add_done_callback(self._clear_pending_start(sandbox_id, task))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except TimeoutError as exc:
            message = (
                f"sandbox did not become ready within {timeout_seconds:.0f}s "
                "(the start is continuing in the background; retry)"
            )
            raise SandboxStartTimeoutError(message) from exc

    def _clear_pending_start(
        self, sandbox_id: str, task: asyncio.Task[None]
    ) -> Callable[[asyncio.Task[None]], None]:
        def callback(_done: asyncio.Task[None]) -> None:
            if self._pending_starts.get(sandbox_id) is task:
                self._pending_starts.pop(sandbox_id, None)

        return callback

    async def _ensure_running(self, sandbox_id: str) -> None:
        lock = self._sandbox_start_locks.setdefault(sandbox_id, asyncio.Lock())
        async with lock:
            await self._ensure_running_locked(sandbox_id)

    async def _ensure_running_locked(self, sandbox_id: str) -> None:
        async with session_factory() as session:
            sandbox = await session.get(Sandbox, sandbox_id)
            if sandbox is None:
                message = "sandbox does not exist"
                raise SandboxUnavailableError(message)
            previous_status = sandbox.status

        if previous_status == "paused_memory":
            started = await self.runtime.resume(sandbox)
        elif previous_status in {"created", "starting", "paused_cold"}:
            started = await self.runtime.start_sandbox(sandbox)
        elif previous_status == "running":
            started = None
        else:
            message = f"sandbox is {previous_status}"
            raise SandboxUnavailableError(message)
        runtime_metadata = dict(sandbox.metadata_)

        async with session_factory() as session:
            sandbox = await session.get(Sandbox, sandbox_id)
            if sandbox is None:
                message = "sandbox does not exist"
                raise SandboxUnavailableError(message)
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
                raise SandboxUnavailableError(message)
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
