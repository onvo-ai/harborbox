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
from harborbox.telemetry import SERVICE_NAME, export_is_configured, service_name_for

COMPOSE = Path(__file__).resolve().parent.parent / "compose.internal-tools.yaml"
# The builder is a *second* Compose project, deployed as a second Coolify
# application. That separation is load-bearing rather than tidiness, and the
# tests below are written against both files together because the property
# worth pinning -- what a caller's build step can reach -- is a property of the
# pair.
BUILDER_COMPOSE = Path(__file__).resolve().parent.parent / "compose.builder.yaml"
# The local development stack. Read here only to assert what it does *not* do:
# telemetry has a safe default, and a safe default is worth nothing unless
# something checks it is still the default.
LOCAL_COMPOSE = Path(__file__).resolve().parent.parent / "compose.yaml"

# `${VAR:-default}` and `${VAR}`. Nothing in these files nests, so one pass is
# enough; the point is to read the *defaults* a deploy gets with no .env set.
INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def resolve(value: object) -> str:
    return INTERPOLATION.sub(lambda m: m.group(2) or "", str(value))


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


@pytest.fixture(scope="module")
def builder_compose() -> dict:
    return yaml.safe_load(BUILDER_COMPOSE.read_text())


@pytest.fixture(scope="module")
def local_compose() -> dict:
    return yaml.safe_load(LOCAL_COMPOSE.read_text())


def deploy_time_environment(compose: dict) -> dict[str, str]:
    """Return the api service's environment as a deploy with no .env sees it.

    Required-with-message vars (`${X:?...}`) are dropped: they have no
    deploy-time value at all, and the deploy fails by name rather than starting
    with an empty one.
    """
    return {
        key: resolve(value)
        for key, value in compose["services"]["api"]["environment"].items()
        if ":?" not in str(value)
    }


def networks_of(service: dict) -> set[str]:
    """Return the network keys a service joins.

    `networks:` is a bare list on some services and a mapping (with aliases) on
    others; both spellings mean the same thing here.
    """
    nets = service.get("networks") or {}
    return set(nets)


def services_on(compose: dict, network: str) -> set[str]:
    return {
        name
        for name, service in compose["services"].items()
        if network in networks_of(service)
    }


def network_name(compose: dict, key: str) -> str:
    """Return the real Docker network name behind a compose network key.

    The `build` network is external and shared between the two projects, so the
    two files agreeing on this string is what makes "the builder and the
    registry are on one network" true rather than a coincidence of naming.
    """
    # A Compose-managed network may be declared as a bare key with no body at
    # all (`control:`), which yaml reads as None; its real name is then scoped
    # to the project, so the key is the right identity for it here.
    declared = compose["networks"].get(key) or {}
    return resolve(declared.get("name") or key)


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


def test_the_builder_holds_no_docker_socket_either(builder_compose: dict) -> None:
    """A builder with the socket would be the same hole wearing a different hat."""
    builder = builder_compose["services"]["builder"]
    volumes = builder.get("volumes") or []

    assert not [volume for volume in volumes if "docker.sock" in resolve(volume)]
    assert builder.get("privileged") is not True


def test_the_builder_is_not_on_the_host_network(builder_compose: dict) -> None:
    """Host networking would put every build step on the host's loopback.

    Verified during the Q2 spike: with `network_mode: host` a `RUN` step reached
    a service published on host loopback. On this host that reaches PostgreSQL
    and the API itself, so the builder gets its own bridge network instead.
    """
    builder = builder_compose["services"]["builder"]

    assert builder.get("network_mode") != "host"
    assert "build" in networks_of(builder)


def test_the_builder_and_the_control_plane_are_separate_compose_projects(
    compose: dict, builder_compose: dict
) -> None:
    """The whole fix, in one assertion, and the one nothing was checking.

    Coolify appends its own project network to every service of a compose
    application. That is invisible to this repository -- it happens after the
    file is read -- so no amount of `networks:` discipline within one file can
    stop it, and `connect_to_docker_network` was already false on the
    application it happened to.

    What *can* be arranged is that the project is not worth attaching to. A
    distinct `name:` is what makes Coolify treat the builder as its own
    application with its own project network, which is why this is asserted on
    the literal field rather than inferred.
    """
    assert compose["name"] != builder_compose["name"]
    assert builder_compose["name"] == "harborbox-builder"


def test_coolify_can_only_attach_the_builder_to_itself(builder_compose: dict) -> None:
    """One service in the builder project, so its project network has one member.

    This is the other half of the separation and the easier half to lose: add a
    second service to compose.builder.yaml -- a cache warmer, a log shipper, a
    metrics sidecar -- and the orchestrator's project network silently becomes
    a bridge from a caller's build step to that service. Anything the builder
    genuinely needs alongside it belongs on the shared `build` network, where
    `test_the_builder_reaches_the_registry_and_nothing_else` will see it.
    """
    assert set(builder_compose["services"]) == {"builder"}


def test_the_builder_reaches_the_registry_and_nothing_else(
    compose: dict, builder_compose: dict
) -> None:
    """What a caller-supplied `RUN` can reach, computed the way deployment builds it.

    The previous version of this test asserted that exactly two services in
    this one file named the `build` network. That was true, it stayed true, and
    the deployment was open anyway -- measured on the infrastructure host, from
    inside the builder container:

        api:8000/health   -> {"status":"ok"}
        opensandbox:8080  -> HTTP/1.1 401 Unauthorized
        postgres:5432     -> OPEN

    The test could not see it because a compose file is not the whole
    deployment: Coolify appends its project network to every service of an
    application, so the builder was on a second network that appears in no file
    here. A test that reads one file and asserts about one network is testing
    the file, not the property.

    So this one models the appended network instead of ignoring it. A build
    step runs inside buildkitd's network namespace, so it reaches:

      - everything on the shared `build` network, from either project;
      - everything in the builder's own compose project, because that is what
        an orchestrator's project network would join it to.

    Static analysis still cannot prove what a *host* does -- only
    tests/e2e_build_isolation.py, which dials these services from inside a real
    build step, does that. What this pins is that the arrangement is one where
    the appended network has nothing in it.
    """
    assert network_name(compose, "build") == network_name(builder_compose, "build")

    reachable = (
        services_on(compose, "build")
        | services_on(builder_compose, "build")
        | set(builder_compose["services"])
    ) - {"builder"}

    assert reachable == {"registry", "buildkit-gateway"}
    # Named individually as well, because the set comparison above would also
    # pass if someone renamed the API to `registry`. These three are what was
    # actually reachable in production.
    assert not reachable & {"api", "postgres", "opensandbox"}


def test_the_builder_requires_a_client_certificate(builder_compose: dict) -> None:
    """Reaching buildkitd's port must not be the same as being able to build.

    The gateway that fronts the port is on the build network by construction,
    so a caller's build step can open a connection to it. `[grpc.tls] ca` is
    the line that makes buildkitd demand and verify a client certificate; drop
    it and buildkitd logs "TLS is not enabled ... enabling mutual TLS
    authentication is highly recommended", accepts anyone, and every build
    keeps working -- so nothing else would notice.
    """
    config = builder_compose["configs"]["buildkitd-config"]["content"]

    assert "tcp://0.0.0.0:1234" in config
    assert "[grpc.tls]" in config
    for key in ("ca =", "cert =", "key ="):
        assert key in config, f"{key} missing from the buildkitd config"
    # The resolver fix that made build steps able to install anything at all;
    # BuildKit's default of 8.8.8.8 is firewalled on the deployed host and
    # every `pip install` fails looking like a bad version pin.
    assert '[dns]\n  nameservers = ["127.0.0.11"]' in config


def test_the_push_and_pull_endpoints_address_one_registry(settings: Settings) -> None:
    """Different hosts, identical repository paths -- see docs section 10.3.

    A mismatch below the host part would build cleanly and then fail at sandbox
    create, which is the failure mode this whole split exists to avoid.
    """
    push = settings.push_image_for_template("base")
    pull = settings.image_for_template("base")

    assert push.split("/", 1)[0] != pull.split("/", 1)[0]
    assert push.split("/", 1)[1] == pull.split("/", 1)[1]


def test_the_api_drives_the_builder_through_something_that_answers(
    compose: dict, settings: Settings
) -> None:
    """The address in HARBORBOX_BUILDER_ADDRESS has to resolve on this stack.

    Same class of failure as the outage this module was written for: a
    configured address nothing answers to. This used to be a unix socket, which
    was checkable by looking for the mount; now it is a hostname again, so the
    check is the one that caught that outage -- a service or an alias must
    answer to the name.
    """
    assert settings.builder_address is not None
    host = settings.builder_address.removeprefix("tcp://").split(":")[0]
    aliases = set()
    for service in compose["services"].values():
        nets = service.get("networks")
        if isinstance(nets, dict):
            for net in nets.values():
                if isinstance(net, dict):
                    aliases.update(net.get("aliases") or [])

    assert host in set(compose["services"]) | aliases


def test_the_api_holds_a_client_certificate_it_actually_mounts(
    compose: dict, settings: Settings
) -> None:
    """Every path the API is told to authenticate with must exist in its container.

    Settings already refuses a `tcp://` builder with no certificate configured,
    which is the security half. This is the deployment half: three paths that
    point at nothing would pass that validator and fail at the first build,
    with a buildctl error about a file rather than about configuration.
    """
    mounts = {
        resolve(volume).split(":")[1] for volume in compose["services"]["api"]["volumes"]
    }
    configured = [
        settings.builder_tls_ca_cert,
        settings.builder_tls_cert,
        settings.builder_tls_key,
    ]

    assert all(configured)
    for path in configured:
        assert path is not None
        assert any(path.startswith(f"{mount}/") for mount in mounts), (
            f"{path} is not inside anything the api service mounts: {sorted(mounts)}"
        )


def test_the_api_shares_no_network_with_the_builder(
    compose: dict, builder_compose: dict
) -> None:
    """The property the socket used to buy, kept across the TCP move.

    Build steps run inside buildkitd's own network namespace, so every network
    the builder joins is one that caller-supplied build steps can reach. The
    API therefore stays off all of them and reaches buildkitd through the
    dual-homed gateway instead -- the same shape as the registry, and safe for
    the same reason: Docker does not route between bridge networks.

    Both project networks are included, because that is where the deployed
    topology differed from the file: distinct `name:` fields mean the
    orchestrator's appended networks are distinct too.
    """
    api = {
        network_name(compose, key) for key in networks_of(compose["services"]["api"])
    } | {compose["name"]}
    builder = {
        network_name(builder_compose, key)
        for key in networks_of(builder_compose["services"]["builder"])
    } | {builder_compose["name"]}

    assert not api & builder


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


LOCAL_COMPOSE = Path(__file__).resolve().parent.parent / "compose.yaml"


def test_the_bundled_local_stack_can_actually_construct_its_settings() -> None:
    """compose.yaml's own defaults must boot, and nothing was checking that.

    Every other test in this module reads compose.internal-tools.yaml, so a
    validator that rejected the *local* defaults passed the whole suite and
    then refused to start: `warm pool leaves no CPU headroom`, on `docker
    compose up` with no .env at all.
    """
    compose = yaml.safe_load(LOCAL_COMPOSE.read_text())
    env = {
        key: resolve(value)
        for key, value in compose["services"]["api"]["environment"].items()
        if ":?" not in str(value)
    }
    prefix = "HARBORBOX_"

    settings = Settings(
        **{
            key[len(prefix) :].lower(): _decode(value)
            for key, value in env.items()
            if key.startswith(prefix)
        }
    )

    assert settings.warm_pool_sizes


def test_the_deployment_exports_under_the_name_the_checkup_queries(
    compose: dict, settings: Settings
) -> None:
    """DEV-1948, pinned at the layer it actually broke.

    Nothing in the code was wrong: the app simply had no endpoint and no
    environment, so it exported nothing, so the daily checkup's
    `service.name = harborbox` error query returned a structural zero and
    rendered it green. Both halves have to be right here for that query to have
    anything to find -- an endpoint, and an environment that resolves to the
    bare service name rather than the `-dev` one.
    """
    assert export_is_configured(deploy_time_environment(compose)), (
        f"{COMPOSE.name} configures no OTLP endpoint, so this deploy exports "
        f"nothing and the daily checkup's error count is structurally zero."
    )
    assert service_name_for(settings.environment) == SERVICE_NAME, (
        f"{COMPOSE.name} deploys as {settings.environment!r}, which exports "
        f"under {service_name_for(settings.environment)!r}. admin queries "
        f"{SERVICE_NAME!r}."
    )


def test_the_deployment_carries_the_ingestion_header_the_estate_uses(
    compose: dict,
) -> None:
    """The same header and the same Infisical entry as every other service.

    Not asserted as *required*: the self-hosted ingester does not enforce the
    token, which is what DEV-1858 was verified against -- an unauthenticated
    `POST https://ingester.onvo.ai/v1/traces` returns 200. What is worth
    pinning is that the wiring stays in place, so enabling enforcement later is
    one Infisical entry rather than a change to this file.
    """
    headers = str(compose["services"]["api"]["environment"]["OTEL_EXPORTER_OTLP_HEADERS"])

    assert headers.startswith("signoz-access-token=")
    assert "SIGNOZ_INGESTION_KEY" in headers, (
        "the header is hardcoded or reads some other variable, so the estate's "
        "existing Infisical entry no longer reaches this deploy."
    )


def test_the_deployment_does_not_export_through_the_box_it_watches(
    compose: dict,
) -> None:
    """DEV-1829's trade-off, applied here.

    SigNoz runs on `internal` (91.99.125.153). Exporting to it over localhost
    or by its own address means a SigNoz outage also loses the telemetry that
    would explain it, so this goes through the public ingester like every other
    exporting service. The address matters twice: configs written before
    2026-08-07 point at 91.99.169.190:4317, where nothing has listened since,
    and an OTLP exporter fails silently against a dead endpoint.
    """
    endpoint = deploy_time_environment(compose)["OTEL_EXPORTER_OTLP_ENDPOINT"]

    assert endpoint.startswith("https://"), endpoint
    for dead in ("localhost", "127.0.0.1", "91.99.169.190"):
        assert dead not in endpoint, (
            f"{endpoint} exports through {dead}, which is either the box being "
            f"watched or an address that stopped listening on 2026-08-07."
        )


def test_local_development_does_not_export_to_the_estate(local_compose: dict) -> None:
    """DEV-1824's rule, kept as a property of the file rather than a habit.

    Laptops exporting under the production service name made 94% of
    "production" errors somebody's `pnpm dev` and hid the two real ones. Both
    defences are asserted, because either alone is one edit from being lost:
    the local stack exports nowhere, and it would not collide even if it did.
    """
    env = deploy_time_environment(local_compose)

    assert not export_is_configured(env), (
        f"{LOCAL_COMPOSE.name} exports by default, so every local stack posts "
        f"into the estate's SigNoz."
    )
    assert service_name_for(env["HARBORBOX_ENVIRONMENT"]) != SERVICE_NAME, (
        f"{LOCAL_COMPOSE.name} would export under {SERVICE_NAME!r} if anyone "
        f"set an endpoint, which is exactly the collision DEV-1824 cost a week."
    )


# Linux's default ephemeral range, and the one both harborbox hosts actually
# run (`/proc/sys/net/ipv4/ip_local_port_range` reads `32768 60999` on
# build-server and on infrastructure). The kernel draws from this range for
# every outbound connection and every bind to port 0.
EPHEMERAL_PORT_RANGE = (32768, 60999)

# Ports below this need CAP_NET_BIND_SERVICE to bind.
FIRST_UNPRIVILEGED_PORT = 1024

SANDBOX_PORT_RANGE = re.compile(
    r"port_range_min\s*=\s*(\d+)\s*\n\s*port_range_max\s*=\s*(\d+)"
)


@pytest.mark.parametrize("path", [COMPOSE, LOCAL_COMPOSE])
def test_sandbox_ports_do_not_overlap_the_kernel_ephemeral_range(path: Path) -> None:
    """A published sandbox port must be one the kernel will never hand out.

    OpenSandbox publishes each sandbox on a host port drawn from
    `[port_range_min, port_range_max]`, and it *remembers* the port: resuming a
    cold-paused sandbox asks for the same one back. Between the pause and the
    resume the container is gone and the port is free, so anything may take it.

    While these ranges sat inside the kernel's ephemeral range, "anything"
    included every outbound socket on the box -- a `pip install`, a
    `docker pull`, another runner's git fetch. On 2026-08-26 a resume lost that
    race on a CI host with 80 ephemeral sockets open:

        failed to bind host port 0.0.0.0:51068/tcp: address already in use
        FAILED tests/e2e_pause_ladder.py::test_a_cold_pause_preserves_the_filesystem

    51068 was inside 40000-60000 (this file's old range) and inside
    32768-60999 (the kernel's). Nothing had leaked and nothing was misbehaving;
    two allocators were simply dealing from the same deck.

    The deployed stack had the same overlap with a narrower range, so this was
    never CI-only -- a production resume could fail the same way and surface as
    a 503 `SANDBOX_START_FAILED`.

    Staying below 32768 is what makes the sandbox range exclusively
    OpenSandbox's to allocate. Explicit binds there still work; the kernel just
    never chooses them on its own.
    """
    text = path.read_text(encoding="utf-8")
    match = SANDBOX_PORT_RANGE.search(text)
    assert match is not None, f"{path.name} declares no sandbox port range"

    low, high = int(match.group(1)), int(match.group(2))
    ephemeral_low, ephemeral_high = EPHEMERAL_PORT_RANGE

    assert low <= high, f"{path.name} has an inverted port range: {low}-{high}"
    assert high < ephemeral_low, (
        f"{path.name} publishes sandboxes on {low}-{high}, which overlaps the "
        f"kernel's ephemeral range {ephemeral_low}-{ephemeral_high}. A resumed "
        f"sandbox will intermittently fail to bind its own port because an "
        f"unrelated outbound connection took it. Keep the range below "
        f"{ephemeral_low}."
    )
    assert low >= FIRST_UNPRIVILEGED_PORT, (
        f"{path.name} publishes sandboxes from {low}, inside the privileged "
        f"port range."
    )
