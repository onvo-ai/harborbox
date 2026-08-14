from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from harborbox_sdk import SandboxClient


@pytest.fixture
def client() -> Iterator[SandboxClient]:
    """A live-stack SDK client. Only meaningful for tests marked `e2e`."""
    api_key = os.environ.get("HARBORBOX_API_KEY")
    if not api_key:
        pytest.fail("HARBORBOX_API_KEY is required for e2e tests")
    with SandboxClient(api_key=api_key) as live:
        yield live
