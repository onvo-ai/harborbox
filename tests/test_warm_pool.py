from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from opensandbox.config import ConnectionConfig

from harborbox.config import Settings
from harborbox.warm_pool import OpenSandboxWarmPools

if TYPE_CHECKING:
    from datetime import timedelta

    from opensandbox.pool_types import AcquirePolicy


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
