"""Observability is behaviour too: if nobody tests it, it silently stops working.

These tests use in-memory OpenTelemetry exporters, so they check exactly what
would be sent to a real backend, without one.
"""

import json
import logging

import pytest
from fastapi.testclient import TestClient
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.db import engine
from app.main import app
from app.observability import JsonFormatter, _sentry_before_send, instrument_database, tracer
from tests.helpers import client_for, first, upload

# Global providers can be set once per process; this module owns them.
SPANS = InMemorySpanExporter()
METRICS = InMemoryMetricReader()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(SPANS))
trace.set_tracer_provider(_provider)
metrics.set_meter_provider(MeterProvider(metric_readers=[METRICS]))
instrument_database(engine)


@pytest.fixture()
def json_logs():
    """Capture log lines exactly as they'd be printed in production."""
    lines: list[dict] = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(json.loads(JsonFormatter().format(record)))

    handler = Capture()
    logging.getLogger().addHandler(handler)
    yield lines
    logging.getLogger().removeHandler(handler)


def access_lines(lines):
    return [line for line in lines if line["logger"] == "ledgerline.access"]


def test_every_request_gets_a_request_id_in_header_and_logs(db, json_logs):
    _, users = db
    response = client_for(users["clerk"]).get("/invoices")
    request_id = response.headers["X-Request-ID"]
    assert len(request_id) == 32

    (line,) = access_lines(json_logs)
    assert line["request_id"] == request_id
    assert line["http_route"] == "/invoices" and line["http_status"] == 200
    # Set by the endpoint (auth), visible to the middleware's log line.
    assert line["tenant_id"] == users["clerk"].tenant_id and line["user_id"] == users["clerk"].id


def test_a_valid_incoming_request_id_is_kept_and_junk_is_replaced(db):
    _, users = db
    client = client_for(users["clerk"])
    assert client.get("/login", headers={"X-Request-ID": "lb-abc.123"}).headers["X-Request-ID"] == "lb-abc.123"
    replaced = client.get("/login", headers={"X-Request-ID": "x\n{\"level\":\"FAKE\"}"}).headers["X-Request-ID"]
    assert replaced != "x\n{\"level\":\"FAKE\"}" and len(replaced) == 32  # no log injection


def test_routes_are_logged_as_templates_and_health_checks_stay_quiet(db, json_logs):
    _, users = db
    client = client_for(users["clerk"])
    client.get("/invoices/does-not-exist")
    client.get("/livez")
    lines = access_lines(json_logs)
    assert lines[0]["http_route"] == "/invoices/{invoice_id}"  # groupable: one value per endpoint
    assert len(lines) == 1  # health checks log at DEBUG, below the INFO level used everywhere


def test_health_checks_produce_no_traces(db):
    SPANS.clear()
    TestClient(app).get("/livez")
    assert SPANS.get_finished_spans() == ()


def test_extraction_is_its_own_trace_linked_to_the_upload_request(db, manifest, json_logs):
    _, users = db
    SPANS.clear()
    upload(client_for(users["clerk"]), first(manifest, "clean"))
    spans = {s.name: s for s in SPANS.get_finished_spans()}
    (upload_line,) = access_lines(json_logs)
    request_trace_id = int(upload_line["trace_id"], 16)  # the log line and the trace agree

    request_span = next(s for s in spans.values() if s.attributes.get("http.route") == "/invoices")
    assert request_span.context.trace_id == request_trace_id
    assert request_span.attributes["ledgerline.request_id"] == upload_line["request_id"]

    extraction = spans["extract_invoice"]
    assert extraction.parent is None                                   # a new trace...
    assert extraction.context.trace_id != request_trace_id
    assert extraction.links[0].context.trace_id == request_trace_id    # ...linked to the upload
    assert extraction.attributes["ledgerline.extraction.outcome"] == "needs_review"

    llm = next(s for name, s in spans.items() if name.startswith("generate_content "))
    assert llm.parent.span_id == extraction.context.span_id
    assert llm.attributes["gen_ai.provider.name"] == "fake"
    assert llm.attributes["gen_ai.request.model"] == "gemini-3.5-flash-lite"
    # No invoice content in telemetry: only the agreed attribute names.
    assert all(key.startswith(("gen_ai.", "ledgerline.", "error.")) for key in llm.attributes)


def test_extraction_metrics_count_outcomes(db, manifest):
    _, users = db

    def extracted_count() -> int:
        data = METRICS.get_metrics_data()
        points = [
            p.value
            for rm in (data.resource_metrics if data else [])
            for sm in rm.scope_metrics
            for m in sm.metrics if m.name == "ledgerline.invoices.extracted"
            for p in m.data.data_points if p.attributes["outcome"] == "needs_review"
        ]
        return sum(points)

    before = extracted_count()
    upload(client_for(users["clerk"]), first(manifest, "clean"))
    assert extracted_count() == before + 1


def test_debug_error_route_fails_outside_production(db):
    _, users = db
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/_debug/error").status_code == 500


def test_sentry_events_carry_request_and_trace_ids():
    from app.observability import _log_context

    _log_context.set({"request_id": "req-1", "tenant_id": "t-1", "user_id": "u-1"})
    with tracer.start_as_current_span("x") as span:
        event = _sentry_before_send({"message": "boom"}, {})
    assert event["tags"]["request_id"] == "req-1" and event["tags"]["tenant_id"] == "t-1"
    assert event["tags"]["otel_trace_id"] == format(span.get_span_context().trace_id, "032x")
    assert "user_id" not in event["tags"]  # user IDs stay out of the third-party tool


def test_database_queries_are_spans_without_values(db):
    _, users = db
    SPANS.clear()
    client_for(users["clerk"]).get("/invoices")
    spans = SPANS.get_finished_spans()
    request = next(s for s in spans if s.attributes.get("http.route") == "/invoices")
    queries = [s for s in spans if s.attributes.get("db.system.name") == "sqlite"]
    assert queries and all(q.context.trace_id == request.context.trace_id for q in queries)
    # The user ID is a query parameter; only the placeholder may appear.
    assert not any(users["clerk"].id in q.attributes["db.query.text"] for q in queries)


def test_llm_calls_and_extractions_log_metric_ready_events(db, manifest, json_logs):
    """CloudWatch metric filters (infra/staging/observability.tf) match these fields."""
    _, users = db
    upload(client_for(users["clerk"]), first(manifest, "clean"))
    llm = next(line for line in json_logs if line.get("event") == "llm_call")
    assert llm["llm_cost_usd"] == 0 and llm["input_tokens"] == 0 and "duration_ms" in llm
    done = next(line for line in json_logs if line.get("event") == "extraction_finished")
    assert done["outcome"] == "needs_review" and isinstance(done["duration_ms"], int)


def test_placeholder_sentry_dsn_does_not_stop_the_app():
    from app.observability import setup_sentry

    assert setup_sentry("not-set", environment="staging", release="x") is False
