"""Settings behaviour that only `config.py` decides.

`config.py` is the second most-changed module in this repo — 21 commits in the
last 90 days, 3 of them fixes — and until now it had no test file of its own.
What coverage it had was incidental: other suites construct `Settings` to get at
something else, so the happy paths were exercised and every refusal was not.

The gaps that left are the ones that matter. A `Settings` field is a deployment
knob, and the three behaviours covered here are each the difference between a
misconfiguration that is refused at startup and one that is discovered in
production:

- `docker_kwargs` decides whether the Docker client talks to a socket or to the
  address someone configured;
- `warm_pool_sizes` is what disables the pool, and a disabled pool that still
  reports its configured sizes would reserve memory for sandboxes that do not
  exist;
- `resources_for_template` and `validate_warm_pool_budget` are both refusals.
  An unraised refusal is silent by construction, which is why they were the
  uncovered lines rather than the exercised ones.
"""

import pytest
from pydantic import ValidationError

from harborbox.config import BASE_TEMPLATE, Settings

_BASE_MEMORY_MB = 512
_BASE_CPU = 1.0
_OVERSIZED_MEMORY_MB = 8_192
_OVERSIZED_CPU = 8.0
_MAX_MEMORY_MB = 4_096
_MAX_CPU = 4.0
_POOLED_SANDBOXES = 3
_DOCKER_ADDRESS = "tcp://docker-host:2375"


def make_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


class TestDockerKwargs:
    """`docker_kwargs` is spread straight into the Docker client constructor."""

    def test_an_explicit_address_is_passed_to_the_docker_client(self) -> None:
        settings = make_settings(docker_base_url=_DOCKER_ADDRESS)

        assert settings.docker_kwargs == {"base_url": _DOCKER_ADDRESS}

    def test_no_address_hands_the_client_nothing_to_override_its_default(self) -> None:
        # An empty mapping and `{"base_url": None}` are not the same thing: the
        # second overrides the Docker client's own socket discovery with None
        # and fails to connect to anything.
        settings = make_settings(docker_base_url=None)

        assert settings.docker_kwargs == {}


class TestWarmPoolSizes:
    def test_configured_sizes_are_reported_when_the_pool_is_enabled(self) -> None:
        settings = make_settings(
            warm_pool_enabled=True, warm_pool={BASE_TEMPLATE: _POOLED_SANDBOXES}
        )

        assert settings.warm_pool_sizes == {BASE_TEMPLATE: _POOLED_SANDBOXES}

    def test_disabling_the_pool_zeroes_every_configured_template(self) -> None:
        # Zeroed rather than dropped: admission sums these to work out what the
        # pool is holding, so a template that vanishes from the mapping and one
        # that reports zero have to mean the same thing. Dropping the keys
        # would also lose which templates are configured, which is what a
        # re-enable reads.
        settings = make_settings(
            warm_pool_enabled=False, warm_pool={BASE_TEMPLATE: _POOLED_SANDBOXES}
        )

        assert settings.warm_pool_sizes == {BASE_TEMPLATE: 0}


class TestResourcesForTemplate:
    def test_the_registered_base_template_reports_its_configured_sizing(self) -> None:
        settings = make_settings(
            base_template_memory_mb=_BASE_MEMORY_MB, base_template_cpu=_BASE_CPU
        )

        assert settings.resources_for_template(BASE_TEMPLATE) == (
            _BASE_MEMORY_MB,
            _BASE_CPU,
        )

    def test_a_missing_template_name_is_refused_rather_than_defaulted(self) -> None:
        # Callers reach here with whatever the request carried. Falling back to
        # the deployment default for a request that named no template at all
        # would size a sandbox nobody asked for, so this raises instead.
        settings = make_settings()

        with pytest.raises(KeyError, match="a registered sandbox template is required"):
            settings.resources_for_template(None)

    def test_an_unregistered_name_that_is_not_a_custom_image_is_refused(self) -> None:
        # `custom-<12 hex>` names fall through to the deployment default,
        # because their real sizing lives on the database row. A name that is
        # neither the base nor well-formed as a custom image has no sizing
        # anywhere and must not borrow the default.
        settings = make_settings()

        with pytest.raises(KeyError, match="not-a-real-template"):
            settings.resources_for_template("not-a-real-template")


class TestWarmPoolBudgetValidation:
    """A template larger than the per-sandbox ceiling can never be admitted.

    Refused at construction, because the failure is otherwise silent: the API
    starts, the pool fills, and every request for that template queues forever
    against a ceiling it cannot fit under.
    """

    def test_a_template_wider_than_the_memory_ceiling_is_refused_at_startup(
        self,
    ) -> None:
        with pytest.raises(
            ValidationError, match="template memory exceeds max sandbox memory"
        ):
            make_settings(
                base_template_memory_mb=_OVERSIZED_MEMORY_MB,
                max_sandbox_memory_mb=_MAX_MEMORY_MB,
            )

    def test_a_template_wider_than_the_cpu_ceiling_is_refused_at_startup(self) -> None:
        with pytest.raises(
            ValidationError, match="template CPU exceeds max sandbox CPU"
        ):
            make_settings(
                base_template_cpu=_OVERSIZED_CPU,
                max_sandbox_cpu=_MAX_CPU,
                # Keep the pool out of it: a 8.0-CPU template in a pool of one
                # trips the headroom check further down the same validator, and
                # this test is about the per-template ceiling above it.
                warm_pool={},
            )

    def test_a_template_inside_both_ceilings_constructs(self) -> None:
        settings = make_settings(
            base_template_memory_mb=_BASE_MEMORY_MB,
            base_template_cpu=_BASE_CPU,
            max_sandbox_memory_mb=_MAX_MEMORY_MB,
            max_sandbox_cpu=_MAX_CPU,
        )

        assert settings.template_resources == {
            BASE_TEMPLATE: (_BASE_MEMORY_MB, _BASE_CPU)
        }
