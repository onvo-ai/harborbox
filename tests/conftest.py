from __future__ import annotations

import os
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
