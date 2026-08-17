"""Runtime-neutral sandbox failures.

These lived in `runtime.py` alongside the Docker provider until that provider
was deleted. They are raised by whichever runtime is in use and caught by the
API, the scheduler and the reaper, so they belong to none of them.
"""

from __future__ import annotations


class SandboxRuntimeError(RuntimeError):
    pass


class SandboxUnavailableError(SandboxRuntimeError):
    pass


class SandboxStartTimeoutError(SandboxRuntimeError):
    """A caller-side deadline elapsed while waiting for a lazy start.

    Distinct from `SandboxUnavailableError`: the sandbox is not known to be
    broken, it is simply still starting. The start itself is not aborted --
    see `Scheduler.ensure_sandbox_ready` -- so a retry can reattach to it.
    """


class SandboxMemoryExceededError(SandboxRuntimeError):
    def __init__(self, memory_mb: int) -> None:
        super().__init__(f"sandbox exceeded its {memory_mb} MiB memory limit")
