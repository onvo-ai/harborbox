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

import pytest
from fastapi import FastAPI
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider

from harborbox import __version__
from harborbox.config import Settings
from harborbox.telemetry import (
    SERVICE_NAME,
    build_resource,
    export_is_configured,
    instrument,
    service_name_for,
)


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
