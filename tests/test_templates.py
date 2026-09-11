from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborbox.config import Settings
from harborbox.schemas import SandboxCreate
from harborbox.templates import (
    TemplateSpec,
    TemplateSpecError,
    render_dockerfile,
    validate_template_spec,
)


def test_template_is_mandatory() -> None:
    with pytest.raises(ValidationError):
        SandboxCreate.model_validate({})


def test_unregistered_template_has_no_generic_fallback() -> None:
    settings = Settings()

    with pytest.raises(KeyError, match="registered sandbox template"):
        settings.image_for_template(None)

    with pytest.raises(KeyError):
        settings.image_for_template("not-a-template")


def test_warm_pool_must_fit_configured_memory_budget() -> None:
    with pytest.raises(ValidationError, match="warm pool exceeds"):
        Settings(sandbox_memory_budget_mb=256, warm_pool={"base": 8})


def test_push_and_pull_references_differ_only_in_the_registry_host() -> None:
    """The builder and the sandbox runtime address one repository by two names.

    BuildKit dials the registry from inside the build network; the Docker
    daemon pulls it from the host. Those are different addresses for the same
    store, and a registry does not care about the host part of a reference --
    but the repository path after it must match exactly, or the build succeeds
    and the sandbox create that follows fails on a missing image.
    """
    settings = Settings(
        registry_push_endpoint="registry:5000",
        registry_pull_endpoint="127.0.0.1:5050",
        template_version="2026.08.03",
    )
    name = "custom-a1b2c3d4e5f6"

    push = settings.push_image_for_template(name)
    pull = settings.image_for_template(name)

    assert push == "registry:5000/harborbox-sandbox-custom-a1b2c3d4e5f6:2026.08.03"
    assert pull == "127.0.0.1:5050/harborbox-sandbox-custom-a1b2c3d4e5f6:2026.08.03"
    assert push.split("/", 1)[1] == pull.split("/", 1)[1]


def test_static_templates_are_addressed_through_the_registry_too() -> None:
    """The endpoint prefixes the configured reference; it does not recompute it.

    `HARBORBOX_RELAYDECK_IMAGE` and friends stay authoritative for the
    repository path and tag, so pointing a deployment at a registry does not
    silently rename the images it was already pinned to.
    """
    settings = Settings(
        registry_pull_endpoint="127.0.0.1:5050",
        base_image="harborbox-sandbox-base:2026.08.03",
    )

    assert (
        settings.image_for_template("base")
        == "127.0.0.1:5050/harborbox-sandbox-base:2026.08.03"
    )


def test_without_a_registry_images_keep_their_local_daemon_names() -> None:
    """The registry is opt-in; an unconfigured deployment still builds locally."""
    settings = Settings()

    assert (
        settings.image_for_template("custom-a1b2c3d4e5f6")
        == "harborbox-sandbox-custom-a1b2c3d4e5f6:local"
    )
    assert (
        settings.push_image_for_template("custom-a1b2c3d4e5f6")
        == "harborbox-sandbox-custom-a1b2c3d4e5f6:local"
    )


RAW = "FROM debian:bookworm-slim\nRUN apt-get update && apt-get install -y jq\n"
SPEC_HASH_LENGTH = 12


def raw_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def test_a_raw_dockerfile_names_itself_from_its_own_digest() -> None:
    """A Dockerfile with no base template is not derived from anything.

    `<base>-<hash>` would be a lie, so it gets its own namespace. The hash is
    still the whole identity, which is what keeps the endpoint idempotent.
    """
    spec = validate_template_spec(raw_settings(), dockerfile=RAW)

    assert spec.dockerfile == RAW
    assert spec.name == f"custom-{spec.spec_hash}"
    assert len(spec.spec_hash) == SPEC_HASH_LENGTH


def test_an_identical_dockerfile_is_the_same_template() -> None:
    settings = raw_settings()

    first = validate_template_spec(settings, dockerfile=RAW)
    second = validate_template_spec(settings, dockerfile=RAW)
    different = validate_template_spec(settings, dockerfile=RAW + "RUN true\n")

    assert first.spec_hash == second.spec_hash
    assert first.spec_hash != different.spec_hash


def test_a_raw_dockerfile_round_trips_through_its_stored_json() -> None:
    """The spec column is what a rebuild reads, so it has to survive the trip."""
    spec = validate_template_spec(raw_settings(), dockerfile=RAW)

    restored = TemplateSpec.from_json(spec.as_json())

    assert restored == spec
    assert restored.name == spec.name


def test_from_lines_must_name_an_allowlisted_registry() -> None:
    """The one control that still means something once arbitrary RUN exists.

    Everything a build installs is unbounded by construction; what it *starts
    from* is not, and that is the difference between "any Dockerfile" and "any
    Dockerfile beginning at an image we vetted".
    """
    settings = raw_settings(template_from_allowlist=["docker.io/library"])

    ok = validate_template_spec(settings, dockerfile="FROM docker.io/library/debian:12\n")
    assert ok.dockerfile

    with pytest.raises(TemplateSpecError, match="not allowlisted"):
        validate_template_spec(settings, dockerfile="FROM evil.example.com/x:1\n")


def test_the_implicit_docker_hub_prefix_is_resolved_before_matching() -> None:
    """`FROM debian:12` and `FROM docker.io/library/debian:12` are one image.

    Matching the raw text would let the short form slip past an allowlist that
    only names the long one.
    """
    settings = raw_settings(template_from_allowlist=["docker.io/library"])

    assert validate_template_spec(settings, dockerfile="FROM debian:12\n").dockerfile

    with pytest.raises(TemplateSpecError, match="not allowlisted"):
        validate_template_spec(settings, dockerfile="FROM someuser/debian:12\n")


def test_every_from_line_is_checked_not_just_the_first() -> None:
    """A multi-stage build has more than one entry point into the image."""
    settings = raw_settings(template_from_allowlist=["docker.io/library"])
    dockerfile = (
        "FROM debian:12 AS build\n"
        "RUN true\n"
        "FROM evil.example.com/base:1\n"
        "COPY --from=build /x /x\n"
    )

    with pytest.raises(TemplateSpecError, match="not allowlisted"):
        validate_template_spec(settings, dockerfile=dockerfile)


def test_a_named_build_stage_is_not_mistaken_for_a_registry() -> None:
    """`FROM build` refers to an earlier stage, not to something to pull."""
    settings = raw_settings(template_from_allowlist=["docker.io/library"])
    dockerfile = "FROM debian:12 AS build\nRUN true\nFROM build\nRUN true\n"

    assert validate_template_spec(settings, dockerfile=dockerfile).dockerfile


def test_the_base_image_is_always_buildable_from() -> None:
    """A product must be able to start from the image this repo ships."""
    settings = raw_settings(registry_push_endpoint="registry:5000")

    spec = validate_template_spec(
        settings, dockerfile="FROM registry:5000/harborbox-sandbox-base:local\n"
    )

    assert spec.dockerfile


def test_a_dockerfile_must_contain_at_least_one_from() -> None:
    with pytest.raises(TemplateSpecError, match="FROM"):
        validate_template_spec(raw_settings(), dockerfile="RUN echo hello\n")


def test_a_dockerfile_is_capped_in_size_and_instruction_count() -> None:
    settings = raw_settings(template_max_dockerfile_bytes=256)

    with pytest.raises(TemplateSpecError, match="bytes"):
        validate_template_spec(settings, dockerfile="FROM debian:12\n" + "RUN true\n" * 100)

    small = raw_settings(template_max_dockerfile_instructions=2)
    with pytest.raises(TemplateSpecError, match="instructions"):
        validate_template_spec(small, dockerfile="FROM debian:12\nRUN a\nRUN b\nRUN c\n")


def test_a_raw_dockerfile_is_emitted_verbatim_then_made_conformant() -> None:
    """The caller's file is theirs; the trailing contract is ours.

    Harborbox's equivalent of E2B injecting envd. Whatever the caller wrote,
    the image has to end up with uid 10001 owning a writable /workspace, or a
    sandbox created from it cannot write its own working directory.
    """
    caller = "FROM debian:bookworm-slim\nRUN apt-get update\nUSER nobody\n"

    rendered = render_dockerfile(caller)

    assert rendered.startswith(caller)
    body = rendered[len(caller) :]
    assert "USER root" in body
    assert "10001" in body
    assert "/workspace" in body
    # Ours wins: a caller's own trailing USER must not decide who the sandbox
    # runs as.
    assert rendered.rstrip().endswith("USER 10001:10001")


def test_the_conformance_layer_tolerates_a_base_that_already_conforms() -> None:
    """Our own static bases already have the user; adding it twice must not fail.

    `useradd` exits non-zero when the user exists, which would turn "built on a
    Harborbox base" into a build failure.
    """
    rendered = render_dockerfile("FROM debian:12\n")

    assert "getent" in rendered


def test_a_dockerfile_with_no_content_is_refused_before_anything_else() -> None:
    """An empty spec must not reach the builder as an empty image.

    `POST /v1/templates` takes the Dockerfile as a string, so "" and a file of
    blank lines are both things a caller can send by accident — a template
    request whose payload silently lost its body. Refusing here is what stops
    that becoming a build of the conformance layer alone.
    """
    for empty in ("", "   ", "\n\n", "  \n\t\n  "):
        with pytest.raises(TemplateSpecError, match="a dockerfile is required"):
            validate_template_spec(raw_settings(), dockerfile=empty)


def test_a_build_context_must_be_a_sha256_digest() -> None:
    """The context is an opaque handle from POST /v1/build-contexts, not a path.

    Anything else is either a caller inventing a reference or a path it hopes
    the builder will read, and both have to stop at validation rather than at
    whatever the builder does with an unknown digest.
    """
    digest = "sha256:" + "ab" * 32
    spec = validate_template_spec(raw_settings(), dockerfile=RAW, context=digest)
    assert spec.context_digest == digest

    for bad in ("../etc/passwd", "sha256:short", "ab" * 32, "SHA256:" + "ab" * 32):
        with pytest.raises(TemplateSpecError, match="invalid build context digest"):
            validate_template_spec(raw_settings(), dockerfile=RAW, context=bad)


def test_comments_and_blank_lines_are_not_instructions() -> None:
    """The instruction cap counts build steps, not lines of text.

    A well-commented Dockerfile would otherwise be refused for being
    well-commented, which is the sort of limit people route around by deleting
    the comments.
    """
    settings = raw_settings(template_max_dockerfile_instructions=2)
    commented = (
        "# what this image is for\n"
        "FROM debian:bookworm-slim\n"
        "\n"
        "   \n"
        "# install the toolchain\n"
        "RUN apt-get update\n"
    )

    spec = validate_template_spec(settings, dockerfile=commented)

    assert spec.dockerfile == commented


def test_a_line_continuation_counts_as_one_instruction() -> None:
    """A wrapped RUN is one build step, and the cap has to agree.

    Every non-trivial Dockerfile chains `apt-get` across wrapped lines. Counting
    each physical line would make the instruction cap a line-length cap, so the
    two forms of the same build must count the same.
    """
    settings = raw_settings(template_max_dockerfile_instructions=2)
    wrapped = (
        "FROM debian:bookworm-slim\n"
        "RUN apt-get update \\\n && apt-get install -y \\\n    curl\n"
    )

    spec = validate_template_spec(settings, dockerfile=wrapped)
    assert spec.dockerfile == wrapped

    # The same steps unwrapped are three instructions, and the same cap refuses
    # them — which is what proves the joining above, not a cap that never bit.
    unwrapped = "FROM debian:bookworm-slim\nRUN apt-get update\nRUN apt-get install -y curl\n"
    with pytest.raises(TemplateSpecError, match="instructions"):
        validate_template_spec(settings, dockerfile=unwrapped)


def test_a_dockerfile_ending_mid_continuation_still_counts_its_last_step() -> None:
    """A trailing backslash must not make the last instruction disappear.

    The joiner only appends when it meets a line that does *not* end in a
    backslash, so a file whose final line does would leave that step buffered
    and uncounted — and an uncounted instruction is one that walks past both the
    instruction cap and the FROM allowlist.
    """
    settings = raw_settings(template_max_dockerfile_instructions=1)
    dangling = "FROM debian:bookworm-slim\nRUN apt-get update \\\n"

    with pytest.raises(TemplateSpecError, match="has 2 instructions"):
        validate_template_spec(settings, dockerfile=dangling)


def test_a_from_carrying_only_flags_names_no_image() -> None:
    """`FROM --platform=…` with nothing after it is refused, not allowlisted.

    Flags are stripped before the allowlist check, so a FROM whose arguments are
    all flags leaves nothing to check. Reading that as "no image, therefore
    nothing disallowed" would be a hole straight through the supply-chain
    control.
    """
    with pytest.raises(TemplateSpecError, match="names no image"):
        validate_template_spec(
            raw_settings(), dockerfile="FROM --platform=linux/amd64\nRUN true\n"
        )
