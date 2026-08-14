"""Verify sandbox readiness for Onvo's workloads. Selected with `pytest -m e2e`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest

from harborbox_sdk import Sandbox, SandboxClient


def timestamp(value: str | None) -> datetime:
    if value is None:
        message = "execution timestamp is missing"
        raise AssertionError(message)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.fixture
def sandbox(client: SandboxClient) -> Iterator[Sandbox]:
    box = client.sandboxes.create(
        template="onvo-lite",
        memory_mb=768,
        cpu=1,
        idle_timeout_seconds=120,
        metadata={"test": "onvo-readiness"},
    )
    try:
        yield box
    finally:
        box.kill()


@pytest.mark.e2e
def test_onvo_sandbox_has_required_python_packages(sandbox: Sandbox) -> None:
    imports = sandbox.commands.run(
        'python -c "import duckdb,pandas,numpy,pymysql,sqlalchemy,psycopg2,'
        "pymongo,pymssql,clickhouse_connect,snowflake.connector,openpyxl,"
        "gspread,google.auth; print('imports-ok')\"",
        timeout=60,
    )
    assert imports.status == "succeeded", imports.error
    assert "imports-ok" in "".join(imports.logs.stdout)


@pytest.mark.e2e
def test_onvo_sandbox_persists_binary_files(sandbox: Sandbox) -> None:
    payload = b"onvo\x00binary\xffpayload"
    assert sandbox.files.write_bytes("/tmp/onvo.bin", payload) == len(payload)
    binary_check = sandbox.commands.run(
        'python -c "from pathlib import Path; '
        "print(Path('/tmp/onvo.bin').read_bytes().hex())\"",
    )
    assert binary_check.status == "succeeded", binary_check.error
    assert payload.hex() in "".join(binary_check.logs.stdout)


@pytest.mark.e2e
def test_onvo_sandbox_runs_duckdb_transform(sandbox: Sandbox) -> None:
    csv_payload = b"region,revenue\nnorth,10\nsouth,20\nnorth,15\n"
    assert sandbox.files.write_bytes("/tmp/data_probe.csv", csv_payload) == len(
        csv_payload
    )
    transform = sandbox.commands.run(
        'python -c "import duckdb,json; '
        "duckdb.execute(\\\"SET memory_limit='400MB'\\\"); "
        'duckdb.execute(\\"CREATE TABLE data AS SELECT * FROM '
        "read_csv_auto('/tmp/data_probe.csv')\\\"); "
        'rows=duckdb.execute(\\"SELECT region, SUM(revenue) total '
        'FROM data GROUP BY region ORDER BY region\\").fetchall(); '
        'print(json.dumps(rows))"',
        timeout=30,
    )
    assert transform.status == "succeeded", transform.error
    assert '[["north", 25], ["south", 20]]' in "".join(transform.logs.stdout)


@pytest.mark.e2e
def test_onvo_sandbox_has_network_egress(sandbox: Sandbox) -> None:
    network = sandbox.commands.run(
        'python -c "import socket; '
        "ip=socket.gethostbyname('example.com'); "
        "s=socket.create_connection((ip,443),5); s.close(); print('egress-ok')\"",
        timeout=10,
    )
    assert network.status == "succeeded", network.error
    assert "egress-ok" in "".join(network.logs.stdout)


@pytest.mark.e2e
def test_onvo_sandbox_runs_commands_in_parallel(sandbox: Sandbox) -> None:
    first = sandbox.commands.run("sleep 2; echo first", wait=False, timeout=10)
    second = sandbox.commands.run("sleep 2; echo second", wait=False, timeout=10)
    first.wait(timeout=15, raise_on_error=True)
    second.wait(timeout=15, raise_on_error=True)
    assert timestamp(first.started_at) < timestamp(second.finished_at)
    assert timestamp(second.started_at) < timestamp(first.finished_at)


@pytest.mark.e2e
def test_onvo_sandbox_idle_timeout_is_configurable(sandbox: Sandbox) -> None:
    sandbox.set_timeout(180_000)
    assert sandbox.idle_timeout_seconds == 180
    assert sandbox.refresh().status == "running"
