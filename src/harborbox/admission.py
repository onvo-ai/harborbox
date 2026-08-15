from dataclasses import dataclass


@dataclass(frozen=True)
class Capacity:
    total_memory_mb: int
    reserve_memory_mb: int
    platform_memory_reserve_mb: int
    reserved_memory_mb: int
    host_available_memory_mb: int
    reserved_cpu: float
    max_parallel_cpu: float
    configured_sandbox_budget_mb: int | None = None
    warm_pool_reserved_memory_mb: int = 0
    warm_pool_reserved_cpu: float = 0.0
    warm_pool_target_sandboxes: int = 0

    @property
    def sandbox_budget_mb(self) -> int:
        host_budget = max(
            0,
            self.total_memory_mb
            - self.reserve_memory_mb
            - self.platform_memory_reserve_mb,
        )
        if self.configured_sandbox_budget_mb is None:
            return host_budget
        return min(host_budget, self.configured_sandbox_budget_mb)

    @property
    def available_reservation_mb(self) -> int:
        return max(0, self.sandbox_budget_mb - self.reserved_memory_mb)


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    waiting_for: str | None = None


def reserve_memory(total_memory_mb: int, percent: int, minimum_mb: int) -> int:
    return max(minimum_mb, total_memory_mb * percent // 100)


def can_admit(
    capacity: Capacity,
    *,
    incremental_memory_mb: int,
    incremental_cpu: float,
    emergency_available_memory_mb: int,
) -> AdmissionDecision:
    if (
        capacity.reserved_memory_mb + incremental_memory_mb
        > capacity.sandbox_budget_mb
    ):
        return AdmissionDecision(admitted=False, waiting_for="memory")

    if (
        capacity.host_available_memory_mb - incremental_memory_mb
        < emergency_available_memory_mb
    ):
        return AdmissionDecision(admitted=False, waiting_for="memory")

    if capacity.reserved_cpu + incremental_cpu > capacity.max_parallel_cpu:
        return AdmissionDecision(admitted=False, waiting_for="cpu")

    return AdmissionDecision(admitted=True)
