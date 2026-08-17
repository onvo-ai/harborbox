"""Wake-ups for the execution queue, so nothing on the hot path polls a clock.

Four separate poll loops used to sit between a caller and its result: the
scheduler slept `scheduler_poll_seconds` between queue scans, the SSE endpoint
slept a full second between reads, and both SDKs polled the execution endpoint
every 200 ms. On a warm sandbox running trivial code those intervals were most
of the latency -- the work itself was single-digit milliseconds.

This replaces the sleeps with notifications. Postgres already carries the queue,
the distributed leases and the warm-pool state, so `LISTEN`/`NOTIFY` on the same
database reaches every API replica without adding a broker.

Two channels, both carrying an id rather than a payload -- a notification is a
hint to go and look, never the data itself, so a listener that misses one and
falls back to its timeout still reads the same committed state:

* `harborbox_queue` - an execution was enqueued. Wakes the scheduler.
* `harborbox_execution` - an execution reached a terminal status. Wakes the
  inline `wait=true` responder and the SSE stream, keyed by execution id.

**Every wait takes a timeout, and every timeout is a correct outcome.** A
dropped listener connection, a notification that races a commit, a replica that
has not reconnected yet -- all of them degrade to exactly the polling behaviour
this replaces, which is why the fallback interval is still
`scheduler_poll_seconds`. Nothing here is load-bearing for correctness; it is
load-bearing for latency only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

import asyncpg
from sqlalchemy.engine import make_url

if TYPE_CHECKING:
    from collections.abc import Iterator

    from harborbox.config import Settings

logger = logging.getLogger(__name__)

QUEUE_CHANNEL = "harborbox_queue"
EXECUTION_CHANNEL = "harborbox_execution"

# How long a dropped listener waits before dialling again. Short enough that a
# database restart costs a few seconds of extra latency, not a few minutes;
# long enough that an unreachable database is not hammered.
_RECONNECT_SECONDS = 2.0


class ExecutionNotifier:
    """Fan-out for queue and completion events, Postgres-backed when it can be.

    Local delivery happens first and unconditionally, so a single-replica
    deployment -- the bundled Compose one -- gets its wake-up without waiting
    for a database round trip. `NOTIFY` is what carries the same event to the
    other replicas.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._queue_event = asyncio.Event()
        self._execution_events: dict[str, asyncio.Event] = {}
        self._waiter_counts: dict[str, int] = {}
        self._connection: asyncpg.Connection | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def _is_postgres(self) -> bool:
        return make_url(self.settings.database_url).get_backend_name() == "postgresql"

    async def start(self) -> None:
        """Begin listening. Safe to call when the database is not Postgres."""
        if not self._is_postgres:
            # SQLite has no LISTEN/NOTIFY. Local delivery still works, which is
            # all a single-process test or dev run needs.
            return
        self._listener_task = asyncio.create_task(
            self._listen_forever(), name="harborbox-notify-listener"
        )

    async def close(self) -> None:
        self._stop.set()
        if self._listener_task is not None:
            self._listener_task.cancel()
            await asyncio.gather(self._listener_task, return_exceptions=True)
        if self._connection is not None:
            with contextlib.suppress(Exception):
                await self._connection.close()
            self._connection = None

    def _dsn(self) -> str:
        """Translate SQLAlchemy's URL into one asyncpg will accept.

        The configured URL carries the `+asyncpg` driver marker and may carry
        SQLAlchemy-only query arguments; asyncpg understands neither.
        """
        url = make_url(self.settings.database_url)
        return url.set(drivername="postgresql", query={}).render_as_string(
            hide_password=False
        )

    async def _listen_forever(self) -> None:
        while not self._stop.is_set():
            try:
                connection: asyncpg.Connection = await asyncpg.connect(self._dsn())
                self._connection = connection
                lost = asyncio.Event()
                connection.add_termination_listener(
                    # Default-bound rather than closed over: the loop rebinds
                    # `lost` on every reconnect, and a late callback from a
                    # previous connection must not set the current one's event.
                    lambda _connection, event=lost: event.set()
                )
                await connection.add_listener(QUEUE_CHANNEL, self._on_queue)
                await connection.add_listener(EXECUTION_CHANNEL, self._on_execution)
                logger.info("Listening for execution notifications")
                # asyncpg dispatches notifications from its own reader task, so
                # this one only has to sleep until the connection dies or we do.
                waiters = [
                    asyncio.create_task(lost.wait()),
                    asyncio.create_task(self._stop.wait()),
                ]
                try:
                    await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for waiter in waiters:
                        waiter.cancel()
                    await asyncio.gather(*waiters, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a listener that dies takes the
                # latency win with it but never correctness, so every failure is
                # logged and retried rather than propagated.
                logger.warning("Notification listener dropped: %s", exc)
            finally:
                if self._connection is not None:
                    with contextlib.suppress(Exception):
                        await self._connection.close()
                    self._connection = None
            if not self._stop.is_set():
                await asyncio.sleep(_RECONNECT_SECONDS)

    def _on_queue(
        self,
        _connection: object,
        _pid: int,
        _channel: str,
        _payload: str,
    ) -> None:
        self._queue_event.set()

    def _on_execution(
        self,
        _connection: object,
        _pid: int,
        _channel: str,
        payload: str,
    ) -> None:
        self._wake_execution(payload)

    def _wake_execution(self, execution_id: str) -> None:
        event = self._execution_events.get(execution_id)
        if event is not None:
            event.set()

    async def _emit(self, channel: str, payload: str) -> None:
        if not self._is_postgres:
            return
        connection = self._connection
        if connection is None or connection.is_closed():
            # No listener connection yet: local delivery already happened and
            # every waiter has a timeout, so dropping the cross-replica hint is
            # a latency cost on other replicas, not a lost result.
            return
        with contextlib.suppress(Exception):
            await connection.execute("SELECT pg_notify($1, $2)", channel, payload)

    async def notify_queued(self) -> None:
        """Announce that there is something new to schedule."""
        self._queue_event.set()
        await self._emit(QUEUE_CHANNEL, "")

    async def notify_execution_finished(self, execution_id: str) -> None:
        """Announce that `execution_id` reached a terminal status."""
        self._wake_execution(execution_id)
        await self._emit(EXECUTION_CHANNEL, execution_id)

    async def wait_for_queue(self, timeout: float) -> None:  # noqa: ASYNC109 - the timeout
        # is the caller's fallback poll interval, not a deadline for an
        # operation, so `asyncio.timeout` at the call site would express the
        # wrong thing: expiry here is the normal path, never an error.
        """Wait for work, or for `timeout` to make us go and look anyway."""
        self._queue_event.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._queue_event.wait(), timeout=timeout)

    @contextlib.contextmanager
    def execution_waiter(self, execution_id: str) -> Iterator[asyncio.Event]:
        """Register interest in one execution for the duration of the block.

        Registration has to happen *before* the caller re-reads the execution's
        status, or a completion landing between the read and the wait is missed
        and the caller pays the full fallback timeout for a result that was
        already there.

        An SSE stream and an inline `wait=true` can watch the same execution, so
        the event is shared and reference counted; the last one out removes it,
        which is what keeps the registry from growing for the process lifetime.
        """
        event = self._execution_events.setdefault(execution_id, asyncio.Event())
        self._waiter_counts[execution_id] = self._waiter_counts.get(execution_id, 0) + 1
        try:
            yield event
        finally:
            remaining = self._waiter_counts.get(execution_id, 1) - 1
            if remaining <= 0:
                self._waiter_counts.pop(execution_id, None)
                self._execution_events.pop(execution_id, None)
            else:
                self._waiter_counts[execution_id] = remaining
