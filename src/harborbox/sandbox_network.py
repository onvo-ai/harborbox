"""The network sandboxes run on, and the host firewall that fences it in.

Run on the Docker host itself (host network namespace, CAP_NET_ADMIN, the
Docker socket) as the `sandbox-guard` Compose service:

    python -m harborbox.sandbox_network guard   # create the network, fence it, keep it fenced
    python -m harborbox.sandbox_network check   # the service's healthcheck

Why this exists (DEV-2727). OpenSandbox used to start every sandbox on Docker's
default `bridge`, and a container on a bridge has the host's bridge address as
its gateway. Measured from inside a sandbox, that reached:

  - any host listener bound to 0.0.0.0, via the gateway or any other host
    address (the host's public and private IPs are all local to it);
  - every *other* sandbox's execd -- directly over the bridge, and through the
    port OpenSandbox publishes for it on the gateway. execd takes no token, so
    that is running commands in another caller's sandbox;
  - 169.254.169.254, and every private range the host routes to.

The fix the ticket proposed -- `com.docker.network.bridge.inhibit_ipv4` on an
`internal: true` network -- does not fit this deployment, and was measured not
to: an internal network gets no published ports at all, and on a network with
no host address the published ports refuse every connection. The API reaches
execd *only* through those published ports (`host.docker.internal:<port>`, see
HARBORBOX_OPENSANDBOX_USE_SERVER_PROXY in the compose files), and sandboxes
have outbound internet by design (tests/e2e_onvo_readiness.py, Toolspoke's
stdio tools). Both would break.

So sandboxes keep a gateway, on a bridge of their own whose interface name is
fixed, and the host drops what arrives from that interface unless it is
outbound internet:

  - `input`: everything addressed to the host itself is dropped, except replies
    to connections the host opened. This covers the gateway, docker0, the
    public and private addresses, and published ports handled by docker-proxy.
  - `forward`: hairpin back onto the sandbox bridge, IPv6, and every private,
    link-local, loopback, CGNAT and multicast destination are dropped. Docker
    DNATs a published port in `prerouting`, before this hook, so a sandbox
    dialling another sandbox's published execd port is dropped here.
  - DNS (port 53) is let through `forward` so a sandbox still resolves names
    when the host's upstream resolver sits on a private address.

Connections *into* a sandbox -- the API dialling execd through its published
port -- are untouched: they do not arrive from the sandbox bridge, and their
replies are `established`.

The table is nftables, not iptables, so it works under either of Docker's
firewall backends: a `drop` in any base chain is final for the packet, whatever
else is hooked there. It is replaced atomically and re-applied on every tick, so
a firewall reload on the host heals within one interval.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

# Versioned, because Docker never changes an existing network's options: a
# change to NETWORK_OPTIONS below ships as a new name (and a new bridge name),
# so sandboxes still running on the old network are not pulled out from under
# their callers. tests/test_compose_deployment.py pins the opensandbox
# `network_mode` in both compose files to this name.
NETWORK_NAME = "harborbox-sandbox-v1"
# The Linux interface name the firewall matches on. 15 characters at most
# (IFNAMSIZ); Docker would otherwise name it br-<random id>.
BRIDGE_NAME = "hbx-sandbox-v1"
NETWORK_LABEL = "ai.onvo.harborbox.network"
NETWORK_OPTIONS: dict[str, str] = {
    "com.docker.network.bridge.name": BRIDGE_NAME,
    # Sandboxes are different callers' code. No direct path between them.
    "com.docker.network.bridge.enable_icc": "false",
    # Outbound internet is a product requirement; see the module docstring.
    "com.docker.network.bridge.enable_ip_masquerade": "true",
}
NFT_TABLE = "harborbox_sandbox_guard"
# Everything a sandbox has no business reaching through the host. The host's
# own addresses are covered by the `input` chain whatever range they are in.
BLOCKED_IPV4: tuple[str, ...] = (
    "0.0.0.0/8",  # "this network"
    "10.0.0.0/8",  # RFC 1918
    "100.64.0.0/10",  # CGNAT, and Tailscale's tailnet addresses
    "127.0.0.0/8",  # loopback
    "169.254.0.0/16",  # link-local: cloud metadata at 169.254.169.254
    "172.16.0.0/12",  # RFC 1918: every Docker bridge by default
    "192.0.0.0/24",  # IETF protocol assignments
    "192.168.0.0/16",  # RFC 1918
    "198.18.0.0/15",  # benchmarking
    "224.0.0.0/3",  # multicast and reserved, broadcast included
)
DEFAULT_INTERVAL_SECONDS = 30.0
DOCKER_SOCKET = "/var/run/docker.sock"


class SandboxNetworkError(RuntimeError):
    """The sandbox network cannot be brought to the shape this module requires."""


class CommandRunner(Protocol):
    def __call__(
        self, args: Sequence[str], *, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]: ...


def run_command(
    args: Sequence[str], *, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        list(args), input=stdin, capture_output=True, text=True, check=False, timeout=30
    )


def render_ruleset() -> str:
    """Return an nft script that atomically replaces the guard table.

    The leading `table` line creates the table when it is absent so that the
    `delete` never fails; `nft -f` applies the whole file as one transaction,
    so there is no moment with the old rules gone and the new ones not yet in.
    """
    iif = f'iifname "{BRIDGE_NAME}"'
    blocked = ", ".join(BLOCKED_IPV4)
    return f"""table inet {NFT_TABLE}
delete table inet {NFT_TABLE}
table inet {NFT_TABLE} {{
  set blocked_ipv4 {{
    type ipv4_addr
    flags interval
    elements = {{ {blocked} }}
  }}

  chain input {{
    type filter hook input priority filter - 10; policy accept;
    {iif} ct state established,related accept
    {iif} counter drop
  }}

  chain forward {{
    type filter hook forward priority filter - 10; policy accept;
    {iif} ct state established,related accept
    {iif} oifname "{BRIDGE_NAME}" counter drop
    {iif} meta nfproto ipv6 counter drop
    {iif} meta l4proto {{ tcp, udp }} th dport 53 accept
    {iif} ip daddr @blocked_ipv4 counter drop
  }}
}}
"""


def apply_ruleset(runner: CommandRunner = run_command) -> None:
    completed = runner(["nft", "-f", "-"], stdin=render_ruleset())
    if completed.returncode != 0:
        message = f"nft refused the sandbox guard ruleset: {completed.stderr.strip()}"
        raise SandboxNetworkError(message)


def ruleset_installed(runner: CommandRunner = run_command) -> bool:
    """Whether the guard table is in the kernel and still fences the bridge."""
    completed = runner(["nft", "list", "table", "inet", NFT_TABLE])
    if completed.returncode != 0:
        return False
    listing = completed.stdout
    expected = render_ruleset().count(f'iifname "{BRIDGE_NAME}"')
    return (
        "hook input" in listing
        and "hook forward" in listing
        # Fewer rules than rendered means someone edited the table by hand.
        and listing.count(f'iifname "{BRIDGE_NAME}"') >= expected
    )


@dataclass(frozen=True)
class NetworkSpec:
    name: str = NETWORK_NAME
    options: dict[str, str] = field(default_factory=lambda: dict(NETWORK_OPTIONS))

    def create_body(self) -> dict[str, Any]:
        return {
            "Name": self.name,
            "Driver": "bridge",
            "Internal": False,
            "EnableIPv6": False,
            "Options": self.options,
            "Labels": {NETWORK_LABEL: "sandbox"},
        }

    def mismatches(self, network: dict[str, Any]) -> list[str]:
        """Describe every way an existing network differs from this spec."""
        problems: list[str] = []
        if network.get("Driver") != "bridge":
            problems.append(f"driver is {network.get('Driver')!r}, not 'bridge'")
        if network.get("Internal"):
            problems.append("it is internal, so Docker publishes no ports on it")
        if network.get("EnableIPv6"):
            problems.append("IPv6 is enabled")
        options = network.get("Options") or {}
        problems.extend(
            f"option {key} is {options.get(key)!r}, not {value!r}"
            for key, value in self.options.items()
            if options.get(key) != value
        )
        return problems


class DockerNetworks:
    """The four Docker Engine calls this module needs, over the unix socket."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    @classmethod
    def over_socket(cls, socket_path: str = DOCKER_SOCKET) -> DockerNetworks:
        transport = httpx.HTTPTransport(uds=socket_path)
        return cls(httpx.Client(transport=transport, base_url="http://docker", timeout=30))

    def inspect(self, name: str) -> dict[str, Any] | None:
        response = self._client.get(f"/networks/{name}")
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        response.raise_for_status()
        network: dict[str, Any] = response.json()
        return network

    def create(self, body: dict[str, Any]) -> None:
        self._client.post("/networks/create", json=body).raise_for_status()

    def remove(self, network_id: str) -> None:
        self._client.delete(f"/networks/{network_id}").raise_for_status()

    def close(self) -> None:
        self._client.close()


def ensure_network(docker: DockerNetworks, spec: NetworkSpec | None = None) -> str:
    """Create the sandbox network, or confirm the existing one is the right shape.

    Returns what was done: "created", "unchanged" or "recreated". A network
    with the wrong options is recreated only while nothing is attached to it.
    With sandboxes on it, removing it would kill them, so this refuses and
    says so instead -- the remedy is a new NETWORK_NAME, not a forced removal.
    """
    spec = spec or NetworkSpec()
    network = docker.inspect(spec.name)
    if network is None:
        docker.create(spec.create_body())
        logger.info("created sandbox network %s", spec.name)
        return "created"
    problems = spec.mismatches(network)
    if not problems:
        return "unchanged"
    attached = network.get("Containers") or {}
    if attached:
        message = (
            f"Docker network {spec.name} exists with the wrong shape "
            f"({'; '.join(problems)}) and {len(attached)} container(s) attached. "
            "Docker cannot change an existing network's options. Ship the new "
            "options under a new NETWORK_NAME rather than removing this one."
        )
        raise SandboxNetworkError(message)
    docker.remove(str(network["Id"]))
    docker.create(spec.create_body())
    logger.warning("recreated sandbox network %s: %s", spec.name, "; ".join(problems))
    return "recreated"


def guard_once(docker: DockerNetworks, runner: CommandRunner = run_command) -> str:
    """Bring the network and the firewall to the required state, once."""
    outcome = ensure_network(docker)
    apply_ruleset(runner)
    return outcome


def guard_forever(
    docker: DockerNetworks,
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    runner: CommandRunner = run_command,
    sleep: Callable[[float], None] = time.sleep,
    ticks: int | None = None,
) -> None:
    """Re-apply on every tick. Any failure propagates, so the container exits.

    Exiting is the point: the service restarts, its healthcheck goes red, and
    the deploy that brought it up does not report success. `ticks` bounds the
    loop for tests; the service runs it unbounded.
    """
    done = 0
    while ticks is None or done < ticks:
        guard_once(docker, runner)
        done += 1
        sleep(interval_seconds)


def main(
    argv: Sequence[str] | None = None,
    *,
    docker_factory: Callable[[], DockerNetworks] = DockerNetworks.over_socket,
    runner: CommandRunner = run_command,
    ticks: int | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m harborbox.sandbox_network")
    sub = parser.add_subparsers(dest="command", required=True)
    guard = sub.add_parser("guard", help="create the network and keep the firewall applied")
    guard.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    sub.add_parser("check", help="exit 0 only while the firewall is installed")
    args = parser.parse_args(argv)

    if args.command == "check":
        return 0 if ruleset_installed(runner) else 1

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # One line per Docker call every tick is noise; failures still surface.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    docker = docker_factory()
    try:
        guard_forever(docker, interval_seconds=args.interval, runner=runner, ticks=ticks)
    except (SandboxNetworkError, httpx.HTTPError) as exc:
        logger.error("sandbox guard failed: %s", exc)  # noqa: TRY400 - the message is the diagnosis
        return 1
    finally:
        docker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
