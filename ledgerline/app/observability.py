"""Observability: logs, traces, metrics and errors (guide Chapter 3).

One job per tool:
    logs     JSON lines on stdout. The platform collects them (docker logs,
             CloudWatch); the app never ships logs itself.
    traces   OpenTelemetry, exported over OTLP to whatever backend is
    metrics  configured (local Grafana, AWS, Datadog...). Vendor-neutral: the
             backend is a configuration choice, not a code change.
    errors   Sentry: grouped exceptions with stack traces and alerting.

Everything is configured from standard environment variables and is OFF when
they're unset, so tests and a bare `docker run` need nothing:
    OTEL_EXPORTER_OTLP_ENDPOINT   e.g. http://otel-lgtm:4318 (traces + metrics on)
    OTEL_SERVICE_NAME             defaults to "ledgerline"
    SENTRY_DSN                    errors on
    LOG_FORMAT                    json (default) | text

Correlation is the point: every log line carries the request ID and the trace
ID, every Sentry error carries both, and a background extraction is linked to
the request that started it. One ID takes you from an alert to the cause.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import UTC, datetime

from opentelemetry import metrics, trace

# ---------------------------------------------------------------- request context

# A contextvar follows the request: through `await`, into FastAPI's thread pool
# and into background tasks. Plain globals or thread-locals would mix requests up.
#
# It holds a dict, created fresh per request, rather than one variable per
# field: the endpoint runs in a child task, and a value it *sets* would be
# invisible to the middleware's access log. Mutating the shared dict isn't.
_log_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar("log_context", default=None)

def log_context() -> dict:
    ctx = _log_context.get()
    if ctx is None:  # outside a request (start-up, CLI, a plain thread)
        ctx = {}
        _log_context.set(ctx)
    return ctx

# Accept a caller's request ID (a load balancer or another service may set one)
# only if it looks like an ID; never echo arbitrary input into logs.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
REQUEST_ID_HEADER = "X-Request-ID"


def set_user_context(tenant_id: str, user_id: str) -> None:
    """Called once the user is known (app.auth), so later log lines carry them."""
    ctx = log_context()
    ctx["tenant_id"], ctx["user_id"] = tenant_id, user_id


def current_trace_ids() -> tuple[str | None, str | None]:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


# ---------------------------------------------------------------- logs

# Attributes every LogRecord has; anything else was passed with extra={...}.
_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message", "asctime", "taskName",
    "color_message",  # uvicorn's copy of the message with terminal colour codes
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: easy to search, filter and count by field.

    Fields: time, level, logger, message, request/tenant/user IDs, trace/span
    IDs, anything passed with `extra=`, and the stack trace for exceptions.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update(_log_context.get() or {})
        trace_id, span_id = current_trace_ids()
        if trace_id:
            entry["trace_id"], entry["span_id"] = trace_id, span_id
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(log_format: str = "json", level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    if log_format == "text":
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    else:
        handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Uvicorn installs its own plain-text handlers; send its lines through ours.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    # Uvicorn's own access log duplicates ours (below) without the request ID.
    logging.getLogger("uvicorn.access").disabled = True


# ---------------------------------------------------------------- access log + request ID

access_log = logging.getLogger("ledgerline.access")

# Health checks arrive every few seconds from the load balancer. Logging them
# at INFO costs money and buries real traffic, so they're DEBUG.
QUIET_PATHS = {"/livez", "/readyz"}


async def request_context_middleware(request, call_next):
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    request_id = supplied if _VALID_REQUEST_ID.match(supplied) else uuid.uuid4().hex
    _log_context.set({"request_id": request_id})
    trace.get_current_span().set_attribute("ledgerline.request_id", request_id)

    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers[REQUEST_ID_HEADER] = request_id  # users can quote it in a bug report
        return response
    finally:
        route = request.scope.get("route")
        access_log.log(
            logging.DEBUG if request.url.path in QUIET_PATHS else logging.INFO,
            "%s %s %s", request.method, request.url.path, status,
            extra={
                "http_method": request.method,
                # The route template ("/invoices/{invoice_id}"), not the raw path:
                # one value per endpoint, so it can be grouped and counted.
                "http_route": getattr(route, "path", None),
                "http_status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )


# ---------------------------------------------------------------- traces and metrics

tracer = trace.get_tracer("ledgerline")
meter = metrics.get_meter("ledgerline")

# Business and AI metrics. HTTP and database metrics come from instrumentation.
invoices_extracted = meter.create_counter(
    "ledgerline.invoices.extracted", unit="{invoice}",
    description="Extractions finished, by outcome (needs_review | extraction_failed)",
)
extraction_duration = meter.create_histogram(
    "ledgerline.extraction.duration", unit="s",
    description="From picking up an invoice to its extraction result, including retries",
)
# Names and attributes follow OpenTelemetry's GenAI semantic conventions, so
# any backend that understands them can chart LLM usage without custom setup.
llm_token_usage = meter.create_histogram(
    "gen_ai.client.token.usage", unit="{token}", description="Tokens per LLM call, by gen_ai.token.type",
)
llm_duration = meter.create_histogram(
    "gen_ai.client.operation.duration", unit="s", description="LLM call duration",
)
llm_cost = meter.create_counter(
    "ledgerline.llm.cost", unit="USD", description="Estimated LLM spend (from the pricing table)",
)


def fastapi_telemetry_config() -> dict:
    """FastAPI's built-in OpenTelemetry: request spans, HTTP metrics, and spans for
    dependencies, the endpoint and background tasks. It uses whatever providers
    setup_telemetry() installs, and does nothing if there are none."""
    return {
        "exclude": lambda scope: scope.get("path") in QUIET_PATHS,  # no traces or metrics for health checks
        "logs": False,  # unhandled exceptions are reported by Sentry, not duplicated as OTel logs
    }


def setup_telemetry(engine, *, service_version: str, environment: str) -> bool:
    """Turn on OpenTelemetry export if an OTLP endpoint is configured."""
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # The resource says WHAT is sending: every span and metric carries it.
    resource = Resource.create({
        "service.name": os.getenv("OTEL_SERVICE_NAME", "ledgerline"),
        "service.version": service_version,
        "deployment.environment.name": environment,
    })
    # Sampling is set with the standard OTEL_TRACES_SAMPLER / _ARG variables
    # (default: every trace). Batching keeps export off the request path.
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    metrics.set_meter_provider(MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=15_000)],
    ))

    # Request spans and HTTP metrics come from FastAPI itself (native
    # OpenTelemetry, configured in app.main); these add the layers below it.
    instrument_database(engine)              # a span per query
    HTTPXClientInstrumentor().instrument()   # outbound HTTP, including the Gemini API
    logging.getLogger("ledgerline").info("OpenTelemetry export on", extra={"otlp_endpoint": os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]})
    return True


def instrument_database(engine) -> None:
    """A span per SQL statement, using SQLAlchemy's own events.

    The statement text is recorded with its placeholders (WHERE id = %(id)s),
    never the values, so no invoice data reaches the tracing backend.
    """
    from opentelemetry.trace import SpanKind, Status, StatusCode
    from sqlalchemy import event

    db_system = {"postgresql": "postgresql", "sqlite": "sqlite"}.get(engine.dialect.name, engine.dialect.name)

    @event.listens_for(engine, "before_cursor_execute")
    def _start(conn, cursor, statement, parameters, context, executemany):
        operation = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "SQL"
        span = tracer.start_span(operation, kind=SpanKind.CLIENT, attributes={
            "db.system.name": db_system,
            "db.operation.name": operation,
            "db.query.text": statement[:2000],
        })
        conn.info.setdefault("_otel_spans", []).append(span)

    @event.listens_for(engine, "after_cursor_execute")
    def _end(conn, cursor, statement, parameters, context, executemany):
        if spans := conn.info.get("_otel_spans"):
            spans.pop().end()

    @event.listens_for(engine, "handle_error")
    def _error(exception_context):
        conn = exception_context.connection
        if conn is not None and (spans := conn.info.get("_otel_spans")):
            span = spans.pop()
            span.set_status(Status(StatusCode.ERROR, type(exception_context.original_exception).__name__))
            span.end()


# ---------------------------------------------------------------- errors (Sentry)


def _sentry_before_send(event, hint):
    """Attach our IDs so a Sentry error leads straight to its logs and trace."""
    tags = event.setdefault("tags", {})
    ctx = _log_context.get() or {}
    for key in ("request_id", "tenant_id"):
        if key in ctx:
            tags[key] = ctx[key]
    trace_id, _ = current_trace_ids()
    if trace_id:
        tags["otel_trace_id"] = trace_id
    return event


def setup_sentry(dsn: str | None, *, environment: str, release: str) -> bool:
    if not dsn:
        return False
    if not dsn.startswith("https://"):
        # A placeholder (e.g. the Parameter Store value before you set the real
        # DSN) must not stop the app starting: report it and carry on.
        logging.getLogger("ledgerline").warning("SENTRY_DSN is not a URL; error tracking is off")
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,                  # "which deploy introduced this error?"
        # What Sentry may collect, category by category. Once this dict is given,
        # any category left OUT defaults to collected, so every one is listed.
        data_collection={
            "user_info": False,                              # no user IDs, emails or IP addresses
            "cookies": {"mode": "off"},                      # session cookies identify users
            "http_headers": {"request": {"mode": "denylist"}},  # keep headers, scrub auth/token ones
            "http_bodies": [],                               # form fields may contain invoice data
            "url_query_params": {"mode": "denylist"},
            "gen_ai": {"inputs": False, "outputs": False},  # no prompts or model answers
            "database_query_data": False,                    # no query parameter values
            "stack_frame_variables": False,                  # local variables can hold invoice data
        },
        max_request_body_size="never",
        traces_sample_rate=0,             # tracing is OpenTelemetry's job
        before_send=_sentry_before_send,
    )
    return True
