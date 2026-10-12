from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, ClassVar

import pytest
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxException

from harborbox.config import Settings
from harborbox.runtime_protocol import WarmPoolReservation
from harborbox.warm_pool import OpenSandboxWarmPools

if TYPE_CHECKING:
    from datetime import timedelta

    from opensandbox.pool_types import AcquirePolicy


_POOL_START_FAILURE = "pool state store unreachable"
_NO_READY_SANDBOX = "no ready sandbox"
_LEASE_GONE = "lease gone"
_CONFIGURED_TARGET = 2


class FakePool:
    def __init__(self, sandbox: FakeWarmHandle) -> None:
        self.sandbox = sandbox
        self.acquired = 0
        self.resized: list[int] = []

    # OpenSandboxWarmPools.acquire only ever calls this with `sandbox_timeout`
    # and `policy`, so the value type is the real union rather than Any.
    async def acquire(
        self, **kwargs: timedelta | AcquirePolicy | None
    ) -> FakeWarmHandle:
        assert kwargs["sandbox_timeout"] is None
        self.acquired += 1
        return self.sandbox

    async def resize(self, target: int) -> None:
        self.resized.append(target)


class FakeWarmHandle:
    id = "warm-test"

    def __init__(self) -> None:
        self.renewed = False

    async def renew(self, _timeout: timedelta) -> None:
        self.renewed = True


class RecordingPool:
    """A SandboxPoolAsync stand-in that records the lifecycle calls made on it.

    `start()` is the one call `OpenSandboxWarmPools.start` awaits before it
    declares itself started, so `start_error` is how a test drives the
    fall-back-to-cold-start path without a real pool backend.
    """

    instances: ClassVar[list[RecordingPool]] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.started = False
        self.released_idle = False
        self.shutdowns: list[bool] = []
        self.resized: list[int] = []
        self.start_error: Exception | None = None
        RecordingPool.instances.append(self)

    async def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    async def release_all_idle(self) -> None:
        self.released_idle = True

    async def shutdown(self, *, graceful: bool) -> None:
        self.shutdowns.append(graceful)

    async def resize(self, target: int) -> None:
        self.resized.append(target)


@pytest.fixture
def recording_pools(monkeypatch: pytest.MonkeyPatch) -> type[RecordingPool]:
    """Patch out the two things `start()` reaches for outside this module."""
    RecordingPool.instances = []
    monkeypatch.setattr("harborbox.warm_pool.SandboxPoolAsync", RecordingPool)
    monkeypatch.setattr(
        "harborbox.warm_pool.AsyncPostgresPoolStateStore",
        lambda _factory: object(),
    )
    return RecordingPool


@pytest.mark.asyncio
async def test_pool_only_claims_an_exact_template_resource_profile() -> None:
    settings = Settings(warm_pool_release_after_inactivity_seconds=10)
    pools = OpenSandboxWarmPools(settings, ConnectionConfig())
    handle = FakeWarmHandle()
    fake = FakePool(handle)
    pools._pools["base"] = fake  # type: ignore[assignment]
    pools._active_targets["base"] = 1
    pools._last_demand["base"] = asyncio.get_running_loop().time()
    pools._started = True

    acquired = await pools.acquire(template="base", memory_mb=512, cpu=1.0)
    mismatched = await pools.acquire(
        template="base", memory_mb=1024, cpu=1.0
    )

    assert acquired is handle
    assert mismatched is None
    assert fake.acquired == 1


@pytest.mark.asyncio
async def test_inactive_pool_scales_to_zero_and_refills_on_demand() -> None:
    settings = Settings(warm_pool_release_after_inactivity_seconds=1)
    pools = OpenSandboxWarmPools(settings, ConnectionConfig())
    handle = FakeWarmHandle()
    fake = FakePool(handle)
    pools._pools["base"] = fake  # type: ignore[assignment]
    pools._active_targets["base"] = 1
    pools._last_demand["base"] = asyncio.get_running_loop().time() - 2
    pools._started = True

    await pools._scale_down_inactive()
    acquired = await pools.acquire(template="base", memory_mb=512, cpu=1.0)

    assert fake.resized == [0, 1]
    assert acquired is handle


def test_configured_pool_is_included_in_admission_reservation() -> None:
    pools = OpenSandboxWarmPools(Settings(), ConnectionConfig())
    pools._started = True

    reservation = pools.reservation()

    # One base slot at the default 512 MiB / 1.0 CPU. This used to be three
    # pools across three product templates; products bring their own images
    # now and name them in HARBORBOX_WARM_POOL if they want one pooled.
    expected_memory_mb = 512
    expected_cpu = 1.0
    expected_sandboxes = 1
    assert reservation.memory_mb == expected_memory_mb
    assert reservation.cpu == expected_cpu
    assert reservation.sandboxes == expected_sandboxes


def test_nothing_is_reserved_before_the_pools_start() -> None:
    # Admission asks for this on every request, including during the window
    # between process start and `start()` finishing. Reserving the configured
    # maximum then would refuse real work for pools that do not exist yet.
    pools = OpenSandboxWarmPools(Settings(), ConnectionConfig())

    assert pools.reservation() == WarmPoolReservation()


@pytest.mark.asyncio
async def test_start_is_a_no_op_when_warm_pools_are_disabled(
    recording_pools: type[RecordingPool],
) -> None:
    pools = OpenSandboxWarmPools(Settings(warm_pool_enabled=False), ConnectionConfig())

    await pools.start()

    assert recording_pools.instances == []
    assert pools._pools == {}
    assert pools._started is False


@pytest.mark.asyncio
async def test_start_is_a_no_op_when_no_template_is_pooled(
    recording_pools: type[RecordingPool],
) -> None:
    pools = OpenSandboxWarmPools(Settings(warm_pool={}), ConnectionConfig())

    await pools.start()

    assert recording_pools.instances == []
    assert pools._started is False


@pytest.mark.asyncio
async def test_start_builds_one_pool_per_configured_template(
    recording_pools: type[RecordingPool],
) -> None:
    settings = Settings(
        warm_pool={"base": _CONFIGURED_TARGET}, template_version="v9"
    )
    pools = OpenSandboxWarmPools(settings, ConnectionConfig())

    await pools.start()

    try:
        assert len(recording_pools.instances) == 1
        pool = recording_pools.instances[0]
        assert pool.started is True
        # The pool name carries the template version, so a version bump leaves
        # the previous generation's warm sandboxes to expire on their own
        # rather than being handed to a job built against new images.
        assert pool.kwargs["pool_name"] == "harborbox-base-v9"
        assert pool.kwargs["max_idle"] == _CONFIGURED_TARGET
        assert pools._pools["base"] is pool
        assert pools._active_targets["base"] == _CONFIGURED_TARGET
        assert pools._started is True
        assert pools._maintenance_task is not None
    finally:
        await pools.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("recording_pools")
async def test_start_falls_back_to_cold_starts_when_a_pool_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A warm pool is an optimisation. If its backend is unavailable the service
    # still has to serve sandboxes, so `start()` swallows the failure, tears
    # down whatever it half-built, and leaves `_pools` empty for `acquire()` to
    # read as "cold start everything".
    failing = RecordingPool()
    failing.start_error = SandboxException(_POOL_START_FAILURE)
    monkeypatch.setattr(
        "harborbox.warm_pool.SandboxPoolAsync", lambda **_kwargs: failing
    )
    pools = OpenSandboxWarmPools(Settings(warm_pool={"base": 1}), ConnectionConfig())

    await pools.start()

    assert pools._pools == {}
    assert pools._active_targets == {}
    assert pools._started is False
    assert pools._maintenance_task is None
    assert failing.shutdowns == [False]
    assert await pools.acquire(template="base", memory_mb=512, cpu=1.0) is None


@pytest.mark.asyncio
async def test_close_cancels_maintenance_and_shuts_every_pool_down(
    recording_pools: type[RecordingPool],
) -> None:
    pools = OpenSandboxWarmPools(Settings(warm_pool={"base": 1}), ConnectionConfig())
    await pools.start()
    maintenance = pools._maintenance_task
    renewed = asyncio.Event()

    async def _renew() -> None:
        renewed.set()

    pools._renew_tasks.add(asyncio.create_task(_renew()))

    await pools.close()

    pool = recording_pools.instances[0]
    assert maintenance is not None
    assert maintenance.cancelled()
    assert renewed.is_set()
    assert pool.released_idle is True
    assert pool.shutdowns == [True]
    assert pools._started is False


@pytest.mark.asyncio
async def test_close_leaves_idle_sandboxes_alone_when_asked_to(
    recording_pools: type[RecordingPool],
) -> None:
    # Restarting the process without releasing is what keeps a rolling deploy
    # from throwing away a pool it is about to want back.
    settings = Settings(warm_pool={"base": 1}, warm_pool_release_on_shutdown=False)
    pools = OpenSandboxWarmPools(settings, ConnectionConfig())
    await pools.start()

    await pools.close()

    pool = recording_pools.instances[0]
    assert pool.released_idle is False
    assert pool.shutdowns == [True]


@pytest.mark.asyncio
async def test_acquire_without_a_template_cold_starts() -> None:
    pools = OpenSandboxWarmPools(Settings(), ConnectionConfig())

    assert await pools.acquire(template=None, memory_mb=512, cpu=1.0) is None


@pytest.mark.asyncio
async def test_acquire_refills_a_pool_that_had_scaled_to_zero() -> None:
    pools = OpenSandboxWarmPools(Settings(), ConnectionConfig())
    fake = FakePool(FakeWarmHandle())
    pools._pools["base"] = fake  # type: ignore[assignment]
    pools._active_targets["base"] = 0
    pools._last_demand["base"] = asyncio.get_running_loop().time()

    await pools.acquire(template="base", memory_mb=512, cpu=1.0)

    assert fake.resized == [1]
    assert pools._active_targets["base"] == 1


@pytest.mark.asyncio
async def test_acquire_cold_starts_when_no_warm_sandbox_is_ready() -> None:
    # FAIL_FAST is deliberate: waiting for a warm sandbox to become ready is
    # strictly slower than creating one directly, so an empty pool must fall
    # through rather than queue.
    class EmptyPool(FakePool):
        async def acquire(
            self, **_kwargs: timedelta | AcquirePolicy | None
        ) -> FakeWarmHandle:
            raise SandboxException(_NO_READY_SANDBOX)

    pools = OpenSandboxWarmPools(Settings(), ConnectionConfig())
    pools._pools["base"] = EmptyPool(FakeWarmHandle())  # type: ignore[assignment]
    pools._active_targets["base"] = 1
    pools._last_demand["base"] = asyncio.get_running_loop().time()

    acquired = await pools.acquire(template="base", memory_mb=512, cpu=1.0)

    assert acquired is None
    assert pools._renew_tasks == set()


@pytest.mark.asyncio
async def test_acquired_sandbox_gets_its_lease_extended() -> None:
    pools = OpenSandboxWarmPools(Settings(), ConnectionConfig())
    handle = FakeWarmHandle()

    await pools._renew_acquired(handle)  # type: ignore[arg-type]

    assert handle.renewed is True


@pytest.mark.asyncio
async def test_a_failed_lease_extension_does_not_lose_the_sandbox(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The sandbox is already handed to the caller by this point. Raising here
    # would surface a warm-pool bookkeeping failure as a failed user request;
    # the worst case of swallowing it is the lease expiring on its own.
    class UnrenewableHandle(FakeWarmHandle):
        async def renew(self, _timeout: timedelta) -> None:
            raise SandboxException(_LEASE_GONE)

    pools = OpenSandboxWarmPools(Settings(), ConnectionConfig())

    with caplog.at_level(logging.WARNING, logger="harborbox.warm_pool"):
        await pools._renew_acquired(UnrenewableHandle())  # type: ignore[arg-type]

    assert "Could not extend acquired warm sandbox lease" in caplog.text


@pytest.mark.asyncio
async def test_maintenance_loop_scales_down_once_per_interval() -> None:
    settings = Settings(warm_pool_reconcile_seconds=0.01)
    pools = OpenSandboxWarmPools(settings, ConnectionConfig())
    passes = 0

    async def _record_pass() -> None:
        nonlocal passes
        passes += 1
        pools._stop.set()

    pools._scale_down_inactive = _record_pass  # type: ignore[method-assign]

    await pools._maintenance_loop()

    assert passes == 1


@pytest.mark.asyncio
async def test_maintenance_loop_exits_without_a_scale_down_when_stopped() -> None:
    # `close()` sets the event and then cancels, so the loop has to treat a set
    # event as "return now" rather than running one more reconcile pass against
    # pools that are already being shut down.
    pools = OpenSandboxWarmPools(Settings(), ConnectionConfig())

    async def _fail() -> None:
        msg = "scale-down ran during shutdown"
        raise AssertionError(msg)

    pools._scale_down_inactive = _fail  # type: ignore[method-assign]
    loop_task = asyncio.create_task(pools._maintenance_loop())
    await asyncio.sleep(0)
    pools._stop.set()

    await loop_task


@pytest.mark.asyncio
async def test_scale_down_is_off_when_the_inactivity_threshold_is_zero() -> None:
    settings = Settings(warm_pool_release_after_inactivity_seconds=0)
    pools = OpenSandboxWarmPools(settings, ConnectionConfig())
    fake = FakePool(FakeWarmHandle())
    pools._pools["base"] = fake  # type: ignore[assignment]
    pools._active_targets["base"] = 1
    pools._last_demand["base"] = asyncio.get_running_loop().time() - 10_000

    await pools._scale_down_inactive()

    assert fake.resized == []


@pytest.mark.asyncio
async def test_scale_down_skips_pools_that_are_already_at_zero() -> None:
    settings = Settings(warm_pool_release_after_inactivity_seconds=1)
    pools = OpenSandboxWarmPools(settings, ConnectionConfig())
    fake = FakePool(FakeWarmHandle())
    pools._pools["base"] = fake  # type: ignore[assignment]
    pools._active_targets["base"] = 0
    pools._last_demand["base"] = asyncio.get_running_loop().time() - 10_000

    await pools._scale_down_inactive()

    assert fake.resized == []


@pytest.mark.asyncio
async def test_scale_down_leaves_a_recently_used_pool_alone() -> None:
    settings = Settings(warm_pool_release_after_inactivity_seconds=300)
    pools = OpenSandboxWarmPools(settings, ConnectionConfig())
    fake = FakePool(FakeWarmHandle())
    pools._pools["base"] = fake  # type: ignore[assignment]
    pools._active_targets["base"] = 1
    pools._last_demand["base"] = asyncio.get_running_loop().time()

    await pools._scale_down_inactive()

    assert fake.resized == []
    assert pools._active_targets["base"] == 1
