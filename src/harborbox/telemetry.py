"""Export Harborbox's traces and logs to the estate's OpenTelemetry ingester.

Harborbox shipped for months with no instrumentation of any kind. The damage
was not the missing traces -- it was that the daily checkup asked SigNoz for
"error groups where service.name = harborbox", got zero because nothing had
ever sent a span under that name, and rendered it as a clean green. A section
that cannot fail is worse than one that is red: `checkup-common`'s rule that
`ok: false` means UNKNOWN never fired, because the query genuinely succeeded.
See DEV-1948.

Three decisions are worth stating, because each one is the difference between
this working and this silently not working.

**Explicit wiring, not `opentelemetry-instrument`.** The usual way to do this
is to add `opentelemetry-distro` and wrap the entrypoint in
`opentelemetry-instrument`, which configures everything from environment
variables. That was rejected here for one reason: it puts the two facts that
must be right -- the service name, and whether we export at all -- in a place
where being wrong is silent. With the wrapper, a missing `OTEL_SERVICE_NAME`
ships spans as `unknown_service`, a typo ships them under a name the checkup
does not query, and a missing endpoint makes the SDK retry
`http://localhost:4318` forever. All three reproduce the exact failure this
module exists to end. Wiring it here makes the service name a constant, and
makes "no endpoint configured" mean "do not export", both of which are unit
tested.

**Off unless an endpoint is configured.** DEV-1824 cost the estate a week of
triage: local `pnpm dev` runs exported into production SigNoz under the
production service name, and 94% of "production" errors turned out to be one
laptop. The rule that came out of it is that development must not export by
default, and that opting in must not be able to collide with production. Both
halves are here: no `OTEL_EXPORTER_OTLP_ENDPOINT` means nothing is
instrumented at all, and a deployment that is not `production` or `staging`
exports under `harborbox-dev` even if it points at the production ingester.

**Traces and logs, not metrics.** The two exit criteria are that harborbox
appears in `platform.ingestion.data.services` (spans) and that a failed
sandbox create lands in `errors.data.groups` (logs -- the failures that matter
most happen in the scheduler's background loop, which no HTTP span covers).
Metrics buy neither and are left for whoever has a question they answer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from harborbox import __version__

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import FastAPI

    from harborbox.config import Settings

logger = logging.getLogger(__name__)

# Not configurable, and that is the point. `admin/lib/products.js` maps the
# harborbox product to `services: ["harborbox"]`, and the checkup keeps every
# error group whose service matches. Any other string here -- a typo, an
# unset variable, the SDK's `unknown_service` default -- reproduces DEV-1948
# exactly: a query that succeeds, finds nothing, and reads as healthy.
SERVICE_NAME = "harborbox"

# DEV-1824's rule, applied ahead of time rather than after the fact. A
# developer who deliberately points a local stack at the estate ingester gets
# their own service name, so the opt-in cannot recreate the collision that
# buried two real production errors under 1121 laptop ones.
DEVELOPMENT_SERVICE_SUFFIX = "-dev"

# Environments whose telemetry is the estate's own. Everything else -- a
# laptop, a branch stack, a one-off -- is development for naming purposes.
DEPLOYED_ENVIRONMENTS = frozenset({"production", "staging"})

# Checked in order, matching the OTLP spec's own precedence: a signal-specific
# endpoint wins over the shared one. Presence of either is what "export is
# configured" means; the SDK reads the values themselves, along with
# `OTEL_EXPORTER_OTLP_HEADERS` (which carries `signoz-access-token`),
# compression and timeouts.
ENDPOINT_VARIABLES = (
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)


def export_is_configured(env: Mapping[str, str]) -> bool:
    """Whether this process has somewhere to export to.

    Deliberately not "is telemetry enabled" with a default of on. An unset
    endpoint is the local-development case, and the SDK's own behaviour there
    -- retry `http://localhost:4318` until the process dies -- is noise a
    developer never asked for.
    """
    return any(env.get(name, "").strip() for name in ENDPOINT_VARIABLES)


def service_name_for(environment: str) -> str:
    """Return the `service.name` a deployment in `environment` exports under.

    Production and staging are the estate's own and share the name the checkup
    queries. Anything else is suffixed, so that a local stack pointed at the
    production ingester shows up as itself instead of drowning the real rows.
    """
    if environment.strip().lower() in DEPLOYED_ENVIRONMENTS:
        return SERVICE_NAME
    return f"{SERVICE_NAME}{DEVELOPMENT_SERVICE_SUFFIX}"


def build_resource(environment: str) -> Resource:
    """Describe this process to the collector.

    `deployment.environment` rather than the newer
    `deployment.environment.name`: the estate's SigNoz views, the daily
    checkup's error grouping and every other service already exporting here
    use the older attribute, and a resource attribute is only worth anything
    if the queries asking for it use the same spelling.

    Set explicitly rather than left to `OTEL_RESOURCE_ATTRIBUTES`, because a
    deployment that forgets it produces errors filed under no environment at
    all -- which the checkup reads as "not production" and skips.
    """
    return Resource.create(
        {
            "service.name": service_name_for(environment),
            "service.version": __version__,
            "deployment.environment": environment,
        }
    )


def _bridge_logging(provider: LoggerProvider) -> None:
    """Send Python log records to the collector as OTLP logs.

    This, not the span instrumentation, is what makes a failed sandbox create
    visible. Creates fail in `Scheduler`'s background loop -- `logger.exception
    ("execution failed")` -- which no request span covers, and in
    `TemplateBuilder` for a build that fails long after the POST returned 202.

    Handler level is NOTSET on purpose: the loggers decide, exactly as they do
    for stderr today, so switching telemetry on does not also change which
    records exist.
    """
    root = logging.getLogger()
    if not root.handlers:
        # Until this call root had no handler, so `logging.lastResort` printed
        # WARNING and above to stderr -- which is what `docker compose logs`
        # and CI's "Stack logs on failure" step read. Adding *any* handler
        # retires lastResort, so the stderr copy has to be made explicit or the
        # container log goes quiet at the moment telemetry is switched on.
        root.addHandler(logging.StreamHandler())
    root.addHandler(LoggingHandler(level=logging.NOTSET, logger_provider=provider))


def instrument(app: FastAPI, settings: Settings, env: Mapping[str, str]) -> bool:
    """Wire this process up to the collector. Returns whether it did.

    A no-op with no endpoint configured -- see `export_is_configured`. Nothing
    is patched in that case, so local development pays nothing and behaves
    exactly as it did before this module existed.
    """
    if not export_is_configured(env):
        logger.info(
            "No OTLP endpoint configured; Harborbox is not exporting telemetry. "
            "Set OTEL_EXPORTER_OTLP_ENDPOINT to export."
        )
        return False

    resource = build_resource(settings.environment)

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)
    _bridge_logging(logger_provider)

    # `/health` is deliberately *not* excluded, and it is the cheapest half of
    # the fix. Harborbox serves widget Python on demand; between one execution
    # and the next it can legitimately go hours with no traffic, and a service
    # that sends nothing when idle is indistinguishable from one that cannot
    # send at all -- which is the whole bug. The container healthcheck polls
    # /health every three seconds, so a traced /health is a heartbeat that
    # makes "harborbox sent no data" a detectable state rather than the normal
    # one.
    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)
    # opentelemetry-instrumentation-asyncpg ships a py.typed marker but leaves
    # `BaseInstrumentor.__init__` unannotated, so strict mypy sees an untyped
    # call. The sibling instrumentors above are annotated; only this one needs
    # the exemption.
    AsyncPGInstrumentor().instrument(  # type: ignore[no-untyped-call]
        tracer_provider=tracer_provider
    )

    logger.info(
        "Exporting telemetry as %s (%s)",
        resource.attributes["service.name"],
        settings.environment,
    )
    return True
