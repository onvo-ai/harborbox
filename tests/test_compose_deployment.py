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
            key[len(prefix) :].lower(): val
            for key, val in env.items()
            if key.startswith(prefix)
        }
    )


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


def test_onvo_pro_sizing_fits_the_configured_cpu_budget(settings: Settings) -> None:
    """onvo-pro's built-in default is 2.0 CPU, which halves concurrency here.

    Not a correctness bug, which is exactly why it would go unnoticed: the box
    just quietly serves half as many widgets at once.
    """
    per_sandbox = settings.onvo_pro_template_cpu
    budget = settings.max_parallel_cpu or 0
    min_concurrency = 8  # this host has run 8 onvo-pro widgets at once.

    assert budget / per_sandbox >= min_concurrency, (
        f"{budget} CPU budget at {per_sandbox} per onvo-pro sandbox allows only "
        f"{budget / per_sandbox:.0f} concurrent; this host has run {min_concurrency}."
    )


@pytest.mark.parametrize("template", ["relaydeck", "onvo-pro", "onvo-lite"])
def test_no_template_starts_a_long_lived_process(template: str) -> None:
    """Every sandbox idles until asked to do something.

    Templates that ran Python used to boot a Jupyter server here, because execd
    runs bash itself but proxied Python to a server nothing else would start.
    Measured (scripts/bench/), that server cost ~3 s of boot and ~197 MB
    resident in every sandbox, to serve one endpoint that now runs Python as an
    ordinary command. A template that grows a bootstrap process again should
    have to justify it against those numbers.
    """
    assert Settings().entrypoint_for_template(template) == ["tail", "-f", "/dev/null"]


def test_no_template_image_installs_a_python_kernel() -> None:
    """The kernel stack is gone from the images, not just unused by the code.

    Leaving jupyter-server and ipykernel installed would keep paying the image
    size and the CVE surface for a path nothing takes, and would let the
    entrypoint quietly regrow a server that appears to work.
    """
    sandbox_dir = Path(__file__).resolve().parent.parent / "sandbox"
    template_requirements = [
        sandbox_dir / "requirements-onvo-pro.txt",
        sandbox_dir / "requirements-onvo-lite.txt",
        sandbox_dir / "requirements-relaydeck.txt",
    ]
    for requirements in template_requirements:
        installed = [
            line.split("#", 1)[0].strip().lower()
            for line in requirements.read_text().splitlines()
        ]
        for forbidden in ("jupyter-server", "ipykernel", "jupyter-client"):
            assert not any(line.startswith(forbidden) for line in installed), (
                f"{requirements.name} still installs {forbidden}; "
                "no template runs a kernel any more"
            )


def test_forkrun_is_installed_in_the_sandbox_image() -> None:
    """The pre-warmed script runner is what makes a batch fast.

    `import pandas` costs ~1.5s and a batch runs eight scripts in one sandbox.
    The client guards with `[ -f /opt/forkrun.py ]`, so dropping it does not
    fail anything — widgets just quietly pay the import every time.
    """
    dockerfile = (
        Path(__file__).resolve().parent.parent / "sandbox" / "Dockerfile"
    ).read_text()

    assert "COPY sandbox/forkrun.py /opt/forkrun.py" in dockerfile


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
