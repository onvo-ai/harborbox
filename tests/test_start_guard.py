from harborbox.scheduler import may_start_execution


def ok(**overrides: object) -> bool:
    kwargs: dict[str, object] = {
        "cancel_requested": False,
        "execution_status": "admitted",
        "sandbox_status": "created",
    }
    kwargs.update(overrides)
    return may_start_execution(**kwargs)  # type: ignore[arg-type]


def test_an_admitted_execution_on_a_live_sandbox_starts() -> None:
    assert ok()
    assert ok(sandbox_status="paused_cold")
    assert ok(sandbox_status="running")


def test_a_deleted_sandbox_does_not_start_its_admitted_execution() -> None:
    # The regression this exists for: DELETE /v1/sandboxes returns 204 and the
    # caller stops tracking the sandbox. If the scheduler starts it anyway, the
    # container it creates is unreachable and unowned, and holds its CPU and
    # memory reservation until the idle reaper — observed as a host that admits
    # nothing for twenty minutes after a burst of cancelled work.
    assert not ok(sandbox_status="killed")
    assert not ok(sandbox_status="failed")


def test_cancellation_is_honoured_even_when_the_status_was_overwritten() -> None:
    # `cancel_requested` is checked separately from the status because the status
    # is not durable against a racing scheduler pass: that pass can hold a
    # snapshot in which the execution is still `queued` and commit `admitted` over
    # `cancelled`. The flag survives that, so it is what the guard trusts.
    assert not ok(cancel_requested=True)
    assert not ok(cancel_requested=True, execution_status="admitted")


def test_settled_executions_do_not_restart() -> None:
    for status in ("succeeded", "failed", "cancelled"):
        assert not ok(execution_status=status)
