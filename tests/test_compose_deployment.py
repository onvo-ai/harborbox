"""The deployed compose must agree with what the code actually does.

This exists because of a real outage. A deploy came up healthy, reported the
right version, and listed all three templates as ready, while every single
sandbox creation died at `[Errno -3] Temporary failure in name resolution`
dialling an OpenSandbox server that does not run on that host.

Every check that could be done without creating a sandbox passed. So the thing
worth pinning is not the code and not the YAML in isolation, but the agreement
between them: feed the compose file's own environment into `Settings` and assert
the resulting behaviour matches what the host can actually provide.

The outage was originally reached through a runtime-provider setting that chose
between OpenSandbox and an in-sandbox Docker agent. That setting and that agent
are gone -- OpenSandbox is the only runtime -- but the failure this pins never
depended on the choice, only on whether the host answers to the name the API
dials.
"""

import contextlib
import json
import re
from pathlib import Path

import pytest
import yaml

from harborbox.config import Settings
from harborbox.schemas import SandboxCreate

COMPOSE = Path(__file__).resolve().parent.parent / "compose.internal-tools.yaml"

# `${VAR:-default}` and `${VAR}`. Nothing in these files nests, so one pass is
# enough; the point is to read the *defaults* a deploy gets with no .env set.
INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def resolve(value: object) -> str:
    return INTERPOLATION.sub(lambda m: m.group(2) or "", str(value))


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


@pytest.fixture(scope="module")
def settings(compose: dict) -> Settings:
    env = {
        key: resolve(val)
        for key, val in compose["services"]["api"]["environment"].items()
        # Required-with-message vars (`${X:?...}`) have no deploy-time default.
        if ":?" not in str(val)
    }
    prefix = "HARBORBOX_"
    return Settings(
        **{
            key[len(prefix) :].lower(): _decode(val)
            for key, val in env.items()
            if key.startswith(prefix)
        }
    )


def _decode(value: str) -> object:
    """Parse JSON-shaped values the way pydantic-settings does for real env vars.

    Compose passes list settings as `["a","b"]`. Read from the environment
    pydantic-settings decodes that into a frozenset; passed here as a keyword
    it would arrive as a string and fail validation, so the fixture would be
    testing its own shortcut rather than the deployment.
    """
    if value.startswith(("[", "{")):
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(value)
    return value


def built_images(compose: dict) -> set[str]:
    """Every image this compose file actually builds."""
    return {
        resolve(svc["image"])
        for svc in compose["services"].values()
        if "build" in svc and "image" in svc
    }


def test_the_opensandbox_server_the_api_dials_is_actually_run(
    compose: dict, settings: Settings
) -> None:
    """The outage, pinned.

    OpenSandbox needs a server to talk to. The file did not run one, and the
    API dialled a hostname that did not resolve, so no container was ever
    created.
    """
    host = settings.opensandbox_domain.split(":")[0]
    names = set(compose["services"])
    # `networks:` is a bare list on some services and a mapping on others.
    aliases = set()
    for svc in compose["services"].values():
        nets = svc.get("networks")
        if not isinstance(nets, dict):
            continue
        for net in nets.values():
            if isinstance(net, dict):
                aliases.update(net.get("aliases") or [])
    assert host in names | aliases, (
        f"the API dials {host!r}, but no service or alias in {COMPOSE.name} "
        f"answers to that name."
    )


def test_every_static_template_resolves_to_an_image_that_gets_built(
    compose: dict, settings: Settings
) -> None:
    """A template pointing at an unbuilt image fails only at create time."""
    build_targets = built_images(compose)

    for template in settings.template_images:
        image = settings.image_for_template(template)
        assert image in build_targets, (
            f"template {template!r} resolves to {image!r}, which no service in "
            f"{COMPOSE.name} builds. Built: {sorted(build_targets)}"
        )


def test_derived_template_images_share_the_built_tag(
    compose: dict, settings: Settings
) -> None:
    """Derived images are prefix+version by construction, never looked up.

    So if the prefix/version pair drifts from the tags built above, a derived
    template silently resolves to an image nobody built.
    """
    built = built_images(compose)
    derived = settings.derived_template_image("onvo-pro-0123456789ab")
    prefix, _, version = derived.rpartition(":")

    assert prefix.startswith(settings.template_image_prefix)
    assert any(image.endswith(f":{version}") for image in built), (
        f"derived images are tagged :{version}, but nothing built here uses "
        f"that tag. Built: {sorted(built)}"
    )


def test_sandboxes_cannot_reach_the_network(settings: Settings) -> None:
    """The header's first stated invariant: widget code reaches nothing.

    Data arrives as files. This caught the header disagreeing with the config
    it describes — the header said "empty" while the value defaulted to
    `harborbox-egress`, a network that has never existed on that host, so the
    first caller to opt in would have hit a Docker NotFound instead of a clean
    refusal.
    """
    assert not settings.sandbox_egress_network


def test_egress_is_opt_in_so_an_empty_network_is_safe() -> None:
    """Why empty is a refusal and not a silent downgrade.

    `_connect_egress` returns early for any sandbox that did not ask, so an
    unset network changes nothing for widget code. If egress ever became
    default-on, an empty network here would quietly mean "no egress" for
    callers that believe they have it.
    """
    assert SandboxCreate.model_fields["egress"].default is False


def test_no_template_starts_a_long_lived_process() -> None:
    """Every sandbox idles until asked to do something.

    Templates that ran Python used to boot a Jupyter server here, because execd
    runs bash itself but proxied Python to a server nothing else would start.
    Measured (scripts/bench/), that server cost ~3 s of boot and ~197 MB
    resident in every sandbox, to serve one endpoint that now runs Python as an
    ordinary command. A template that grows a bootstrap process again should
    have to justify it against those numbers.
    """
    assert Settings().entrypoint_for_template("base") == ["tail", "-f", "/dev/null"]


def test_built_template_images_are_held_open_by_a_running_container(
    compose: dict, settings: Settings
) -> None:
    """The second outage, pinned: images that nothing references get pruned.

    This host runs Coolify's *forced* docker cleanup on `0 0 * * *` — forced, so
    the 80% disk threshold never gets a vote — and that cleanup prunes stopped
    containers and then runs `docker image prune -af`: delete every image no
    container refers to. The API creates sandboxes from these images at runtime,
    which is a `docker create` against a name, not a standing reference. So a
    build target that exits after building holds nothing open, and at midnight
    its tag is gone. The next widget got

        Failed to pull image harborbox-sandbox-onvo-pro:coolify: 404 ...
        pull access denied for harborbox-sandbox-onvo-pro

    because a locally-built name has no registry to fall back to.

    A running container is the reference prune honours. Nothing here needs the
    container to *do* anything — only to still exist at 00:00 UTC.
    """
    for template in settings.template_images:
        image = settings.image_for_template(template)
        holders = {
            name: svc
            for name, svc in compose["services"].items()
            if resolve(svc.get("image", "")) == image
        }
        assert holders, f"nothing in {COMPOSE.name} declares {image!r}."

        for name, svc in holders.items():
            assert svc.get("restart") in {"always", "unless-stopped"}, (
                f"{name} builds {image!r} but does not restart, so a reboot or "
                f"a cleanup leaves the image unreferenced until the next deploy."
            )
            assert svc.get("entrypoint") != ["/bin/true"], (
                f"{name} exits as soon as it builds {image!r}. The nightly "
                f"`docker image prune -af` then deletes the image and every "
                f"sandbox create fails until someone redeploys."
            )


def test_the_api_does_not_wait_for_the_image_holders_to_exit(compose: dict) -> None:
    """`service_completed_successfully` on a container that never exits hangs.

    The two halves of the fix above live in different blocks of the same file:
    make the build targets long-lived without loosening this condition and the
    deploy waits forever for an exit code that is never coming.
    """
    holders = {
        name
        for name, svc in compose["services"].items()
        if "build" in svc and svc.get("restart") in {"always", "unless-stopped"}
    }
    depends_on = compose["services"]["api"]["depends_on"]

    for name in holders & set(depends_on):
        assert depends_on[name]["condition"] != "service_completed_successfully", (
            f"api waits for {name} to complete, but {name} is a long-lived "
            f"container by design."
        )


def test_built_images_carry_the_label_that_survives_coolify_cleanup(
    compose: dict,
) -> None:
    """The other half of the prune fix, and the half that needs no container.

    Coolify's `CleanupDocker` runs a plain `docker rmi` over every image that is
    neither named after an application repo nor labelled `coolify.managed=true`.
    The keeper containers above defeat that too, but only while they are up —
    a `docker stop` puts the image back on death row for the next 00:00 UTC
    sweep. The label does not care about container state.
    """
    root = Path(__file__).resolve().parent.parent

    dockerfiles = {
        svc["build"]["dockerfile"]
        for svc in compose["services"].values()
        if isinstance(svc.get("build"), dict) and svc["build"].get("dockerfile")
    }
    assert dockerfiles, "no build targets found; this test would pass vacuously."

    for name in sorted(dockerfiles):
        body = (root / name).read_text()
        assert "LABEL coolify.managed=true" in body, (
            f"{name} builds an image on the host, and Coolify's nightly cleanup "
            f"deletes host-built images that carry no `coolify.managed` label. "
            f"A deleted one has no registry to be pulled back from."
        )


def test_the_api_does_not_hold_the_docker_socket(compose: dict) -> None:
    """Phase 0's whole point: the control plane stops being able to run anything.

    The API previously mounted the socket only to build derived template images.
    Those builds now happen on the rootless builder, so the mount is no longer
    load-bearing -- and while it is present, a leaked API key is still one
    request away from arbitrary code execution on the host, which is what the
    Traefik routing comments in this file are working around.
    """
    volumes = compose["services"]["api"].get("volumes") or []

    assert not [volume for volume in volumes if "docker.sock" in resolve(volume)]


def test_the_builder_holds_no_docker_socket_either(compose: dict) -> None:
    """A builder with the socket would be the same hole wearing a different hat."""
    builder = compose["services"]["builder"]
    volumes = builder.get("volumes") or []

    assert not [volume for volume in volumes if "docker.sock" in resolve(volume)]
    assert builder.get("privileged") is not True


def test_the_builder_is_not_on_the_host_network(compose: dict) -> None:
    """Host networking would put every build step on the host's loopback.

    Verified during the Q2 spike: with `network_mode: host` a `RUN` step reached
    a service published on host loopback. On this host that reaches PostgreSQL
    and the API itself, so the builder gets its own bridge network instead.
    """
    builder = compose["services"]["builder"]

    assert builder.get("network_mode") != "host"
    assert "build" in builder["networks"]


def test_the_builder_reaches_the_registry_and_nothing_else(compose: dict) -> None:
    """The build network exists to hold exactly two members."""
    on_build_network = {
        name
        for name, service in compose["services"].items()
        if "build" in (service.get("networks") or [])
    }

    assert on_build_network == {"builder", "registry"}


def test_the_push_and_pull_endpoints_address_one_registry(settings: Settings) -> None:
    """Different hosts, identical repository paths -- see docs section 10.3.

    A mismatch below the host part would build cleanly and then fail at sandbox
    create, which is the failure mode this whole split exists to avoid.
    """
    push = settings.push_image_for_template("base")
    pull = settings.image_for_template("base")

    assert push.split("/", 1)[0] != pull.split("/", 1)[0]
    assert push.split("/", 1)[1] == pull.split("/", 1)[1]


def test_the_api_drives_the_builder_over_a_socket_it_actually_mounts(
    compose: dict, settings: Settings
) -> None:
    """The path in HARBORBOX_BUILDER_ADDRESS has to exist in the API container.

    Same class of failure as the outage this module was written for: a
    configured address nothing answers to. A socket makes it checkable
    statically, which a hostname never was.
    """
    assert settings.builder_address is not None
    path = settings.builder_address.removeprefix("unix://")
    mounts = {
        resolve(volume).split(":")[1]
        for volume in compose["services"]["api"]["volumes"]
    }

    assert any(path.startswith(mount) for mount in mounts)


def test_the_api_shares_no_network_with_the_builder(compose: dict) -> None:
    """A socket instead of TCP is what buys this, and it is the point.

    Build steps run inside buildkitd's own network namespace, so every network
    the builder joins is one that caller-supplied build steps can reach. Over
    TCP the API would have to share one.
    """
    shared = set(compose["services"]["api"]["networks"]) & set(
        compose["services"]["builder"]["networks"]
    )

    assert not shared


BAKE = Path(__file__).resolve().parent.parent / "docker-bake.hcl"


def bake_tags(registry: str, prefix: str, version: str) -> set[str]:
    """Return the tags `scripts/build-templates.sh` would build and push.

    A regex rather than an HCL parser, for the same reason `resolve` above is a
    regex over compose interpolation: the file does not nest, and the point is
    to read what a deploy actually gets.
    """
    text = BAKE.read_text()
    substitutions = {
        # Mirrors the ternary the HCL computes for this local. Verified against
        # `docker buildx bake --print`, which is the real evaluator and too
        # heavy to invoke from a unit test.
        "${TEMPLATE_REGISTRY_PREFIX}": f"{registry}/" if registry else "",
        "${TEMPLATE_IMAGE_PREFIX}": prefix,
        "${TEMPLATE_VERSION}": version,
    }
    tags = set()
    for raw in re.findall(r'tags\s*=\s*\["([^"]+)"\]', text):
        tag = raw
        for placeholder, value in substitutions.items():
            tag = tag.replace(placeholder, value)
        tags.add(tag)
    return tags


def test_the_static_images_are_pushed_under_the_names_the_api_resolves(
    settings: Settings,
) -> None:
    """`build-templates.sh --push` has to publish what `image_for_template` asks for.

    These are two independent spellings of the same name -- one in HCL, one in
    Settings -- and nothing but this test makes them agree. A mismatch is
    invisible until a sandbox create 404s on an image nobody pushed.
    """
    built = bake_tags(
        registry=settings.registry_pull_endpoint or "",
        prefix=settings.template_image_prefix,
        version=settings.template_version,
    )

    assert settings.image_for_template("base") in built


def test_the_api_can_reach_the_registry_it_deletes_from(compose: dict) -> None:
    """The template collector deletes manifests over HTTP, so it has to get there.

    The registry is dual-homed for this: `build` for the builder's push,
    `control` for the API's own calls. Docker does not route between bridge
    networks, so this does not put the builder within reach of the control
    plane -- which is the property test_the_api_shares_no_network_with_the_builder
    pins from the other side.
    """
    shared = set(compose["services"]["api"]["networks"]) & set(
        compose["services"]["registry"]["networks"]
    )

    assert shared
