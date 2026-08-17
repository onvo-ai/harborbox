"""The warm pool must leave room for the largest template to actually run.

A configuration where the warm pool *fits* but leaves less headroom than the
biggest template needs is not a tight fit — it is a deadlock. Every request for
that template queues on `waiting_for: cpu` and never clears, because the thing
holding the budget is an idle pool that never yields it.

This is not hypothetical. Onvo Lite hit it on 2026-08-10:

    warm pool  onvo-pro 1x2.0 + relaydeck 2x0.5 = 3.0 reserved
    HARBORBOX_MAX_PARALLEL_CPU                  = 4.0
    onvo-lite needs                             = 2.0   -> never admitted

Those three templates no longer exist -- products bring their own images now --
but the deadlock does. A pool sized against a ceiling that leaves less headroom
than one sandbox needs still starves every request for it, and "one sandbox"
is now bounded by `max_sandbox_cpu` rather than by any registered template.

The old validation passed that config: 3.0 <= 4.0. It took hours to find,
because the symptom is silence — a sandbox stuck in `created` with no error.
"""

import pytest

from harborbox.config import Settings


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "api_keys": "test-key",
        # Three 1.0 CPU base slots = 3.0 reserved, matching the incident.
        "base_template_cpu": 1.0,
        "warm_pool": {"base": 3},
        # The largest thing that can ask for admission.
        "max_sandbox_cpu": 2.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestCpuHeadroom:
    def test_rejects_a_pool_that_starves_the_largest_template(self) -> None:
        """The shape of the Onvo Lite config. Fits, but deadlocks."""
        with pytest.raises(ValueError, match="headroom"):
            make_settings(max_parallel_cpu=4.0)

    def test_accepts_a_pool_that_leaves_room(self) -> None:
        # 3.0 reserved + 2.0 largest sandbox = 5.0 <= 6.0.
        max_parallel_cpu = 6.0
        settings = make_settings(max_parallel_cpu=max_parallel_cpu)
        assert settings.max_parallel_cpu == max_parallel_cpu

    def test_boundary_is_inclusive(self) -> None:
        """Exactly enough is enough — 3.0 + 2.0 == 5.0 must pass."""
        max_parallel_cpu = 5.0
        settings = make_settings(max_parallel_cpu=max_parallel_cpu)
        assert settings.max_parallel_cpu == max_parallel_cpu

    def test_still_rejects_a_pool_larger_than_the_whole_budget(self) -> None:
        """The original check must keep working."""
        with pytest.raises(ValueError, match="aggregate sandbox CPU budget"):
            make_settings(max_parallel_cpu=2.0)

    def test_no_ceiling_configured_is_left_alone(self) -> None:
        """`None` means 'derive from the host', which is checked at runtime."""
        settings = make_settings(max_parallel_cpu=None)
        assert settings.max_parallel_cpu is None

    def test_a_pool_of_zero_needs_only_the_largest_sandbox(self) -> None:
        max_parallel_cpu = 2.0
        settings = make_settings(warm_pool={}, max_parallel_cpu=max_parallel_cpu)
        assert settings.max_parallel_cpu == max_parallel_cpu
