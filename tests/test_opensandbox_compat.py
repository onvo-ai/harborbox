"""The OpenSandbox wire contract: what Harborbox accepts, refuses, and echoes back.

This module is the whole of the translation between the OpenSandbox API shape
that clients speak and the `Sandbox` rows Harborbox keeps. Two kinds of bug
live here and neither shows up as an exception in production:

  * A refusal that silently becomes an acceptance. `OpenSandboxCreate` rejects
    the OpenSandbox features the Docker provider cannot honour -- snapshot
    restore, persistent `env`, custom volumes, `networkPolicy`, `secureAccess`.
    Dropping one of those guards does not fail; it accepts a sandbox that then
    quietly ignores what the caller asked for, which for `networkPolicy` and
    `secureAccess` means running with less isolation than the caller believes
    they bought.
  * A round trip that stops round-tripping. Everything Harborbox needs but the
    `sandboxes` table has no column for -- entrypoint, extensions, platform,
    expiry -- is JSON stuffed into the metadata blob under a reserved prefix
    and read back out on the way to a response. The write and read halves are
    two functions that have to agree; tested apart, they can drift and still
    each look right.

So the tests below pair the halves (`create_metadata` -> `expiration`,
`create_metadata` -> `response_for`) rather than asserting on the stored
strings, and state each refusal as its own behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from harborbox.config import Settings
from harborbox.models import Sandbox
from harborbox.opensandbox_compat import (
    INTERNAL_PREFIX,
    OpenSandboxCreate,
    create_metadata,
    expiration,
    lifecycle_status,
    parse_cpu,
    parse_memory_mb,
    public_metadata,
    response_for,
    template_for,
)

IMAGE_URI = "harborbox-sandbox-base:local"
CUSTOM_TEMPLATE = "custom-0123456789ab"
CREATED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
TIMEOUT_SECONDS = 900


def create_body(**overrides: object) -> OpenSandboxCreate:
    """Build the minimal create request that passes validation."""
    values: dict[str, object] = {"image": {"uri": IMAGE_URI}}
    values.update(overrides)
    return OpenSandboxCreate.model_validate(values)


def sandbox_row(**overrides: object) -> Sandbox:
    """Build a running sandbox row, the shape `response_for` renders."""
    values: dict[str, object] = {
        "id": "sbx-1",
        "status": "running",
        "container_id": "c1",
        "container_name": "n1",
        "agent_token": "test-token",
        "memory_mb": 512,
        "cpu": 1.0,
        "pids_limit": 128,
        "idle_timeout_seconds": 60,
        "metadata_": {"template": "base"},
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
        "last_activity_at": CREATED_AT,
    }
    values.update(overrides)
    return Sandbox(**values)


# --- what the create contract accepts -------------------------------------


def test_create_accepts_a_camel_case_body_and_keeps_its_aliases() -> None:
    body = OpenSandboxCreate.model_validate(
        {
            "image": {"uri": IMAGE_URI},
            "resourceLimits": {"memory": "512Mi", "cpu": "500m"},
            "metadata": {"owner": "onvo-pro"},
            "entrypoint": ["python", "-u"],
            "timeout": TIMEOUT_SECONDS,
        }
    )
    assert body.resource_limits == {"memory": "512Mi", "cpu": "500m"}
    assert body.entrypoint == ["python", "-u"]
    assert body.timeout == TIMEOUT_SECONDS


def test_create_accepts_a_template_ref_without_an_image() -> None:
    body = OpenSandboxCreate.model_validate({"extensions": {"templateRef": CUSTOM_TEMPLATE}})
    assert body.image is None
    assert body.extensions["templateRef"] == CUSTOM_TEMPLATE


def test_create_requires_an_image_or_a_template_ref() -> None:
    with pytest.raises(ValidationError, match=r"image or extensions\.templateRef is required"):
        OpenSandboxCreate.model_validate({})


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ({"snapshotId": "snap-1"}, "snapshot restore is not supported"),
        ({"env": {"TOKEN": "secret"}}, "pass secrets per execution"),
        ({"volumes": [{"name": "data"}]}, "custom volumes are not supported"),
        ({"networkPolicy": {"egress": "deny"}}, "networkPolicy requires an OpenSandbox"),
        ({"secureAccess": True}, "secureAccess requires an OpenSandbox"),
    ],
)
def test_create_refuses_features_the_docker_provider_cannot_honour(
    field: dict[str, object], message: str
) -> None:
    """Silently accepting any of these would run a sandbox that ignores the request."""
    with pytest.raises(ValidationError, match=message):
        OpenSandboxCreate.model_validate({"image": {"uri": IMAGE_URI}, **field})


# --- resource limit parsing ------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected_mb"),
    [
        ("512Mi", 512),
        ("512M", 512),
        ("2Gi", 2048),
        ("2G", 2048),
        ("1Ti", 1024 * 1024),
        ("2097152Ki", 2048),
        ("536870912", 512),  # bare digits are bytes
        ("1.5Gi", 1536),
        ("  1Gi  ", 1024),
        ("1gi", 1024),
    ],
)
def test_parse_memory_mb_converts_kubernetes_quantities(value: str, expected_mb: int) -> None:
    assert parse_memory_mb(value) == expected_mb


def test_parse_memory_mb_rounds_up_so_a_request_is_never_shrunk() -> None:
    rounded_up_mb = 2
    assert parse_memory_mb("1.1Mi") == rounded_up_mb


def test_parse_memory_mb_floors_a_sub_megabyte_request_at_one() -> None:
    """A rounding result of 0 would ask Docker for an unlimited sandbox."""
    floor_mb = 1
    assert parse_memory_mb("1024") == floor_mb
    assert parse_memory_mb("0") == floor_mb


@pytest.mark.parametrize("value", ["", "lots", "512MB", "-512Mi", "512 Mi B"])
def test_parse_memory_mb_rejects_a_quantity_it_cannot_read(value: str) -> None:
    with pytest.raises(ValueError, match="invalid memory resource limit"):
        parse_memory_mb(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("500m", 0.5), ("1500m", 1.5), ("2", 2.0), ("0.5", 0.5), ("  1 ", 1.0), ("500M", 0.5)],
)
def test_parse_cpu_reads_millicores_and_whole_cores(value: str, expected: float) -> None:
    assert parse_cpu(value) == expected


# --- choosing the template -------------------------------------------------


def test_template_for_prefers_an_explicit_template_ref() -> None:
    settings = Settings()
    body = create_body(extensions={"templateRef": CUSTOM_TEMPLATE})
    assert template_for(body, settings) == CUSTOM_TEMPLATE


def test_template_for_accepts_the_shorter_template_extension_key() -> None:
    settings = Settings()
    body = create_body(extensions={"template": "base"})
    assert template_for(body, settings) == "base"


def test_template_for_rejects_a_malformed_template_name() -> None:
    """Well-formedness only -- whether the template exists is the registry's call."""
    settings = Settings()
    body = create_body(extensions={"templateRef": "../etc/passwd"})
    with pytest.raises(ValueError, match="unknown sandbox template"):
        template_for(body, settings)


def test_template_for_maps_a_known_image_back_to_its_template() -> None:
    settings = Settings()
    body = create_body(image={"uri": settings.base_image})
    assert template_for(body, settings) == "base"


def test_template_for_rejects_an_image_this_deployment_does_not_ship() -> None:
    settings = Settings()
    body = create_body(image={"uri": "docker.io/library/python:3.12"})
    with pytest.raises(ValueError, match="registered Harborbox templateRef"):
        template_for(body, settings)


def test_template_for_rejects_a_body_with_neither_image_nor_extension() -> None:
    """Reachable through the warm-pool path, which builds bodies without validation."""
    body = OpenSandboxCreate.model_construct(image=None, extensions={})
    with pytest.raises(ValueError, match="registered Harborbox templateRef"):
        template_for(body, Settings())


# --- metadata: the write half ----------------------------------------------


def test_create_metadata_keeps_caller_metadata_alongside_the_template() -> None:
    metadata = create_metadata(
        create_body(metadata={"owner": "onvo-pro"}), template="base", now=CREATED_AT
    )
    assert metadata["owner"] == "onvo-pro"
    assert metadata["template"] == "base"


def test_create_metadata_refuses_to_let_a_caller_forge_internal_keys() -> None:
    """The reserved prefix is what tells internal state apart from caller data."""
    body = create_body(metadata={f"{INTERNAL_PREFIX}expires_at": "2099-01-01T00:00:00Z"})
    with pytest.raises(ValueError, match="are reserved"):
        create_metadata(body, template="base", now=CREATED_AT)


def test_create_metadata_records_an_expiry_only_when_a_timeout_was_asked_for() -> None:
    with_timeout = create_metadata(
        create_body(timeout=TIMEOUT_SECONDS), template="base", now=CREATED_AT
    )
    without = create_metadata(create_body(), template="base", now=CREATED_AT)
    assert with_timeout[f"{INTERNAL_PREFIX}expires_at"] == (
        CREATED_AT + timedelta(seconds=TIMEOUT_SECONDS)
    ).isoformat()
    assert f"{INTERNAL_PREFIX}expires_at" not in without


def test_create_metadata_defaults_its_clock_to_now() -> None:
    before = datetime.now(UTC)
    metadata = create_metadata(create_body(timeout=60), template="base")
    expires = datetime.fromisoformat(metadata[f"{INTERNAL_PREFIX}expires_at"])
    assert before + timedelta(seconds=60) <= expires <= datetime.now(UTC) + timedelta(seconds=60)


def test_create_metadata_records_an_empty_image_for_a_template_ref_body() -> None:
    """`response_for` reads this back and falls through to the configured image."""
    body = create_body(image=None, extensions={"templateRef": CUSTOM_TEMPLATE})
    metadata = create_metadata(body, template=CUSTOM_TEMPLATE, now=CREATED_AT)
    assert metadata[f"{INTERNAL_PREFIX}image"] == ""


# --- metadata: the read half -----------------------------------------------


def test_expiration_round_trips_the_timeout_create_metadata_stored() -> None:
    metadata = create_metadata(
        create_body(timeout=TIMEOUT_SECONDS), template="base", now=CREATED_AT
    )
    sandbox = sandbox_row(metadata_=metadata)
    assert expiration(sandbox) == CREATED_AT + timedelta(seconds=TIMEOUT_SECONDS)


def test_expiration_normalizes_a_stored_offset_to_utc() -> None:
    sandbox = sandbox_row(
        metadata_={f"{INTERNAL_PREFIX}expires_at": "2026-01-01T14:00:00+02:00"}
    )
    assert expiration(sandbox) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("stored", [{}, {f"{INTERNAL_PREFIX}expires_at": ""}])
def test_expiration_is_none_for_a_sandbox_that_never_expires(stored: dict[str, str]) -> None:
    assert expiration(sandbox_row(metadata_=stored)) is None


def test_expiration_treats_an_unreadable_timestamp_as_no_expiry() -> None:
    """A sandbox nobody can parse an expiry for must not be reaped on a guess."""
    sandbox = sandbox_row(metadata_={f"{INTERNAL_PREFIX}expires_at": "whenever"})
    assert expiration(sandbox) is None


def test_public_metadata_hides_internal_bookkeeping_from_the_caller() -> None:
    metadata = create_metadata(
        create_body(timeout=TIMEOUT_SECONDS, metadata={"owner": "onvo-pro"}),
        template="base",
        now=CREATED_AT,
    )
    assert public_metadata(sandbox_row(metadata_=metadata)) == {"owner": "onvo-pro"}


# --- lifecycle -------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "state", "reason"),
    [
        ("created", "Pending", "awaiting_capacity"),
        ("starting", "Pending", "container_starting"),
        ("running", "Running", "container_running"),
        ("paused_memory", "Paused", "memory_paused"),
        ("paused_cold", "Paused", "cold_paused"),
        ("killed", "Terminated", "user_delete"),
        ("failed", "Failed", "runtime_error"),
    ],
)
def test_lifecycle_status_maps_each_harborbox_state(
    status: str, state: str, reason: str
) -> None:
    rendered = lifecycle_status(sandbox_row(status=status))
    assert (rendered.state, rendered.reason) == (state, reason)
    assert rendered.message
    assert rendered.last_transition_at == CREATED_AT


def test_lifecycle_status_reports_an_unmapped_state_as_failed_and_names_it() -> None:
    """A state added without a mapping must surface, not read as healthy."""
    rendered = lifecycle_status(sandbox_row(status="hibernating"))
    assert rendered.state == "Failed"
    assert rendered.reason == "unknown_state"
    assert "hibernating" in rendered.message


# --- the full response -----------------------------------------------------


def test_response_for_returns_what_create_metadata_stored() -> None:
    body = create_body(
        entrypoint=["python", "-u"],
        timeout=TIMEOUT_SECONDS,
        metadata={"owner": "onvo-pro"},
        extensions={"template": "base"},
        platform={"os": "linux"},
    )
    sandbox = sandbox_row(metadata_=create_metadata(body, template="base", now=CREATED_AT))

    response = response_for(sandbox, Settings())

    assert response.id == "sbx-1"
    assert response.entrypoint == ["python", "-u"]
    assert response.extensions == {"template": "base"}
    assert response.platform == {"os": "linux"}
    assert response.metadata == {"owner": "onvo-pro"}
    assert response.created_at == CREATED_AT
    assert response.expires_at == CREATED_AT + timedelta(seconds=TIMEOUT_SECONDS)
    assert response.image is not None
    assert response.image.uri == IMAGE_URI
    assert response.status.state == "Running"


def test_response_for_falls_back_to_the_configured_image_for_a_template_ref_sandbox() -> None:
    """A templateRef body stores no image, so the response has to resolve one."""
    settings = Settings()
    body = create_body(image=None, extensions={"templateRef": "base"})
    sandbox = sandbox_row(metadata_=create_metadata(body, template="base", now=CREATED_AT))

    response = response_for(sandbox, settings)

    assert response.image is not None
    assert response.image.uri == settings.image_for_template("base")


def test_response_for_survives_metadata_written_before_the_json_keys_existed() -> None:
    """Rows predating the JSON metadata keys must still render, not 500."""
    sandbox = sandbox_row(metadata_={"template": "base", f"{INTERNAL_PREFIX}image": IMAGE_URI})

    response = response_for(sandbox, Settings())

    assert response.entrypoint == []
    assert response.extensions == {}
    assert response.platform is None
    assert response.expires_at is None
