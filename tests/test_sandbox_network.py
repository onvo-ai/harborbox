"""The sandbox network and its host firewall (DEV-2727).

What a sandbox can actually reach is measured from inside one by
tests/e2e_sandbox_isolation.py. These tests pin the parts that can be checked
without a Docker host: the shape of the network, the rules, and how the guard
behaves when Docker or nft says no.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from harborbox import sandbox_network as sn

if TYPE_CHECKING:
    from collections.abc import Sequence


class FakeNft:
    """Records every nft invocation and answers from a script."""

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(
        self, args: Sequence[str], *, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), stdin))
        return subprocess.CompletedProcess(
            list(args), self.returncode, stdout=self.stdout, stderr=self.stderr
        )


class FakeDocker:
    """A Docker Engine that holds networks in a dict, behind httpx.MockTransport."""

    def __init__(self, networks: dict[str, dict[str, Any]] | None = None) -> None:
        self.networks = networks or {}
        self.requests: list[tuple[str, str]] = []
        self.fail_with: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, json={"message": "daemon says no"})
        if request.method == "GET" and request.url.path.startswith("/networks/"):
            name = request.url.path.removeprefix("/networks/")
            if name not in self.networks:
                return httpx.Response(404, json={"message": "network not found"})
            return httpx.Response(200, json=self.networks[name])
        if request.method == "POST" and request.url.path == "/networks/create":
            body = json.loads(request.content)
            self.networks[body["Name"]] = {**body, "Id": f"id-{body['Name']}", "Containers": {}}
            return httpx.Response(201, json={"Id": f"id-{body['Name']}"})
        if request.method == "DELETE":
            network_id = request.url.path.removeprefix("/networks/")
            self.networks = {
                name: net for name, net in self.networks.items() if net["Id"] != network_id
            }
            return httpx.Response(204)
        return httpx.Response(500)  # pragma: no cover - a request this fake never expects

    def client(self) -> sn.DockerNetworks:
        transport = httpx.MockTransport(self.handler)
        return sn.DockerNetworks(httpx.Client(transport=transport, base_url="http://docker"))


def existing(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401 - Docker's JSON is untyped
    network = {**sn.NetworkSpec().create_body(), "Id": "id-old", "Containers": {}}
    network.update(overrides)
    return network


# --- the network -------------------------------------------------------------


def test_the_bridge_name_fits_a_linux_interface_name() -> None:
    # IFNAMSIZ is 16 including the terminator. Docker rejects a longer name at
    # create time, and the firewall matches on exactly this string.
    assert len(sn.BRIDGE_NAME) <= 15  # noqa: PLR2004


def test_the_network_keeps_a_gateway_so_published_ports_and_egress_work() -> None:
    """The ticket's `inhibit_ipv4` / `internal` fix breaks this deployment.

    Measured on Docker 29: an internal network gets no published ports, and a
    network with `inhibit_ipv4` refuses connections to them. The API reaches
    execd only through a published port, and sandboxes need outbound internet.
    """
    body = sn.NetworkSpec().create_body()
    assert body["Driver"] == "bridge"
    assert body["Internal"] is False
    assert body["EnableIPv6"] is False
    assert "com.docker.network.bridge.inhibit_ipv4" not in body["Options"]
    assert body["Options"]["com.docker.network.bridge.name"] == sn.BRIDGE_NAME
    assert body["Options"]["com.docker.network.bridge.enable_icc"] == "false"


def test_a_missing_network_is_created() -> None:
    docker = FakeDocker()
    assert sn.ensure_network(docker.client()) == "created"
    assert docker.networks[sn.NETWORK_NAME]["Options"] == sn.NETWORK_OPTIONS


def test_a_network_of_the_right_shape_is_left_alone() -> None:
    docker = FakeDocker({sn.NETWORK_NAME: existing(Containers={"abc": {}})})
    assert sn.ensure_network(docker.client()) == "unchanged"
    assert [method for method, _ in docker.requests] == ["GET"]


def test_an_empty_network_of_the_wrong_shape_is_recreated() -> None:
    docker = FakeDocker({sn.NETWORK_NAME: existing(Internal=True)})
    assert sn.ensure_network(docker.client()) == "recreated"
    assert docker.networks[sn.NETWORK_NAME]["Internal"] is False
    assert [method for method, _ in docker.requests] == ["GET", "DELETE", "POST"]


def test_a_wrong_network_with_sandboxes_on_it_is_never_removed() -> None:
    """Removing it would kill running sandboxes; the remedy is a new name."""
    wrong = existing(
        Driver="overlay",
        EnableIPv6=True,
        Options={"com.docker.network.bridge.enable_icc": "true"},
        Containers={"sandbox-1": {}},
    )
    docker = FakeDocker({sn.NETWORK_NAME: wrong})
    with pytest.raises(sn.SandboxNetworkError) as raised:
        sn.ensure_network(docker.client())
    message = str(raised.value)
    assert "1 container(s) attached" in message
    assert "driver is 'overlay'" in message
    assert "IPv6 is enabled" in message
    assert "enable_icc is 'true'" in message
    assert "new NETWORK_NAME" in message
    assert [method for method, _ in docker.requests] == ["GET"]


def test_a_docker_error_is_not_mistaken_for_a_missing_network() -> None:
    docker = FakeDocker()
    docker.fail_with = 500
    with pytest.raises(httpx.HTTPStatusError):
        sn.ensure_network(docker.client())


def test_the_socket_client_talks_to_the_unix_socket() -> None:
    docker = sn.DockerNetworks.over_socket("/nonexistent/docker.sock")
    try:
        with pytest.raises(httpx.TransportError):
            docker.inspect(sn.NETWORK_NAME)
    finally:
        docker.close()


# --- the firewall ------------------------------------------------------------


def test_the_ruleset_replaces_the_table_atomically() -> None:
    lines = sn.render_ruleset().splitlines()
    # Create-if-absent, delete, define: one `nft -f` transaction, no gap.
    assert lines[:3] == [
        f"table inet {sn.NFT_TABLE}",
        f"delete table inet {sn.NFT_TABLE}",
        f"table inet {sn.NFT_TABLE} {{",
    ]


def test_everything_addressed_to_the_host_is_dropped() -> None:
    ruleset = sn.render_ruleset()
    iif = f'iifname "{sn.BRIDGE_NAME}"'
    input_chain = ruleset.split("chain input {", 1)[1].split("}", 1)[0]
    rules = [line.strip() for line in input_chain.strip().splitlines()[1:]]
    # Replies to connections the host opened, then nothing else.
    assert rules == [
        f"{iif} ct state established,related accept",
        f"{iif} counter drop",
    ]


def test_forwarded_traffic_keeps_only_the_internet() -> None:
    ruleset = sn.render_ruleset()
    iif = f'iifname "{sn.BRIDGE_NAME}"'
    forward = ruleset.split("chain forward {", 1)[1]
    assert f'{iif} oifname "{sn.BRIDGE_NAME}" counter drop' in forward
    assert f"{iif} meta nfproto ipv6 counter drop" in forward
    assert f"{iif} ip daddr @blocked_ipv4 counter drop" in forward
    # DNS is let through before the private ranges, so a private resolver works.
    assert forward.index("th dport 53 accept") < forward.index("@blocked_ipv4 counter drop")


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",  # cloud metadata
        "172.17.0.1",  # docker0, where OpenSandbox publishes every execd port
        "10.0.0.4",  # a private network peer
        "192.168.1.1",
        "100.100.100.100",  # CGNAT / tailnet
        "127.0.0.1",
    ],
)
def test_the_blocked_ranges_cover(address: str) -> None:
    ip = ipaddress.ip_address(address)
    assert any(ip in ipaddress.ip_network(net) for net in sn.BLOCKED_IPV4)


@pytest.mark.parametrize("address", ["1.1.1.1", "8.8.8.8", "93.184.215.14"])
def test_the_blocked_ranges_leave_the_internet(address: str) -> None:
    ip = ipaddress.ip_address(address)
    assert not any(ip in ipaddress.ip_network(net) for net in sn.BLOCKED_IPV4)


def test_applying_pipes_the_ruleset_to_nft() -> None:
    nft = FakeNft()
    sn.apply_ruleset(nft)
    assert nft.calls == [(["nft", "-f", "-"], sn.render_ruleset())]


def test_a_refused_ruleset_is_an_error() -> None:
    nft = FakeNft(returncode=1, stderr="Error: Could not process rule\n")
    with pytest.raises(sn.SandboxNetworkError, match="Could not process rule"):
        sn.apply_ruleset(nft)


def test_the_check_passes_on_the_table_as_rendered() -> None:
    # `nft list` prints the definition back, counters added; the rendered
    # table's body stands in for it here.
    listing = sn.render_ruleset().split("\n", 2)[2]
    nft = FakeNft(stdout=listing)
    assert sn.ruleset_installed(nft)
    assert nft.calls == [(["nft", "list", "table", "inet", sn.NFT_TABLE], None)]


def test_the_check_fails_when_the_table_is_gone() -> None:
    assert not sn.ruleset_installed(FakeNft(returncode=1, stderr="No such file"))


def test_the_check_fails_when_a_rule_was_removed_by_hand() -> None:
    listing = sn.render_ruleset().replace(f'iifname "{sn.BRIDGE_NAME}" counter drop\n', "")
    assert not sn.ruleset_installed(FakeNft(stdout=listing))


def test_run_command_returns_the_exit_status_and_output() -> None:
    completed = sn.run_command(["cat"], stdin="hello")
    assert completed.returncode == 0
    assert completed.stdout == "hello"


# --- the service --------------------------------------------------------------


def test_one_tick_creates_the_network_and_applies_the_firewall() -> None:
    docker, nft = FakeDocker(), FakeNft()
    assert sn.guard_once(docker.client(), nft) == "created"
    assert sn.NETWORK_NAME in docker.networks
    assert nft.calls[0][0] == ["nft", "-f", "-"]


def test_the_guard_reapplies_on_every_tick() -> None:
    docker, nft = FakeDocker(), FakeNft()
    slept: list[float] = []
    sn.guard_forever(docker.client(), interval_seconds=7, runner=nft, sleep=slept.append, ticks=3)
    assert len(nft.calls) == 3  # noqa: PLR2004
    assert slept == [7, 7, 7]


def test_main_guard_runs_until_told_to_stop() -> None:
    docker, nft = FakeDocker(), FakeNft()
    status = sn.main(
        ["guard", "--interval", "0"], docker_factory=docker.client, runner=nft, ticks=2
    )
    assert status == 0
    assert len(nft.calls) == 2  # noqa: PLR2004


def test_main_guard_exits_nonzero_when_nft_refuses(caplog: pytest.LogCaptureFixture) -> None:
    docker = FakeDocker()
    status = sn.main(
        ["guard", "--interval", "0"],
        docker_factory=docker.client,
        runner=FakeNft(returncode=1, stderr="Operation not permitted"),
        ticks=1,
    )
    assert status == 1
    assert "Operation not permitted" in caplog.text


def test_main_guard_exits_nonzero_when_docker_refuses() -> None:
    docker = FakeDocker()
    docker.fail_with = 403
    assert sn.main(["guard"], docker_factory=docker.client, runner=FakeNft(), ticks=1) == 1


def test_main_check_reports_the_table() -> None:
    listing = sn.render_ruleset().split("\n", 2)[2]
    assert sn.main(["check"], runner=FakeNft(stdout=listing)) == 0
    assert sn.main(["check"], runner=FakeNft(returncode=1)) == 1
