"""Measures what a caller's build step can reach. Selected with `pytest -m e2e`.

The unit tests in test_compose_deployment.py read compose files. That is worth
doing and it is not sufficient, which is the whole reason this file exists:
`test_the_builder_reaches_the_registry_and_nothing_else` asserted for months
that the build network held exactly the builder and the registry, stayed green
the entire time, and the deployed stack was open anyway. Coolify appends its
project network to every service of an application, a build step runs inside
buildkitd's network namespace, and so from inside a build on the infrastructure
host:

    api:8000/health   -> {"status":"ok"}
    opensandbox:8080  -> HTTP/1.1 401 Unauthorized
    postgres:5432     -> OPEN

No amount of reading the compose file finds that. Only opening a socket from
inside a real build step does, so that is what this does: it builds a template
whose Dockerfile probes the control plane, and reads the results out of a
sandbox created from the resulting image.

The probe reaches for the registry as well as for the control plane, and that
control is the load-bearing part of the design here. A test that only asserts
"nothing was reachable" passes just as well when the probe itself is broken --
when `timeout` is missing from the base image, when the port syntax is wrong,
when DNS is dead for an unrelated reason. Requiring `registry:5000` to answer
means a broken probe fails the test instead of quietly passing it.

Run against the split stack:

    docker network create harborbox-build
    ./scripts/gen-buildkit-certs.sh
    docker compose -f compose.builder.yaml up -d
    docker compose up -d
    HARBORBOX_API_KEY=... uv run pytest -m e2e tests/e2e_build_isolation.py
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from live_client import SandboxClient

# Every service the control plane runs, by the name Docker's embedded DNS would
# give it, plus the aliases the deployed file adds. If a build step can open a
# socket to any of these, the boundary section 10.3 of
# docs/arbitrary-dockerfile-templates.md describes does not exist.
CONTROL_PLANE = (
    "api:8000",
    "harborbox-api:8000",
    "harborbox:8000",
    "postgres:5432",
    "opensandbox:8080",
)
# The one thing a build step is supposed to reach, and the reason a green run
# here means something: it proves the probe works.
REGISTRY = "registry:5000"

PROBE_TARGETS = (REGISTRY, *CONTROL_PLANE)

# bash's /dev/tcp, because debian:bookworm-slim ships no curl or netcat and
# adding one would make the probe depend on the network being up before it can
# test whether the network is up. `timeout` bounds a target that blackholes
# rather than refusing -- an unreachable bridge address does not answer at all.
PROBE = """FROM debian:bookworm-slim
# Cache buster, and it is not optional: BuildKit caches the RUN below, so
# without a value that changes per run a second invocation replays the *first*
# run's measurement. A test that reports last week's topology as today's is
# worse than no test -- and the first draft of this file did exactly that, in
# 4.4 seconds, which is how it was noticed.
ENV PROBE_RUN={run_id}
RUN for target in {targets}; do \\
      host="${{target%%:*}}"; port="${{target##*:}}"; \\
      if timeout 4 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null; then \\
        echo "$target REACHED"; \\
      else \\
        echo "$target unreachable"; \\
      fi; \\
    done > /probe.txt; cat /probe.txt
"""

BUILD_TIMEOUT_SECONDS = 300


def build_probe_template(client: SandboxClient) -> str:
    dockerfile = PROBE.format(
        run_id=time.time_ns(), targets=" ".join(PROBE_TARGETS)
    )
    created = client._request("POST", "/v1/templates", json={"dockerfile": dockerfile})
    name = created["name"]

    deadline = time.monotonic() + BUILD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        template = client._request("GET", f"/v1/templates/{name}")
        if template["status"] == "ready":
            return str(name)
        if template["status"] == "failed":
            pytest.fail(f"the probe template failed to build: {template['error']}")
        time.sleep(2)
    pytest.fail(f"the probe template was still building after {BUILD_TIMEOUT_SECONDS}s")


@pytest.mark.e2e
def test_a_build_step_reaches_the_registry_and_nothing_else(
    client: SandboxClient,
) -> None:
    """The claim in section 10.3, measured rather than read off a compose file."""
    name = build_probe_template(client)
    sandbox = client.sandboxes.create(template=name)
    try:
        probe = sandbox.commands.run("cat /probe.txt", wait_timeout=120)
        assert probe.exit_code == 0, probe.error
        # `logs.stdout`, not `.text`: `.text` is for rich results from Python
        # execution, and a shell command leaves nothing there.
        stdout = "\n".join(probe.logs.stdout)
        results = dict(line.rsplit(" ", 1) for line in stdout.strip().splitlines())
        print("\nreachable from inside a build step:")  # noqa: T201 - the measurement
        for target, outcome in results.items():
            print(f"  {target:<24} {outcome}")  # noqa: T201

        # The control first: a probe that cannot reach anything proves nothing.
        assert results.get(REGISTRY) == "REACHED", (
            f"the build step could not reach the registry either, so this run "
            f"measured nothing: {results}"
        )
        reached = {target for target, outcome in results.items() if outcome == "REACHED"}
        assert reached == {REGISTRY}, (
            f"a caller's build step reached the control plane: {sorted(reached)}"
        )
    finally:
        sandbox.kill()
        client._request("DELETE", f"/v1/templates/{name}")
