from harborbox.scheduler import has_sandbox_execution_slot


def test_commands_can_share_a_sandbox_up_to_the_limit() -> None:
    assert has_sandbox_execution_slot(
        kind="command",
        active_count=2,
        active_code=False,
        limit=4,
    )
    assert not has_sandbox_execution_slot(
        kind="command",
        active_count=4,
        active_code=False,
        limit=4,
    )


def test_kernel_code_remains_exclusive() -> None:
    assert not has_sandbox_execution_slot(
        kind="code",
        active_count=1,
        active_code=False,
        limit=4,
    )
    assert not has_sandbox_execution_slot(
        kind="command",
        active_count=1,
        active_code=True,
        limit=4,
    )
    assert has_sandbox_execution_slot(
        kind="code",
        active_count=0,
        active_code=False,
        limit=4,
    )
