from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from opensandbox import Sandbox as OpenSandbox
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxException
from opensandbox.pool_async import SandboxPoolAsync
from opensandbox.pool_types import AcquirePolicy, PoolCreationSpec

from harborbox.config import Settings
from harborbox.db import session_factory
from harborbox.postgres_pool_store import AsyncPostgresPoolStateStore
from harborbox.runtime_protocol import WarmPoolReservation

logger = logging.getLogger(__name__)


class OpenSandboxWarmPools:
    """Adaptive, PostgreSQL-coordinated pools of disposable template sandboxes."""

    def __init__(self, settings: Settings, connection: ConnectionConfig) -> None:
        self.settings = settings
        self.connection = connection
        self._pools: dict[str, SandboxPoolAsync] = {}
        self._configured_targets = {
            template: count
            for template, count in settings.warm_pool_sizes.items()
            if count > 0
        }
        self._active_targets: dict[str, int] = {}
        self._last_demand: dict[str, float] = {}
        self._maintenance_task: asyncio.Task[None] | None = None
        self._renew_tasks: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        self._started = False

    async def start(self) -> None:
        if not self.settings.warm_pool_enabled or not self._configured_targets:
            return

        try:
            store = AsyncPostgresPoolStateStore(session_factory)
            now = asyncio.get_running_loop().time()
            for template, target in self._configured_targets.items():
                memory_mb, cpu = self.settings.resources_for_template(template)
                pool = SandboxPoolAsync(
                    pool_name=self._pool_name(template),
                    max_idle=target,
                    state_store=store,
                    connection_config=self.connection,
                    creation_spec=PoolCreationSpec(
                        image=self.settings.image_for_template(template),
                        entrypoint=self.settings.entrypoint_for_template(template),
                        resource={"cpu": str(cpu), "memory": f"{memory_mb}Mi"},
                        metadata={
                            "harborbox.warm_pool": "true",
                            "harborbox.template": template,
                            "harborbox.template_version": (
                                self.settings.template_version
                            ),
                        },
                    ),
                    warmup_concurrency=min(
                        target, self.settings.warm_pool_warmup_concurrency
                    ),
                    reconcile_interval=timedelta(
                        seconds=self.settings.warm_pool_reconcile_seconds
                    ),
                    acquire_ready_timeout=timedelta(
                        seconds=self.settings.opensandbox_ready_timeout_seconds
                    ),
                    acquire_skip_health_check=True,
                    warmup_ready_timeout=timedelta(
                        seconds=self.settings.opensandbox_ready_timeout_seconds
                    ),
                    idle_timeout=timedelta(
                        seconds=self.settings.warm_pool_idle_ttl_seconds
                    ),
                    acquire_min_remaining_ttl=timedelta(seconds=30),
                )
                self._pools[template] = pool
                self._active_targets[template] = target
                self._last_demand[template] = now

            await asyncio.gather(*(pool.start() for pool in self._pools.values()))
        except Exception:
            logger.exception("Warm pools unavailable; using direct sandbox creation")
            await asyncio.gather(
                *(pool.shutdown(graceful=False) for pool in self._pools.values()),
                return_exceptions=True,
            )
            self._pools.clear()
            self._active_targets.clear()
            return

        self._started = True
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(), name="harborbox-warm-pool-maintenance"
        )

    async def close(self) -> None:
        self._stop.set()
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
        if self._renew_tasks:
            await asyncio.gather(*self._renew_tasks, return_exceptions=True)

        if self.settings.warm_pool_release_on_shutdown:
            await asyncio.gather(
                *(pool.release_all_idle() for pool in self._pools.values()),
                return_exceptions=True,
            )
        await asyncio.gather(
            *(pool.shutdown(graceful=True) for pool in self._pools.values()),
            return_exceptions=True,
        )
        self._started = False

    async def acquire(
        self, *, template: str | None, memory_mb: int, cpu: float
    ) -> OpenSandbox | None:
        if template is None:
            return None
        if template not in self._pools:
            if self.settings.base_of_derived_template(template) is not None:
                # An accepted tradeoff: pooling per derived template would trade
                # this design's bounded image count for an unbounded pool count.
                logger.debug(
                    "Derived template %s has no warm pool; cold starting", template
                )
            return None
        expected_memory, expected_cpu = self.settings.resources_for_template(template)
        if memory_mb != expected_memory or abs(cpu - expected_cpu) > 0.000_001:
            return None

        pool = self._pools[template]
        self._last_demand[template] = asyncio.get_running_loop().time()
        target = self._configured_targets[template]
        if self._active_targets.get(template, 0) == 0:
            await pool.resize(target)
            self._active_targets[template] = target

        try:
            sandbox = await pool.acquire(
                sandbox_timeout=None,
                policy=AcquirePolicy.FAIL_FAST,
            )
            task = asyncio.create_task(
                self._renew_acquired(sandbox),
                name=f"harborbox-warm-pool-renew-{sandbox.id}",
            )
            self._renew_tasks.add(task)
            task.add_done_callback(self._renew_tasks.discard)
        except SandboxException:
            logger.debug(
                "No ready warm sandbox for template %s; using direct creation",
                template,
                exc_info=True,
            )
            return None
        else:
            return sandbox

    async def _renew_acquired(self, sandbox: OpenSandbox) -> None:
        try:
            await sandbox.renew(
                timedelta(
                    seconds=self.settings.warm_pool_acquired_timeout_seconds
                )
            )
        except SandboxException:
            logger.warning(
                "Could not extend acquired warm sandbox lease %s",
                sandbox.id,
                exc_info=True,
            )

    def reservation(self) -> WarmPoolReservation:
        if not self._started:
            return WarmPoolReservation()
        memory_mb = 0
        cpu = 0.0
        sandboxes = 0
        # Reserve the configured maximum even when the adaptive pool has scaled
        # to zero. This lets it refill without racing active-job admission above
        # the host's aggregate budget.
        for template, target in self._configured_targets.items():
            template_memory, template_cpu = self.settings.resources_for_template(
                template
            )
            memory_mb += target * template_memory
            cpu += target * template_cpu
            sandboxes += target
        return WarmPoolReservation(
            memory_mb=memory_mb,
            cpu=cpu,
            sandboxes=sandboxes,
        )

    async def _maintenance_loop(self) -> None:
        interval = max(1.0, self.settings.warm_pool_reconcile_seconds)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                pass
            else:
                return
            await self._scale_down_inactive()

    async def _scale_down_inactive(self) -> None:
        threshold = self.settings.warm_pool_release_after_inactivity_seconds
        if threshold == 0:
            return
        now = asyncio.get_running_loop().time()
        for template, pool in self._pools.items():
            if self._active_targets.get(template, 0) == 0:
                continue
            if now - self._last_demand[template] < threshold:
                continue
            await pool.resize(0)
            self._active_targets[template] = 0
            logger.info("Scaled inactive warm pool %s to zero", template)

    def _pool_name(self, template: str) -> str:
        return f"harborbox-{template}-{self.settings.template_version}"
