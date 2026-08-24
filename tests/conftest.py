from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest
from live_client import SandboxClient

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def client() -> Iterator[SandboxClient]:
    """Build a live-stack HTTP client. Only meaningful for tests marked `e2e`."""
    api_key = os.environ.get("HARBORBOX_API_KEY")
    if not api_key:
        pytest.fail("HARBORBOX_API_KEY is required for e2e tests")
    with SandboxClient(api_key=api_key) as live:
        yield live


# The data stack the e2e suite exercises.
#
# This used to be `template="onvo-lite"`, a template `templates/manifest.yaml`
# declared and Harborbox built. "Build images from Dockerfiles, and only from
# Dockerfiles" (e00f81a) deleted every per-product template: products own a
# Dockerfile and POST it, and the manifest keeps exactly one entry, `base`, to
# start FROM and to give the warm pool somewhere to live. That commit updated
# the unit tests and left the e2e suite naming a template that no longer
# exists, so every sandbox it created returned 422 -- invisibly, because the
# runners were accepting no jobs at the time (DEV-1971).
#
# So the suite builds its own, the same way a product does. The package list is
# a copy of the deleted `sandbox/requirements-onvo-lite.txt`, kept because the
# readiness tests import each of these by name. It is a copy on purpose: the
# point of e00f81a was that this repository should not have to know which
# pandas version Onvo wants, and a fixture asserting "a data-stack image builds
# and runs" is not the same coupling as a product template shipped in the
# manifest. If Onvo changes its own image, nothing here needs to follow.
DATA_STACK_DOCKERFILE = """FROM python:3.12-slim-bookworm
RUN pip install --no-cache-dir \
      duckdb \
      pandas==2.2.3 \
      numpy \
      "pymysql>=1.1.0" \
      cryptography \
      sqlalchemy \
      psycopg2-binary \
      pymongo \
      pymssql \
      clickhouse-connect \
      snowflake-connector-python \
      openpyxl \
      gspread \
      google-auth
# The old onvo-lite image kept its interpreter at /opt/venv/bin/python and the
# suite still addresses it absolutely -- opensandbox runs commands through its
# own bootstrap, so a bare `python` depends on whatever PATH that bootstrap
# happens to export. Both spellings resolve here: the real interpreter for a
# bare `python`, and this symlink for the absolute path.
RUN mkdir -p /opt/venv/bin && ln -s /usr/local/bin/python /opt/venv/bin/python
"""

# Generous: this installs a dozen wheels, several of them large, on a runner
# shared with two sibling instances. A build that is merely slow should not
# read as a build that is broken.
TEMPLATE_BUILD_TIMEOUT_SECONDS = 900


def build_template(
    client: SandboxClient,
    dockerfile: str,
    *,
    timeout_seconds: int = TEMPLATE_BUILD_TIMEOUT_SECONDS,
) -> str:
    """POST a Dockerfile, wait for the build, and return the template name."""
    created = client._request("POST", "/v1/templates", json={"dockerfile": dockerfile})
    name = str(created["name"])

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        template = client._request("GET", f"/v1/templates/{name}")
        if template["status"] == "ready":
            return name
        if template["status"] == "failed":
            pytest.fail(f"template {name} failed to build: {template['error']}")
        time.sleep(2)
    pytest.fail(f"template {name} was still building after {timeout_seconds}s")


@pytest.fixture(scope="session")
def data_stack_template() -> str:
    """Build the data-stack template once for the whole e2e session.

    Session-scoped because the build is the expensive part; every test that
    needs a sandbox reuses the resulting image. It takes its own client rather
    than the function-scoped `client` fixture, which a session fixture cannot
    depend on.
    """
    api_key = os.environ.get("HARBORBOX_API_KEY")
    if not api_key:
        pytest.fail("HARBORBOX_API_KEY is required for e2e tests")
    with SandboxClient(api_key=api_key) as live:
        return build_template(live, DATA_STACK_DOCKERFILE)
