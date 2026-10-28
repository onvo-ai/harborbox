"""What has to stay true for the daily checkup to be able to see Harborbox.

The bug these pin (DEV-1948) was not a crash. Harborbox exported nothing, the
checkup asked SigNoz for error groups under `service.name = harborbox`, got a
structural zero and rendered it green. Every assertion below is one of the
ways that comes back: the wrong service name, an accidental export from a
laptop, a resource with no environment on it, or a log bridge that silences
the container log it replaces.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    InMemoryLogRecordExporter,
)
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from harborbox import __version__, telemetry
from harborbox.config import Settings
from harborbox.telemetry import (
    SERVICE_NAME,
    build_resource,
    export_is_configured,
    instrument,
    service_name_for,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence
    from pathlib import Path


def test_the_production_service_name_is_the_one_the_checkup_queries() -> None:
    """`admin/lib/products.js` maps harborbox to `services: ["harborbox"]`.

    A rename here does not break anything visibly -- spans keep flowing, the
    checkup keeps returning `ok: true` -- it just makes the error count
    structurally zero again, which is the entire bug.
    """
    assert service_name_for("production") == "harborbox"
    assert SERVICE_NAME == "harborbox"


def test_staging_shares_the_production_service_name() -> None:
    """Staging is the estate's own, and is separated by `deployment.environment`.

    DEV-1824's tables group by that attribute with one service name per
    service, so giving staging a name of its own would hide it from every view
    built on the pair.
    """
    assert service_name_for("staging") == SERVICE_NAME


@pytest.mark.parametrize("environment", ["development", "local", "branch-42", ""])
def test_anything_that_is_not_the_estate_exports_under_its_own_name(
    environment: str,
) -> None:
    """DEV-1824, prevented rather than repeated.

    A developer pointing a local stack at the production ingester is a
    legitimate thing to do. What is not survivable is that stack's errors
    arriving indistinguishable from production's -- 1121 laptop errors buried
    the two real production ones for a week.
    """
    assert service_name_for(environment) == f"{SERVICE_NAME}-dev"


@pytest.mark.parametrize("environment", ["PRODUCTION", " production ", "Staging"])
def test_the_environment_match_is_forgiving_about_spelling(environment: str) -> None:
    """A capitalised or padded value is the same deployment, not a dev one.

    Getting this wrong fails in the expensive direction: production exports
    under `harborbox-dev` and the checkup goes back to reporting zero, with
    every span present and correct in SigNoz under a name nothing queries.
    """
    assert service_name_for(environment) == SERVICE_NAME


def test_the_resource_carries_what_the_error_views_filter_on() -> None:
    """`deployment.environment`, spelled the way the estate's queries spell it.

    DEV-1824's third exit criterion is that the triage view filters to
    production. A resource without the attribute is filed under no environment
    at all, which that filter excludes -- so the errors would reach SigNoz and
    still never be counted.
    """
    resource = build_resource("production")

    assert resource.attributes["service.name"] == SERVICE_NAME
    assert resource.attributes["deployment.environment"] == "production"
    assert resource.attributes["service.version"] == __version__


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"OTEL_EXPORTER_OTLP_ENDPOINT": ""},
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "   "},
    ],
)
def test_no_endpoint_means_no_export(env: dict[str, str]) -> None:
    """The safe default, including the empty-string spelling Compose produces.

    `OTEL_EXPORTER_OTLP_ENDPOINT: ${OTEL_EXPORTER_OTLP_ENDPOINT:-}` sets the
    variable to an empty value rather than leaving it unset, so a bare
    `in os.environ` check would read a local stack as configured and then
    retry an empty endpoint forever.
    """
    assert export_is_configured(env) is False


@pytest.mark.parametrize(
    "variable",
    ["OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"],
)
def test_either_spelling_of_the_endpoint_counts(variable: str) -> None:
    """The OTLP spec lets a signal-specific endpoint stand alone.

    Recognising only the shared one would mean a deployment that followed the
    spec exported nothing, silently.
    """
    assert export_is_configured({variable: "https://ingester.onvo.ai"}) is True


def test_an_unconfigured_process_instruments_nothing() -> None:
    """No endpoint is not "export to nowhere" -- it is "do not instrument".

    The distinction is what keeps local development free: no middleware on the
    app, no patched httpx, and none of the SDK's default behaviour of retrying
    `http://localhost:4318` until the process exits.
    """
    app = FastAPI()
    before = list(app.user_middleware)

    assert instrument(app, Settings(), {}) is False
    assert list(app.user_middleware) == before
    assert HTTPXClientInstrumentor().is_instrumented_by_opentelemetry is False


def test_bridging_logs_keeps_the_stderr_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching telemetry on must not empty `docker compose logs`.

    Harborbox configures no logging at all, so warnings and errors reach stderr
    through `logging.lastResort` -- which Python retires the moment the root
    logger gains any handler. Attaching only the OTLP handler would therefore
    take the container log with it, and CI's "Stack logs on failure" step and
    every `docker compose logs` reads that log.
    """
    from harborbox.telemetry import _bridge_logging  # noqa: PLC0415

    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])

    _bridge_logging(LoggerProvider())

    kinds = [type(handler) for handler in root.handlers]
    assert LoggingHandler in kinds
    assert logging.StreamHandler in kinds


def test_the_log_bridge_leaves_which_records_exist_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler filters nothing; the loggers keep deciding, as they do today.

    A handler with its own level would quietly become a second, invisible
    filter -- and the records it dropped would be exactly the ones nobody
    thought to look for.
    """
    from harborbox.telemetry import _bridge_logging  # noqa: PLC0415

    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])

    _bridge_logging(LoggerProvider())

    bridge = next(h for h in root.handlers if isinstance(h, LoggingHandler))
    assert bridge.level == logging.NOTSET


# -- Shutdown flush (DEV-2618) -------------------------------------------------


def _batched_providers() -> tuple[
    TracerProvider, InMemorySpanExporter, LoggerProvider, InMemoryLogRecordExporter
]:
    """Build providers whose batches only leave on shutdown, like a quiet deploy.

    A schedule delay of an hour means nothing is exported unless the
    shutdown path drains the queue -- which is exactly the property under test.
    """
    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider(shutdown_on_exit=False)
    tracer_provider.add_span_processor(BatchSpanProcessor(spans, schedule_delay_millis=3_600_000))
    logs = InMemoryLogRecordExporter()
    logger_provider = LoggerProvider(shutdown_on_exit=False)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(logs, schedule_delay_millis=3_600_000)
    )
    return tracer_provider, spans, logger_provider, logs


async def test_the_lifespan_flushes_what_is_still_queued() -> None:
    """The end of the lifespan exports the last spans and logs.

    `atexit` would do this too, if it ran -- and under `docker stop` it does
    not, because uvicorn re-raises SIGTERM after its graceful shutdown.
    """
    tracer_provider, spans, logger_provider, logs = _batched_providers()
    app = FastAPI()
    telemetry._flush_on_shutdown(app, tracer_provider, logger_provider)

    async with app.router.lifespan_context(app):
        tracer_provider.get_tracer(__name__).start_span("last request").end()
        logger_provider.get_logger(__name__).emit(body="last log")
        assert spans.get_finished_spans() == ()

    assert [span.name for span in spans.get_finished_spans()] == ["last request"]
    assert [record.log_record.body for record in logs.get_finished_logs()] == ["last log"]


async def test_the_flush_comes_after_the_app_s_own_teardown() -> None:
    """Shutdown logs are the ones that explain a bad stop, so they are flushed too.

    `api.lifespan` stops the scheduler and runtime in its `finally`. Flushing
    before that would drop exactly the records about what went wrong on the
    way down.
    """
    tracer_provider, _spans, logger_provider, logs = _batched_providers()

    @asynccontextmanager
    async def app_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        logger_provider.get_logger(__name__).emit(body="scheduler stopped")

    app = FastAPI(lifespan=app_lifespan)
    telemetry._flush_on_shutdown(app, tracer_provider, logger_provider)

    async with app.router.lifespan_context(app):
        pass

    assert [record.log_record.body for record in logs.get_finished_logs()] == [
        "scheduler stopped"
    ]


async def test_a_failing_startup_still_flushes() -> None:
    """A lifespan that raises is the stop most worth having telemetry for."""
    tracer_provider, _spans, logger_provider, logs = _batched_providers()

    @asynccontextmanager
    async def app_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger_provider.get_logger(__name__).emit(body="schema creation failed")
        message = "database unreachable"
        raise RuntimeError(message)
        yield  # pragma: no cover

    app = FastAPI(lifespan=app_lifespan)
    telemetry._flush_on_shutdown(app, tracer_provider, logger_provider)

    with pytest.raises(RuntimeError, match="database unreachable"):
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover

    assert [record.log_record.body for record in logs.get_finished_logs()] == [
        "schema creation failed"
    ]


# Ten times the patched bound: loose enough for a slow CI runner, tight enough
# that an unbounded wait on the hanging exporter cannot pass.
FLUSH_GIVES_UP_WELL_WITHIN_SECONDS = 2


class _HangingExporter(SpanExporter):
    """An ingester that accepts the connection and never answers."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:  # noqa: ARG002
        self.release.wait()
        return SpanExportResult.FAILURE


async def test_a_dead_ingester_cannot_hold_the_stop_past_the_grace_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flush gives up so Docker's SIGKILL is not what ends the stop.

    With the ingester down the batches are lost either way; what the bound
    buys is a process that exits on its own instead of being killed mid-way
    through whatever else shutdown was doing.
    """
    monkeypatch.setattr(telemetry, "SHUTDOWN_FLUSH_TIMEOUT_SECONDS", 0.2)
    exporter = _HangingExporter()
    tracer_provider = TracerProvider(shutdown_on_exit=False)
    tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
    tracer_provider.get_tracer(__name__).start_span("stuck").end()

    started = time.monotonic()
    try:
        await telemetry._shutdown_providers(tracer_provider, LoggerProvider(shutdown_on_exit=False))
        assert time.monotonic() - started < FLUSH_GIVES_UP_WELL_WITHIN_SECONDS
    finally:
        exporter.release.set()


class _Collector(BaseHTTPRequestHandler):
    paths: list[str]

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self.paths.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def collector() -> Iterator[tuple[str, list[str]]]:
    paths: list[str] = []
    handler = type("Handler", (_Collector,), {"paths": paths})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", paths
    finally:
        server.shutdown()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX signals")
def test_a_sigtermed_uvicorn_delivers_its_last_batches(
    collector: tuple[str, list[str]], tmp_path: Path
) -> None:
    """The real thing: `docker stop` against uvicorn, with nothing mocked.

    This is the test that found DEV-2618. Before the lifespan flush the
    process exited 143 and the collector received nothing, even answering 200
    -- the SDK's `atexit` hook never ran because uvicorn re-raises the SIGTERM
    it caught. The unit tests above pin the wrapper; this one pins that the
    wrapper is still what runs when uvicorn stops.
    """
    endpoint, paths = collector
    (tmp_path / "stopped_app.py").write_text(
        textwrap.dedent(
            """
            import logging, os
            from fastapi import FastAPI
            from harborbox.config import Settings
            from harborbox.telemetry import instrument

            app = FastAPI()

            @app.get("/work")
            def work() -> dict[str, str]:
                logging.getLogger("harborbox.test").warning("before the stop")
                return {}

            instrument(app, Settings(), os.environ)
            """
        )
    )
    port = _free_port()
    env = {
        **os.environ,
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        # Nothing leaves on a timer during the test; only shutdown can send it.
        "OTEL_BSP_SCHEDULE_DELAY": "3600000",
        "OTEL_BLRP_SCHEDULE_DELAY": "3600000",
    }
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "uvicorn", "stopped_app:app", "--port", str(port)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/work", timeout=1)
                break
            except OSError:
                if time.monotonic() > deadline or process.poll() is not None:
                    raise
                time.sleep(0.1)
        assert paths == []

        process.send_signal(signal.SIGTERM)
        process.wait(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert "/v1/traces" in paths
    assert "/v1/logs" in paths
