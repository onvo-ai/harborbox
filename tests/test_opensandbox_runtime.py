from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import harborbox.opensandbox_runtime as runtime_module
from harborbox.config import Settings
from harborbox.models import Sandbox
from harborbox.opensandbox_runtime import (
    SNAPSHOT_METADATA_KEY,
    OpenSandboxRuntime,
    _BoundedOutput,
)


# `overrides` can set any Sandbox column, whose types are heterogeneous
# (str, int, float, dict, datetime, ...), so Any is the honest type here.
def sandbox_record(**overrides: Any) -> Sandbox:  # noqa: ANN401
    values: dict[str, Any] = {
        "id": "sbx-test",
        "status": "created",
        "container_id": None,
        "container_name": None,
        "agent_token": "unused-with-opensandbox",
        "memory_mb": 768,
        "cpu": 1.5,
        "pids_limit": 128,
        "idle_timeout_seconds": 60,
        "metadata_": {"template": "onvo-pro", "team": "test"},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "last_activity_at": datetime.now(UTC),
    }
    values.update(overrides)
    return Sandbox(**values)


class FakeHandle:
    def __init__(self, sandbox_id: str = "osb-runtime-1") -> None:
        self.id = sandbox_id
        self.killed = False
        self.closed = False
        self.paused = False

    async def close(self) -> None:
        self.closed = True

    async def kill(self) -> None:
        self.killed = True

    async def pause(self) -> None:
        self.paused = True

    async def create_snapshot(self, name: str | None = None) -> SimpleNamespace:
        assert name == "harborbox-sbx-test"
        return SimpleNamespace(id="snap-test")


class FakeManager:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def get_snapshot(self, snapshot_id: str) -> SimpleNamespace:
        assert snapshot_id == "snap-test"
        return SimpleNamespace(
            status=SimpleNamespace(state="Ready", message=None)
        )

    async def delete_snapshot(self, snapshot_id: str) -> None:
        self.deleted.append(snapshot_id)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_start_delegates_image_and_resource_limits_to_opensandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    handle = FakeHandle()

    class FakeOpenSandbox:
        @classmethod
        # Stands in for opensandbox.Sandbox.create and exists to capture
        # whatever the runtime passes it, so it must accept anything.
        async def create(cls, *args: Any, **kwargs: Any) -> FakeHandle:  # noqa: ANN401
            captured["args"] = args
            captured["kwargs"] = kwargs
            return handle

    monkeypatch.setattr(runtime_module, "OpenSandbox", FakeOpenSandbox)
    runtime = OpenSandboxRuntime(Settings())
    sandbox = sandbox_record()

    started = await runtime.start_sandbox(sandbox)

    assert started.id == "osb-runtime-1"
    assert captured["args"] == ("harborbox-sandbox-onvo-pro:local",)
    assert captured["kwargs"]["resource"] == {
        "cpu": "1.5",
        "memory": "768Mi",
    }
    assert captured["kwargs"]["metadata"]["harborbox.sandbox_id"] == "sbx-test"
    assert captured["kwargs"]["timeout"] is None
    await runtime.close()


@pytest.mark.asyncio
async def test_start_claims_matching_warm_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = FakeHandle("osb-warm-1")

    # Stands in for the warm pool's acquire() and exists to assert on
    # whatever kwargs the runtime forwards, so it must accept anything.
    async def acquire(**kwargs: Any) -> FakeHandle:  # noqa: ANN401
        assert kwargs == {
            "template": "relaydeck",
            "memory_mb": 256,
            "cpu": 0.5,
        }
        return handle

    runtime = OpenSandboxRuntime(Settings())
    monkeypatch.setattr(runtime._warm_pools, "acquire", acquire)
    sandbox = sandbox_record(
        memory_mb=256,
        cpu=0.5,
        metadata_={"template": "relaydeck"},
    )

    started = await runtime.start_sandbox(sandbox)

    assert started.id == "osb-warm-1"
    assert sandbox.metadata_["harborbox.runtime.warm_pool"] == "true"
    await runtime.close()


@pytest.mark.asyncio
async def test_cold_pause_snapshots_kills_and_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = FakeHandle()
    restored = FakeHandle("osb-runtime-2")
    manager = FakeManager()
    captured: dict[str, Any] = {}

    class FakeOpenSandbox:
        @classmethod
        # Stands in for opensandbox.Sandbox.create and exists to capture
        # whatever the runtime passes it, so it must accept anything.
        async def create(cls, *args: Any, **kwargs: Any) -> FakeHandle:  # noqa: ANN401
            captured["args"] = args
            captured["kwargs"] = kwargs
            return restored

    monkeypatch.setattr(runtime_module, "OpenSandbox", FakeOpenSandbox)
    runtime = OpenSandboxRuntime(Settings())
    runtime._manager = manager  # type: ignore[assignment]
    runtime._sandboxes["sbx-test"] = original
    sandbox = sandbox_record(
        status="running",
        container_id="osb-runtime-1",
        container_name="osb-runtime-1",
    )

    await runtime.pause(sandbox, memory=False)

    assert original.killed is True
    assert original.closed is True
    assert sandbox.metadata_[SNAPSHOT_METADATA_KEY] == "snap-test"

    sandbox.container_id = None
    sandbox.container_name = None
    started = await runtime.start_sandbox(sandbox)

    assert started.id == "osb-runtime-2"
    assert captured["args"] == ()
    assert captured["kwargs"]["snapshot_id"] == "snap-test"
    assert sandbox.metadata_[SNAPSHOT_METADATA_KEY] == "snap-test"
    assert manager.deleted == []

    sandbox.container_id = started.id
    sandbox.container_name = started.name
    await runtime.kill(sandbox)

    assert restored.killed is True
    assert SNAPSHOT_METADATA_KEY not in sandbox.metadata_
    assert manager.deleted == ["snap-test"]
    await runtime.close()


@pytest.mark.asyncio
async def test_output_collector_enforces_byte_limit() -> None:
    output = _BoundedOutput(5)

    await output.on_stdout(SimpleNamespace(text="abcdef"))
    await output.on_stderr(SimpleNamespace(text="ignored"))

    assert output.stdout == ["abcde"]
    assert output.stderr == []
    assert output.truncated is True
