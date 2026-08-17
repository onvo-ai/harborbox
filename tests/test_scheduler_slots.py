"""Per-sandbox execution slots.

This used to encode a second rule: code executions ran on one shared Jupyter
namespace per sandbox, so they were exclusive and blocked everything else.
`POST /v1/sandboxes/{id}/executions` has been removed -- it had no caller --
so every execution is now an ordinary process and concurrency is the only
limit left.
"""

from __future__ import annotations

from harborbox.scheduler import has_sandbox_execution_slot


def test_an_idle_sandbox_has_a_slot() -> None:
    assert has_sandbox_execution_slot(active_count=0, limit=1)


def test_a_sandbox_at_its_limit_has_none() -> None:
    assert not has_sandbox_execution_slot(active_count=1, limit=1)


def test_concurrency_above_one_allows_overlap() -> None:
    """The setting that makes two shell commands share a sandbox."""
    assert has_sandbox_execution_slot(active_count=1, limit=2)
    assert not has_sandbox_execution_slot(active_count=2, limit=2)
