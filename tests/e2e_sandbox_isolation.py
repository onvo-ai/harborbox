"""Measures what a sandbox can reach on its own host. Selected with `pytest -m e2e`.

DEV-2727. Until it, OpenSandbox started every sandbox on Docker's default
`bridge`, and from inside one -- the same code path widget Python takes -- all
of these answered:

    host listener on 0.0.0.0, via the gateway 172.17.0.1    CONNECTED
    the same listener via the host's own address             CONNECTED
    169.254.169.254:80                                       CONNECTED
    another sandbox's execd, at its bridge address           CONNECTED
    another sandbox's execd, at its port published on docker0 CONNECTED

execd takes no token, so the last two are running commands in another caller's
sandbox. Sandboxes now run on `harborbox-sandbox-v1`, which the sandbox-guard
service fences (src/harborbox/sandbox_network.py), and every line above has to
fail -- while the API keeps driving both sandboxes through their published
execd ports, and e2e_onvo_readiness keeps proving outbound internet.

The host side is set up with the Docker CLI against the stack's own daemon,
since only the daemon's host can hold a "host listener": a container on the
host network namespace listens on 0.0.0.0. Two controls keep a broken probe
from passing as isolation, in the manner of e2e_build_isolation.py: that
listener must answer an ordinary container on the default bridge, and the probe
must connect to the sandbox's own execd on loopback.

    docker compose up -d --wait
    HARBORBOX_API_KEY=... uv run pytest -m e2e tests/e2e_sandbox_isolation.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import TYPE_CHECKING, Any

import pytest

from harborbox.sandbox_network import NETWORK_NAME

if TYPE_CHECKING:
    from collections.abc import Iterator

    from live_client import Sandbox, SandboxClient

# Has Python, and is already on every host that runs the stack: both compose
# files run opensandbox from it.
LISTENER_IMAGE = "opensandbox/server:release-1.1.1"
LISTENER_NAME = "harborbox-e2e-host-listener"
# Unprivileged, and outside both the kernel's ephemeral range and every
# sandbox port range the compose files publish from.
LISTENER_PORT = 19555
EXECD_PORT = 44772
METADATA = "169.254.169.254"
CONNECT_TIMEOUT_SECONDS = 4

# bash's /dev/tcp opens a real TCP connection. `timeout` turns a dropped SYN
# into an answer instead of a hang. The base image is debian-slim: bash and
# coreutils, no Python.
PROBE = (
    "if timeout {timeout} bash -c 'exec 3<>/dev/tcp/{host}/{port}' 2>/dev/null; "
    "then echo CONNECTED; else echo BLOCKED; fi"
)


def docker(*args: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed argv
        [_docker_cli(), *args], capture_output=True, text=True, check=False, timeout=120
    )
    if completed.returncode != 0:
        message = f"docker {' '.join(args)} failed: {completed.stderr.strip()}"
        raise AssertionError(message)
    return completed.stdout.strip()


def _docker_cli() -> str:
    path = shutil.which("docker")
    if path is None:
        pytest.fail("the sandbox isolation suite drives the host with the docker CLI")
    return path


def gateway_of(network: str) -> str:
    return docker("network", "inspect", network, "--format", "{{(index .IPAM.Config 0).Gateway}}")


def probe(sandbox: Sandbox, host: str, port: int) -> str:
    result = sandbox.commands.run(
        PROBE.format(timeout=CONNECT_TIMEOUT_SECONDS, host=host, port=port), timeout=30
    )
    assert result.status == "succeeded", result.error
    return "".join(result.logs.stdout).strip()


def container_of(sandbox: Sandbox) -> dict[str, Any]:
    container_id = docker(
        "ps", "-q", "--filter", f"label=harborbox.sandbox_id={sandbox.id}"
    ).splitlines()
    assert len(container_id) == 1, f"expected one container for {sandbox.id}: {container_id}"
    inspected: list[dict[str, Any]] = json.loads(docker("inspect", container_id[0]))
    return inspected[0]


def new_sandbox(client: SandboxClient, label: str) -> Sandbox:
    box = client.sandboxes.create(
        template="base",
        memory_mb=256,
        cpu=0.5,
        idle_timeout_seconds=300,
        metadata={"test": f"sandbox-isolation-{label}"},
    )
    # Sandboxes start lazily; the first command is what puts a container on
    # the host, and it doubles as proof the API can drive this one.
    started = box.commands.run("echo ready", timeout=30)
    assert started.status == "succeeded", started.error
    return box


@pytest.fixture
def host_listener() -> Iterator[int]:
    if docker("ps", "-aq", "--filter", f"name=^{LISTENER_NAME}$"):
        docker("rm", "-f", LISTENER_NAME)  # left over from an interrupted run
    docker(
        "run", "-d", "--rm", "--name", LISTENER_NAME, "--network", "host",
        "--entrypoint", "python", LISTENER_IMAGE,
        # 0.0.0.0 is the point: the exposure is a host listener on every address.
        "-m", "http.server", str(LISTENER_PORT), "--bind", "0.0.0.0",  # noqa: S104
    )  # fmt: skip
    try:
        yield LISTENER_PORT
    finally:
        docker("rm", "-f", LISTENER_NAME)


@pytest.fixture
def sandbox(client: SandboxClient) -> Iterator[Sandbox]:
    box = new_sandbox(client, "probe")
    try:
        yield box
    finally:
        box.kill()


@pytest.fixture
def sibling(client: SandboxClient) -> Iterator[Sandbox]:
    box = new_sandbox(client, "sibling")
    try:
        yield box
    finally:
        box.kill()


@pytest.mark.e2e
def test_sandboxes_run_on_the_guarded_network(sandbox: Sandbox) -> None:
    networks = container_of(sandbox)["NetworkSettings"]["Networks"]
    assert set(networks) == {NETWORK_NAME}


@pytest.mark.e2e
def test_a_sandbox_cannot_reach_a_host_listener(sandbox: Sandbox, host_listener: int) -> None:
    sandbox_gateway = gateway_of(NETWORK_NAME)
    docker0 = gateway_of("bridge")

    # Control: the listener is up and answers an ordinary container on the
    # default bridge. Polled because `docker run -d` returns before it binds.
    for _ in range(20):
        if _listening(docker0, host_listener):
            break
        time.sleep(0.5)
    else:
        pytest.fail("the host listener never answered; this test would be vacuous")
    # Control: the probe itself works from inside the sandbox.
    assert probe(sandbox, "127.0.0.1", EXECD_PORT) == "CONNECTED"

    assert probe(sandbox, sandbox_gateway, host_listener) == "BLOCKED"
    assert probe(sandbox, docker0, host_listener) == "BLOCKED"


def _listening(host: str, port: int) -> bool:
    """Whether the listener accepts yet, asked from an unguarded container."""
    completed = subprocess.run(  # noqa: S603 - fixed argv
        [
            _docker_cli(), "run", "--rm", "--network", "bridge", "--entrypoint", "python",
            LISTENER_IMAGE, "-c",
            "import socket,sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 2)",
            host, str(port),
        ],
        capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
    return completed.returncode == 0


@pytest.mark.e2e
def test_a_sandbox_cannot_reach_the_metadata_address(sandbox: Sandbox) -> None:
    assert probe(sandbox, METADATA, 80) == "BLOCKED"


@pytest.mark.e2e
def test_a_sandbox_cannot_reach_another_sandboxes_execd(sandbox: Sandbox, sibling: Sandbox) -> None:
    attrs = container_of(sibling)
    direct = attrs["NetworkSettings"]["Networks"][NETWORK_NAME]["IPAddress"]
    published = attrs["NetworkSettings"]["Ports"][f"{EXECD_PORT}/tcp"][0]

    assert probe(sandbox, direct, EXECD_PORT) == "BLOCKED"
    assert probe(sandbox, published["HostIp"], int(published["HostPort"])) == "BLOCKED"

    # And the API, which reaches execd through that same published port, still
    # drives both of them.
    for box in (sandbox, sibling):
        result = box.commands.run("echo still-driven", timeout=30)
        assert result.status == "succeeded", result.error
        assert "still-driven" in "".join(result.logs.stdout)


@pytest.mark.e2e
def test_a_sandbox_has_no_ipv6_route_out(sandbox: Sandbox) -> None:
    result = sandbox.commands.run(
        "cat /proc/net/if_inet6 2>/dev/null | awk '{print $1}'", timeout=30
    )
    assert result.status == "succeeded", result.error
    addresses = "".join(result.logs.stdout).split()
    # Loopback (::1) and link-local (fe80::/10) only: nothing routable.
    routable = [a for a in addresses if a != "0" * 31 + "1" and not a.startswith("fe80")]
    assert routable == [], routable
