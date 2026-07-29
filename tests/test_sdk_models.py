from typing import Any

from harborbox_sdk.models import Execution


class FakeClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses

    def _request(self, *_: object, **__: object) -> dict[str, Any]:
        return self.responses.pop(0)


def payload(status: str, *, text: str | None = None) -> dict[str, Any]:
    return {
        "id": "exec_1",
        "sandbox_id": "sbx_1",
        "kind": "code",
        "status": status,
        "queue_position": 1 if status == "queued" else None,
        "logs": {"stdout": ["hello\n"], "stderr": [], "truncated": False},
        "results": [{"text": text, "data": {}}] if text else [],
        "error": None,
    }


def test_execution_wait_and_text_shape() -> None:
    client = FakeClient([payload("running"), payload("succeeded", text="42")])
    execution = Execution(client, payload("queued"))  # type: ignore[arg-type]
    execution.wait(timeout=1, poll_interval=0)
    assert execution.status == "succeeded"
    assert execution.text == "42"
    assert execution.logs.stdout == ["hello\n"]

