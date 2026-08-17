"""Measure the control plane's own latency: notification wake-ups vs polling.

What this measures is the waiting, not the work. An execution's journey through
Harborbox is submit -> scheduler notices -> run -> result written -> client
notices, and on a warm sandbox running small code the two "notices" used to
dominate: the scheduler slept up to `scheduler_poll_seconds` between scans and
the SDK slept 200 ms between reads, around work measured in milliseconds.

Both modes run the real `ExecutionNotifier` and the real intervals from
`Settings`; the sandbox is a stub that sleeps `--work-ms`, because the point is
to isolate the queue's contribution from the workload's. The database round
trips are identical in both modes and therefore cancel out -- which is also why
this is not an end-to-end number and should not be quoted as one.

    poll   - what 0.2.0 did: scheduler wakes on a timer, client polls
    notify - what this branch does: scheduler wakes on an enqueue, client is
             answered inline when the execution finishes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from harborbox.config import Settings
from harborbox.notify import ExecutionNotifier

# What the Python SDK slept between reads before this change; kept as a literal
# so the comparison does not move when the SDK's own constants are retuned.
LEGACY_SDK_POLL_SECONDS = 0.2


@dataclass
class Result:
    mode: str
    samples: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float


class Harness:
    """A queue, a scheduler loop and a client, wired for one mode."""

    def __init__(self, settings: Settings, *, notify: bool, work_seconds: float) -> None:
        self.settings = settings
        self.work_seconds = work_seconds
        self.notifier = ExecutionNotifier(settings) if notify else None
        self.pending: list[str] = []
        self.finished: set[str] = set()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._scheduler_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self.notifier is not None:
            await self.notifier.notify_queued()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _scheduler_loop(self) -> None:
        """Scan the queue, then wait for more -- the shape of `Scheduler._scheduler_loop`."""
        while not self._stop.is_set():
            for execution_id in list(self.pending):
                self.pending.remove(execution_id)
                await asyncio.sleep(self.work_seconds)
                self.finished.add(execution_id)
                if self.notifier is not None:
                    await self.notifier.notify_execution_finished(execution_id)
            await self._wait_for_more_work()

    async def _wait_for_more_work(self) -> None:
        """Block until there may be more work; the one line the two modes differ on."""
        if self.notifier is not None:
            await self.notifier.wait_for_queue(self.settings.scheduler_poll_seconds)
        else:
            # The 0.2.0 behaviour being measured: wake on a timer, not an event.
            await asyncio.sleep(self.settings.scheduler_poll_seconds)

    async def submit_and_wait(self, execution_id: str) -> float:
        """Submit one execution and return how long the caller waited, in ms."""
        started = time.perf_counter()
        if self.notifier is None:
            self.pending.append(execution_id)
            # Precisely the loop being replaced, so it is spelled out here
            # rather than expressed with the Event that replaced it.
            while execution_id not in self.finished:  # noqa: ASYNC110 - see above
                await asyncio.sleep(LEGACY_SDK_POLL_SECONDS)
        else:
            # Registered before submitting, exactly as `_await_execution` does:
            # the execution can finish before the wait starts.
            with self.notifier.execution_waiter(execution_id) as finished:
                self.pending.append(execution_id)
                await self.notifier.notify_queued()
                while execution_id not in self.finished:
                    finished.clear()
                    try:
                        await asyncio.wait_for(
                            finished.wait(),
                            timeout=self.settings.scheduler_poll_seconds,
                        )
                    except TimeoutError:
                        continue
        return (time.perf_counter() - started) * 1000


async def measure(*, notify: bool, samples: int, work_seconds: float) -> Result:
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:")
    harness = Harness(settings, notify=notify, work_seconds=work_seconds)
    await harness.start()
    try:
        # One untimed warm-up so task startup does not land in the first sample.
        await harness.submit_and_wait("warmup")
        timings = [
            await harness.submit_and_wait(f"exec_{index}") for index in range(samples)
        ]
    finally:
        await harness.stop()

    ordered = sorted(timings)
    return Result(
        mode="notify" if notify else "poll",
        samples=samples,
        mean_ms=round(statistics.fmean(timings), 2),
        p50_ms=round(statistics.median(timings), 2),
        p95_ms=round(ordered[int(len(ordered) * 0.95) - 1], 2),
        max_ms=round(max(timings), 2),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument(
        "--work-ms",
        type=float,
        default=1.0,
        help="stand-in for the sandbox's own execution time",
    )
    parser.add_argument("--out", type=Path)
    arguments = parser.parse_args()

    work_seconds = arguments.work_ms / 1000
    results = [
        await measure(notify=False, samples=arguments.samples, work_seconds=work_seconds),
        await measure(notify=True, samples=arguments.samples, work_seconds=work_seconds),
    ]

    report = {"work_ms": arguments.work_ms, "results": [asdict(r) for r in results]}
    rendered = json.dumps(report, indent=2)
    if arguments.out:
        arguments.out.parent.mkdir(parents=True, exist_ok=True)
        arguments.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)  # noqa: T201 - CLI output

    print("\nmode      mean      p50      p95      max")  # noqa: T201
    for result in results:
        print(  # noqa: T201
            f"{result.mode:<8}"
            f"{result.mean_ms:7.1f}ms"
            f"{result.p50_ms:8.1f}ms"
            f"{result.p95_ms:8.1f}ms"
            f"{result.max_ms:8.1f}ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
