"""The deployed compose must agree with what the code actually does.

This exists because of a real outage. `compose.internal-tools.yaml` never set
`HARBORBOX_RUNTIME_PROVIDER`, which was harmless for as long as the API
constructed `DockerRuntime` directly in its lifespan and never read the setting.
0.2.0 introduced `create_runtime()`, which honours it — and its default is
`opensandbox`. The deploy came up healthy, reported the right version, and
listed all three templates as ready, while every single sandbox creation died at
`[Errno -3] Temporary failure in name resolution` dialling a service that does
not run on that host.

Every check that could be done without creating a sandbox passed. So the thing
worth pinning is not the code and not the YAML in isolation, but the agreement
between them: feed the compose file's own environment into `Settings` and assert
the resulting behaviour matches what the host can actually provide.
"""

import re
from pathlib import Path

import pytest
import yaml

from harborbox.config import Settings

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


def test_the_configured_runtime_provider_is_actually_provided(
    compose: dict, settings: Settings
) -> None:
    """The outage, pinned — and its mirror image.

    Both halves of this bit once, in opposite directions:

    `opensandbox` needs a server to talk to. The file did not run one, and the
    API dialled a hostname that did not resolve, so no container was ever
    created.

    `docker` needs images that answer on :8080. 0.2.0's templates ship no
    agent — no fastapi, no uvicorn, `CMD ["tail","-f","/dev/null"]` — so
    switching the flag without changing the images produces sandboxes that
    start, sit there, and fail every execution with "agent did not become
    ready". That looks like a network fault and is not one.
    """
    if settings.runtime_provider == "opensandbox":
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
            f"runtime_provider is opensandbox and the API dials {host!r}, but "
            f"no service or alias in {COMPOSE.name} answers to that name."
        )
    else:
        agent_deps = Path(__file__).resolve().parent.parent / "sandbox"
        for req in agent_deps.glob("requirements-*.txt"):
            body = req.read_text()
            assert "uvicorn" in body, (
                f"runtime_provider is docker, which polls a harborbox agent on "
                f":8080, but {req.name} installs no uvicorn to serve it."
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
    from harborbox.schemas import SandboxCreate

    assert SandboxCreate.model_fields["egress"].default is False


def test_onvo_pro_sizing_fits_the_configured_cpu_budget(settings: Settings) -> None:
    """onvo-pro's built-in default is 2.0 CPU, which halves concurrency here.

    Not a correctness bug, which is exactly why it would go unnoticed: the box
    just quietly serves half as many widgets at once.
    """
    per_sandbox = settings.onvo_pro_template_cpu
    budget = settings.max_parallel_cpu or 0

    assert budget / per_sandbox >= 8, (
        f"{budget} CPU budget at {per_sandbox} per onvo-pro sandbox allows only "
        f"{budget / per_sandbox:.0f} concurrent; this host has run 8."
    )


def test_python_kernelspec_is_registered_under_the_language_name() -> None:
    """execd looks a kernel up by the language it was asked for.

    The SDK's default context language is `python`, and execd resolves that
    against Jupyter's kernelspec *names*. `ipykernel install` registers
    `python3` only, so execd fetched `/api/kernelspecs`, got a 200 listing
    python3, found nothing called `python`, and reported `no kernel specs
    found` — which reads like Jupyter is missing and is really a name mismatch.
    Registering both names is what made execution work.
    """
    dockerfile = (
        Path(__file__).resolve().parent.parent / "sandbox" / "Dockerfile"
    ).read_text()

    assert "--name python3" in dockerfile
    assert "--name python " in dockerfile, (
        "only python3 is registered; execd asks for a kernel named 'python' "
        "and will report 'no kernel specs found'"
    )


def test_jupyter_flags_live_in_the_entrypoint_not_only_the_image() -> None:
    """opensandbox discards the image CMD and runs the create request's list.

    Flags added to the Dockerfile alone silently do nothing, which cost an
    afternoon: token auth stayed on because `--IdentityProvider.token=` was in
    the CMD and not in what actually ran.
    """
    from harborbox.config import Settings

    entrypoint = Settings().entrypoint_for_template("onvo-pro")

    assert entrypoint[0].endswith("jupyter")
    assert "--IdentityProvider.token=" in entrypoint


def test_sandbox_readiness_does_not_wait_for_a_jupyter_kernel() -> None:
    """Readiness must not include the kernel, because Onvo never uses it.

    Onvo's client uploads a script and runs it through
    /v1/sandboxes/{id}/commands, reading only stdout — the Jupyter path is
    never touched. Waiting for a kernel in `wait_until_ready` therefore charged
    every sandbox ~6-11s of Jupyter boot and kernel spawn for a capability the
    only caller does not use, and turned a dashboard refresh from ~15s a batch
    into ~190s.

    `execute_code` still waits, so callers that do run Python through a kernel
    get a clear failure rather than a race.
    """
    import inspect

    from harborbox.opensandbox_runtime import OpenSandboxRuntime

    ready = inspect.getsource(OpenSandboxRuntime.wait_until_ready)
    execute = inspect.getsource(OpenSandboxRuntime.execute_code)

    assert "_wait_python_ready" not in ready, (
        "wait_until_ready waits for a Jupyter kernel again; that cost is paid "
        "by every sandbox, including the ones that only ever run commands."
    )
    assert "_wait_python_ready" in execute


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
