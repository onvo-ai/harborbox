from harborbox.admission import Capacity, can_admit, reserve_memory


def capacity(**overrides: object) -> Capacity:
    values: dict[str, object] = {
        "total_memory_mb": 16_384,
        "reserve_memory_mb": 4_096,
        "platform_memory_reserve_mb": 768,
        "reserved_memory_mb": 6_144,
        "host_available_memory_mb": 8_000,
        "reserved_cpu": 3.0,
        "max_parallel_cpu": 8.0,
        "configured_sandbox_budget_mb": None,
    }
    values.update(overrides)
    return Capacity(**values)  # type: ignore[arg-type]


def test_reserve_uses_larger_of_percentage_and_floor() -> None:
    floor_mb = 2_048
    percentage_result_mb = 4_000  # 25% of 16_000
    assert reserve_memory(16_000, 25, floor_mb) == percentage_result_mb
    assert reserve_memory(4_000, 25, floor_mb) == floor_mb


def test_admits_parallel_job_when_reservation_fits() -> None:
    decision = can_admit(
        capacity(),
        incremental_memory_mb=1_024,
        incremental_cpu=1.0,
        emergency_available_memory_mb=1_024,
    )
    assert decision.admitted


def test_rejects_job_that_crosses_reserved_memory_budget() -> None:
    decision = can_admit(
        capacity(reserved_memory_mb=10_000),
        incremental_memory_mb=2_000,
        incremental_cpu=1.0,
        emergency_available_memory_mb=1_024,
    )
    assert not decision.admitted
    assert decision.waiting_for == "memory"


def test_configured_budget_is_an_absolute_ceiling_without_preallocation() -> None:
    budget_mb = 4_096
    empty = capacity(
        reserved_memory_mb=0,
        configured_sandbox_budget_mb=budget_mb,
    )
    assert empty.sandbox_budget_mb == budget_mb
    assert empty.available_reservation_mb == budget_mb
    assert empty.reserved_memory_mb == 0

    full = capacity(
        reserved_memory_mb=3_584,
        configured_sandbox_budget_mb=budget_mb,
    )
    decision = can_admit(
        full,
        incremental_memory_mb=1_024,
        incremental_cpu=1.0,
        emergency_available_memory_mb=1_024,
    )
    assert not decision.admitted
    assert decision.waiting_for == "memory"


def test_configured_budget_never_overrides_safer_host_budget() -> None:
    expected_sandbox_budget_mb = 2_560
    limited_host = capacity(
        total_memory_mb=4_096,
        reserve_memory_mb=1_024,
        platform_memory_reserve_mb=512,
        configured_sandbox_budget_mb=4_096,
    )
    assert limited_host.sandbox_budget_mb == expected_sandbox_budget_mb


def test_rejects_job_when_live_available_memory_hits_emergency_reserve() -> None:
    decision = can_admit(
        capacity(host_available_memory_mb=1_500),
        incremental_memory_mb=1_024,
        incremental_cpu=1.0,
        emergency_available_memory_mb=1_024,
    )
    assert not decision.admitted
    assert decision.waiting_for == "memory"


def test_rejects_job_when_cpu_reservation_is_full() -> None:
    decision = can_admit(
        capacity(reserved_cpu=7.5),
        incremental_memory_mb=128,
        incremental_cpu=1.0,
        emergency_available_memory_mb=1_024,
    )
    assert not decision.admitted
    assert decision.waiting_for == "cpu"
