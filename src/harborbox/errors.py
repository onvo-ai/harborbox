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


class SandboxMemoryExceededError(SandboxRuntimeError):
    def __init__(self, memory_mb: int) -> None:
        super().__init__(f"sandbox exceeded its {memory_mb} MiB memory limit")
