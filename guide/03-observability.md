# Chapter 3: Observability

> **In this chapter:** knowing what your system is doing, and finding out it's broken before your users tell you. Covers the four signals (logs, metrics, traces and errors) and the one idea that makes them useful together: correlation. Then structured logging, metrics and the cardinality trap, distributed tracing with OpenTelemetry, error tracking, what's different about AI systems, SLOs and alerting that people don't learn to ignore, the tool landscape with real prices, and what observability costs.
>
> **Part 2, [Observability on AWS](03b-observability-on-aws.md),** takes this to the staging environment: dashboards, alarms that email you, uptime checks and SLOs, all in Terraform.

---

## 3.1 Why it matters

At the end of Chapter 2, Ledgerline was running on AWS. Then someone asked: *is it working?* The only honest answer was "let me open it and check". That works for one developer and one environment, for about a week.

Questions you will be asked, usually at a bad time:

- "A customer says uploads are slow. Are they?"
- "Did yesterday's deploy break anything?"
- "Why did this invoice end up as `extraction_failed`?"
- "How much did we spend on the LLM this week, and for which customer?"
- "Is it down, or is it just me?"

Without observability, each one starts an investigation: SSH-ing around, adding print statements, redeploying, waiting for it to happen again. With it, each is a query.

> **Observability is being able to answer a question about your running system that you didn't think to ask in advance, without shipping new code.**

**Monitoring** watches for problems you predicted ("alert if the site is down"). **Observability** lets you investigate problems you didn't ("why are invoices from this one vendor slow, but only on Tuesdays?"). You need both. Monitoring tells you *that* something is wrong. Observability tells you *what* and *why*.

The cost of not having it isn't only slower debugging. Teams without it:

- **hear about outages from customers**, which damages trust in a way the outage alone doesn't;
- **can't tell whether a change helped**, so they argue from opinion;
- **fear deploying**, because they can't see what a deploy did. That's Chapter 2's 3 a.m. test failing for a different reason.

---

## 3.2 The four signals

| Signal | What it is | Answers | Cost grows with | Example (Ledgerline) |
|---|---|---|---|---|
| **Logs** | Timestamped records of events | "What happened, in detail, for this request?" | Volume: every line is stored | `{"message": "Extraction finished: needs_review", "invoice_id": "…", "duration_ms": 412}` |
| **Metrics** | Numbers aggregated over time | "How much, how often, how fast — overall and trending?" | Cardinality: the number of distinct label combinations | `ledgerline.invoices.extracted{outcome="needs_review"} = 1,284` |
| **Traces** | The path of one request through the system, as a tree of timed *spans* | "Where did the time go? Which step failed?" | Volume of requests × spans per request (so you sample) | `POST /invoices` → `INSERT` → `SELECT`; linked `extract_invoice` → `generate_content` → `UPDATE` |
| **Errors** | Exceptions, grouped by cause, with stack traces | "What's breaking, since when, how often, after which release?" | Number of errors | `RuntimeError` in `extract_invoice`, 37 events, first seen in release `1bf430f` |

Two more that appear at the growth stage:

- **Profiles**: where CPU and memory go *inside* the code, function by function. Useful when a service is slow and the trace says "it's in this one function".
- **Events and audit logs**: business facts ("invoice approved by Arjun"). Ledgerline's `audit_events` table is one. These are records the business owns, not telemetry, and they have different rules for retention and access.

**They're not alternatives; each is cheap for one job and expensive for others.** Counting requests from logs means storing and scanning every line; a metric does it with one number. Finding out why *one* request failed from metrics is impossible; a trace or a log shows it immediately.

```
alert fires (metric) ──▶ which requests? (trace) ──▶ what exactly happened? (logs) ──▶ which code? (error)
```

---

## 3.3 Correlation: the idea that makes it work

Four signals in four tools are four separate puzzles, unless they share an ID.

> **Every log line, span and error carries the same request ID and trace ID. One ID takes you from an alert to the cause.**

How Ledgerline does it:

1. **Every request gets a request ID.** Taken from the incoming `X-Request-ID` header if it looks like an ID (a load balancer or another service may set one), otherwise generated. It's returned in the response header, so a user can quote it in a bug report.
2. **Every request gets a trace ID** from OpenTelemetry.
3. **Both are added to every log line automatically**, along with the tenant and user once they're known. Nobody has to remember to pass them to `log.info()`.
4. **Sentry errors are tagged with both**, so an error links to its logs and trace.
5. **Background work is linked to the request that started it** (section 3.6).

```json
{"time": "2026-10-30T13:09:30.941+00:00", "level": "INFO", "logger": "ledgerline.access",
 "message": "POST /invoices 303", "request_id": "52d29237…", "tenant_id": "ec782795…",
 "user_id": "95cc1e7b…", "trace_id": "56ef4574…", "span_id": "…",
 "http_method": "POST", "http_route": "/invoices", "http_status": 303, "duration_ms": 16.9}
```

With this line, you can search logs for everything that request did, open its trace by `trace_id`, find the user's other requests by `user_id`, and count slow uploads per tenant.

> **How the context travels.** In Python, the request ID is stored in a `contextvar`, which follows the request through `await`, into FastAPI's thread pool and into background tasks. A global variable or a thread-local would mix up concurrent requests. One subtlety cost us a test failure: FastAPI runs the endpoint in a *child* task, so a value the endpoint *sets* (the tenant, after login) is invisible to the middleware that writes the access log. Ledgerline stores a small dict per request in the contextvar and has the endpoint add to it, so both see the same values.

---

## 3.4 Logs done properly

### Structured, not prose

```text
# Prose: easy to write, hard to use
2026-10-30 13:09:30 INFO Invoice 7507ce9f extracted for tenant ec782795 in 412ms (needs_review)

# Structured: every field is queryable
{"time": "…", "level": "INFO", "message": "Extraction finished: needs_review",
 "invoice_id": "7507ce9f…", "tenant_id": "ec782795…", "outcome": "needs_review", "duration_ms": 412}
```

With the prose version, "average extraction time for this tenant" needs a regular expression that breaks the day someone rewords the message. With JSON, it's a query:

```sql
-- CloudWatch Logs Insights (part 2); every log tool has an equivalent
fields duration_ms
| filter outcome = "needs_review" and tenant_id = "ec782795…"
| stats avg(duration_ms), pct(duration_ms, 95) by bin(1h)
```

In Python, add fields with `extra=`:

```python
log.info("Extraction finished: %s", outcome,
         extra={"invoice_id": invoice_id, "outcome": outcome, "duration_ms": duration_ms})
```

Ledgerline's `JsonFormatter` ([`app/observability.py`](../ledgerline/app/observability.py)) writes one JSON object per line with time, level, logger, message, the request context, trace IDs, any `extra` fields and the stack trace for exceptions. Many libraries do the same (`python-json-logger`, `structlog`); it's 30 lines to write your own and understand it.

**Logs go to standard output.** The app doesn't send them anywhere; the platform collects them (Docker, ECS → CloudWatch, Kubernetes → a log agent). This is one of the [twelve-factor](https://12factor.net/logs) rules: the app shouldn't care where logs end up, and log shipping never slows down or crashes a request.

### What to log

Log **events that matter**, with the fields needed to understand them:

| Log | Don't log |
|---|---|
| Each request, once: method, route, status, duration (the *access log*) | Every function entered and exited |
| State changes: "extraction finished", "invoice approved" | "Starting loop iteration 4" |
| Decisions: "retrying after 429, attempt 2" | Success messages for things that almost always succeed |
| Failures, with the exception | The same failure at every layer it passes through |
| Start-up configuration (minus secrets): which LLM provider, which storage | Health-check requests at INFO (see below) |

**Levels mean something.** `ERROR`: something failed that someone may need to act on. `WARNING`: something unexpected that was handled. `INFO`: normal significant events. `DEBUG`: detail for development, off in production. If `ERROR` lines appear constantly during normal operation, nobody will look at them when it matters.

**Log the route template, not the path.** `/invoices/{invoice_id}` has one value per endpoint; `/invoices/7507ce9f-…` has one per invoice. You can group, count and alert on the first.

**Health checks are noise.** A load balancer checking `/readyz` every 15 seconds on two targets is 11,520 log lines a day that say nothing. Ledgerline logs them at `DEBUG` and excludes them from traces and metrics.

### What never to log

Logs get copied, exported, shared in tickets and kept for months, by people and systems with broader access than your database. Never log:

- **Secrets:** passwords, API keys, tokens, session cookies, `Authorization` headers, database URLs with passwords in them.
- **Personal data** you don't need: names, emails, phone numbers, addresses, IP addresses where regulations treat them as personal. Use IDs (`user_id`) instead; the ID can be looked up by someone with access to the database.
- **Request and response bodies.** They contain whatever the user sent. For Ledgerline that's invoices: vendor names, tax IDs, bank details.
- **LLM prompts and outputs**, for the same reason. Ledgerline keeps the model's answer in the `extraction_runs` table, under the database's access controls, not in logs.

> **The cheapest PII leak to fix is the one that never happened.** Decide what goes in logs at the start, write it down, and test it. Ledgerline's tests check that database spans contain no parameter values and that LLM spans carry only agreed attributes.

### Retention and volume

Logs are usually the biggest observability cost, and the one that grows without anyone deciding it should. Set retention explicitly (Ledgerline staging: 14 days), drop noise at the source, and consider keeping `DEBUG` off and `INFO` lean. Compliance may require longer retention for *audit* events; keep those separate from debugging logs, as Ledgerline does with its audit table.

---

## 3.5 Metrics

### Three types cover nearly everything

| Type | What it records | Example | Typical question |
|---|---|---|---|
| **Counter** | A number that only goes up | Requests served, invoices extracted, dollars spent | "How many per minute?" (the *rate*) |
| **Gauge** | A value that goes up and down | Requests in progress, queue length, memory used | "What is it now?" |
| **Histogram** | The distribution of values | Request duration, tokens per LLM call | "What's the 95th percentile?" |

**Use percentiles, not averages, for latency.** If 95 requests take 100 ms and 5 take 10 seconds, the average is 595 ms, which describes no request at all, and it hides that one user in twenty is having a terrible time. The 95th or 99th percentile (p95, p99) shows it.

### What to measure

Two standard checklists:

- **The four golden signals** (from Google's SRE book): **latency**, **traffic**, **errors**, **saturation** (how full the system is).
- **RED**, for request-driven services: **R**ate, **E**rrors, **D**uration. **USE**, for resources: **U**tilisation, **S**aturation, **E**rrors.

Then add the metrics the business cares about. These are often the most useful and the most forgotten:

| Ledgerline metric | Type | Why |
|---|---|---|
| `http.server.request.duration` | Histogram | Latency, traffic and errors (by status code) for every endpoint. Comes free from FastAPI's built-in OpenTelemetry |
| `ledgerline.invoices.extracted` by `outcome` | Counter | Is the core job working? A rising share of `extraction_failed` is the first sign of a model or provider problem |
| `ledgerline.extraction.duration` | Histogram | How long until a user can review an invoice, including retries |
| `gen_ai.client.token.usage` by `gen_ai.token.type` | Histogram | Tokens per call: catches prompts that quietly got bigger |
| `gen_ai.client.operation.duration` by `error.type` | Histogram | LLM latency and failure rate, separate from the app's |
| `ledgerline.llm.cost` by model | Counter | Spend, in dollars, as it happens, not at the end of the month |

### The cardinality trap

A metric's **cardinality** is the number of distinct combinations of its labels (also called attributes or tags). Each combination is a separate time series that the backend stores and bills for.

```
http.server.request.duration{route, method, status}
  ~20 routes × 4 methods × 6 statuses = ~480 series            fine

…plus {user_id}
  × 10,000 users = 4,800,000 series                            a very large bill, or a crashed backend
```

> **Never use unbounded values as metric labels:** user IDs, invoice IDs, request IDs, email addresses, raw URLs, error messages. Those belong in logs and traces, which are built for high-cardinality data. Metric labels should have a small, known set of values: route templates, status codes, outcomes, model names, and tenant IDs only while you have tens of tenants, not thousands.

### Naming

Use the names from OpenTelemetry's **semantic conventions** when one exists (`http.server.request.duration`, `gen_ai.client.token.usage`); backends and dashboards understand them without configuration. Prefix your own with the service or company (`ledgerline.invoices.extracted`), and include the unit in the definition, not the name.

---

## 3.6 Traces

### Spans and traces

A **span** is one timed operation: an HTTP request, a database query, an LLM call. Spans nest: a request span contains the query spans it caused. A **trace** is the whole tree for one request.

```
POST /invoices                                          ██████████████████  16.9 ms
├─ fastapi.dependencies                                 ██                   1.1 ms
│  └─ SELECT (users)                                    █                    0.4 ms
├─ fastapi.endpoint                                     █████████████       12.0 ms
│  ├─ SELECT (documents)                                █                    0.3 ms
│  ├─ INSERT (documents)                                █                    0.5 ms
│  ├─ INSERT (invoices)                                 █                    0.4 ms
│  └─ INSERT (audit_events)                             █                    0.4 ms
└─ fastapi.background_task  ┄┄ link ┄┄▶  extract_invoice (its own trace)
```

A trace answers questions that logs and metrics can't: *where did the time go?* (one query took 4 seconds), *what called what?*, *which step failed first?*

### Propagation: one trace across services

When service A calls service B, the trace continues if A sends its trace context along. The standard is the W3C **`traceparent`** HTTP header, which OpenTelemetry adds and reads automatically for instrumented HTTP clients and servers. Ledgerline's outbound calls to the Gemini API are instrumented the same way; the span shows the call even though Google's side isn't in our trace.

### Automatic and manual instrumentation

- **Automatic:** libraries create spans for you: the web framework (requests), the database driver (queries), HTTP clients (outbound calls). Start here; it's most of the value for the least work.
- **Manual:** you add spans for operations that matter to *your* domain. Ledgerline adds `extract_invoice` and one span per LLM call, with attributes for the model, tokens, cost and attempt number.

```python
with tracer.start_as_current_span(f"generate_content {model}", attributes={
    "gen_ai.operation.name": "generate_content",
    "gen_ai.provider.name": provider.name,
    "gen_ai.request.model": model,
}) as span:
    result = provider.generate_json(...)
    span.set_attributes({
        "gen_ai.usage.input_tokens": result.input_tokens,
        "gen_ai.usage.output_tokens": result.output_tokens,
        "ledgerline.llm.cost_usd": float(result.cost_usd),
    })
```

### Background work: a new trace, linked

The upload request returns in 17 ms; extraction then takes anywhere from 1 to 60 seconds. If extraction were a child span of the request, the request's trace would stretch to 60 seconds and every "slow request" analysis would be wrong. When extraction moves to a separate worker (Chapter 6), there won't even be a request in the same process.

So Ledgerline starts a **new trace** for extraction and adds a **span link** to the request that triggered it:

```python
caller = trace.get_current_span().get_span_context()
with tracer.start_as_current_span(
    "extract_invoice",
    context=otel_context.Context(),                       # empty context: start a new trace
    links=[trace.Link(caller)] if caller.is_valid else [],
):
    ...
```

In a trace viewer, you can jump from the extraction to the upload and back. This is the standard pattern for queues, schedulers and any work that outlives the request.

### Sampling

Tracing every request is fine at low traffic and expensive at high traffic. **Sampling** keeps a fraction:

- **Head sampling** decides at the start of a trace ("keep 10%"). Cheap and simple; set with the standard `OTEL_TRACES_SAMPLER=parentbased_traceidratio` and `OTEL_TRACES_SAMPLER_ARG=0.1`. Its weakness: it decides before knowing whether the request will fail, so it throws away 90% of your errors too.
- **Tail sampling** decides after the trace is complete ("keep all errors, all slow traces, and 5% of the rest"). Much more useful, but it needs a collector that holds traces in memory until they finish.

Ledgerline keeps every trace for now. At its traffic, that costs almost nothing. Revisit when traces become a noticeable line on the bill.

---

## 3.7 OpenTelemetry

**OpenTelemetry (OTel)** is the open standard for producing telemetry, run by the Cloud Native Computing Foundation. It's what lets the instrumentation in your code stay the same while the backend changes.

| Part | What it is |
|---|---|
| **API** | What your code calls: `tracer.start_as_current_span()`, `meter.create_counter()`. Does nothing until an SDK is configured, so libraries can use it safely |
| **SDK** | The implementation: sampling, batching, resource attributes |
| **Instrumentation** | Libraries that create spans and metrics for frameworks and clients (and, increasingly, frameworks that do it themselves) |
| **OTLP** | The wire protocol for sending telemetry. Every major backend accepts it |
| **Collector** | An optional separate process that receives, processes (filter, sample, redact, batch) and forwards telemetry to one or more backends |
| **Semantic conventions** | Agreed names for common attributes and metrics: `http.route`, `db.system.name`, `gen_ai.usage.input_tokens` |

### Configured by standard environment variables

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-lgtm:4318   # where to send (local Grafana stack here)
OTEL_SERVICE_NAME=ledgerline
OTEL_TRACES_SAMPLER=parentbased_traceidratio        # optional sampling
OTEL_TRACES_SAMPLER_ARG=0.1
OTEL_EXPORTER_OTLP_HEADERS=api-key=…                # for hosted backends that need a key
```

Moving from the local Grafana stack to AWS, Grafana Cloud, Honeycomb or Datadog means changing these variables, not the code. Ledgerline turns OTel export off entirely when `OTEL_EXPORTER_OTLP_ENDPOINT` isn't set, so tests and a bare `docker run` need nothing.

Every span and metric carries a **resource** describing what sent it: `service.name`, `service.version` (Ledgerline uses the git SHA, so every span says which deploy produced it) and `deployment.environment.name`.

### Direct export or a collector?

```
Small:   app ──OTLP──▶ backend
Growth:  app ──OTLP──▶ collector (sidecar or agent) ──▶ backend(s)
```

Start direct. Add a collector when you need tail sampling, want to scrub data centrally before it leaves your network, send to two backends during a migration, or want the app to hand off telemetry instantly to a local process. Part 2 uses a collector sidecar on ECS to send to AWS.

### Worked example: what changed in Ledgerline, and what we learned

> **FastAPI now has OpenTelemetry built in.** In recent versions (Ledgerline is on 0.162), `FastAPI(telemetry={...})` creates request spans, HTTP metrics, and spans for dependencies, the endpoint and background tasks, without the separate `opentelemetry-instrumentation-fastapi` package. It uses the global providers the app configures. Ledgerline uses it to exclude health checks from traces and metrics, and turns off its exception logs because Sentry reports those. We found out because our first test failed: two systems were creating request spans. **Check your framework's current docs before adding instrumentation packages; the ecosystem moves fast.**
>
> **Instrumentation lags behind libraries.** The SQLAlchemy instrumentation package (October 2026) supported SQLAlchemy up to 2.0, and we were on 2.1. It didn't fail; it logged one line at start-up and instrumented *nothing*. We replaced it with 30 lines using SQLAlchemy's own events (`before_cursor_execute` / `after_cursor_execute`), recording each statement with placeholders, never values. **Read your start-up logs after adding instrumentation, and test that the spans you expect exist.** Observability that silently stops working is worse than none, because you trust it.

> **Tests inherit the environment.** The tests passed on a laptop and failed inside Docker. Compose sets `OTEL_EXPORTER_OTLP_ENDPOINT`, and `.env` sets `SENTRY_DSN`, so importing the app in the test run switched on the real exporters. OpenTelemetry allows one global provider, so the tests' in-memory exporters were silently ignored, and the test suite was **sending telemetry to Grafana and errors to Sentry**. The fix: the test setup removes every telemetry variable before importing the app. Any code that turns behaviour on from environment variables needs the test setup to turn it off explicitly.

The full setup is in [`app/observability.py`](../ledgerline/app/observability.py); the tests that keep it honest are in [`tests/test_observability.py`](../ledgerline/tests/test_observability.py). They use OpenTelemetry's in-memory exporters to check exactly what would be sent: request spans, the extraction trace and its link, query spans without values, LLM spans with only agreed attributes, and metric counts.

---

## 3.8 Errors

Logs record errors; an **error tracker** turns them into a work list:

- **Grouping:** 5,000 occurrences of the same bug become one *issue*, with a count and a graph.
- **Context:** stack trace with local variables, the release, the environment, tags (request ID, tenant), and the breadcrumbs (log lines) leading up to it.
- **Lifecycle:** new, ongoing, resolved, *regressed* (came back after being marked fixed).
- **Release tracking:** "this error first appeared in release `1bf430f`" points straight at the deploy that caused it.
- **Alerts:** "a new issue appeared" is one of the most useful alerts a small team can have.

Sentry is the best-known; Rollbar, Bugsnag and Honeybadger are alternatives, and Datadog, New Relic and Grafana have error tracking inside their platforms.

### Privacy settings matter more here

An error tracker is a third party receiving data from inside your app, with stack traces that can include local variables. Ledgerline's Sentry setup:

```python
sentry_sdk.init(
    dsn=settings.sentry_dsn,
    environment=settings.app_env,
    release=settings.git_sha,          # "which deploy introduced this error?"
    data_collection={                  # what Sentry may collect, per category
        "user_info": False,                                 # no user IDs, emails or IP addresses
        "cookies": {"mode": "off"},
        "http_headers": {"request": {"mode": "denylist"}},  # keep headers, scrub auth/token ones
        "http_bodies": [],                                  # form fields may contain invoice data
        "url_query_params": {"mode": "denylist"},
        "gen_ai": {"inputs": False, "outputs": False},     # no prompts or model answers
        "database_query_data": False,
        "stack_frame_variables": False,                     # local variables can hold invoice data
    },
    traces_sample_rate=0,              # tracing is OpenTelemetry's job
    before_send=_sentry_before_send,   # adds request_id, tenant_id and the OTel trace ID as tags
)
```

> **Read the defaults of privacy settings, not just their names.** Sentry's SDK replaced the old `send_default_pii=False` switch with `data_collection` in 2026. Once you pass `data_collection`, **every category you leave out defaults to collected**, including user info. A config that sets only `{"http_bodies": []}` looks careful and sends more than the old one did. List every category, and print the resolved settings once (`sentry_sdk.get_client().options["data_collection"]`) to check.

Turning off stack-frame variables is a real trade-off: they're often the fastest way to understand a bug. For an app handling financial documents, we'd rather lose that than ship invoice data to a third party. An app without sensitive data might keep them, with the default scrubbing of names like `password` and `token`.

The `user_id` deliberately isn't sent to Sentry; the `request_id` is enough to find it in our own logs. The DSN (Sentry's project address) only allows sending events, not reading them, but it's still kept out of git and stored like other configuration secrets.

### One job per tool

Most tools now do everything: Sentry offers tracing, logs, metrics and profiling; Datadog and Grafana offer error tracking. Turning everything on everywhere means paying twice, sending sensitive data to more places, and never being sure which dashboard is right. Decide which tool owns which signal:

| Signal | Ledgerline uses | Not |
|---|---|---|
| Logs | stdout → platform (Docker locally, CloudWatch on AWS) | Sentry logs |
| Traces, metrics | OpenTelemetry → Grafana stack locally, AWS on staging | Sentry tracing (off), Sentry metrics |
| Errors | Sentry | — |
| Profiling | Nothing yet | Sentry profiling (usage-billed after the trial) |

---

## 3.9 Observability for AI systems

An LLM feature adds failure modes that ordinary monitoring doesn't see.

| What can go wrong | How it shows up | What to watch |
|---|---|---|
| Provider outage or rate limiting | Errors, retries, latency | `gen_ai.client.operation.duration` by `error.type`; retry counts |
| Model retired or silently redirected | Errors (404), or quietly different behaviour | `gen_ai.response.model` vs `gen_ai.request.model`; Chapter 1's deprecation story |
| Prompt grew (more context, longer documents) | Cost and latency creep up slowly | Tokens per call (histogram), cost per invoice |
| **Quality degrades** | **Nothing errors.** Answers are just worse | Production quality signals (below) |
| Runaway usage (a loop, an abusive tenant) | Cost spike | Cost per hour, per tenant; budget alerts |

The hardest row is quality, because **a wrong answer looks exactly like a right answer** to every metric in this chapter. Ways to see it:

- **Validation failures**: the model's output didn't match the schema. Ledgerline records these as `Invalid model output`.
- **Business checks that fail**: totals don't add up, the tax ID's checksum is wrong, no matching purchase order. Ledgerline's checks already produce these flags; a rising rate is a quality signal.
- **Human corrections**: every field a reviewer edits before approving is a labelled example of the model being wrong. The *correction rate* is the closest thing to production accuracy you get for free.
- **Sampled review and online evals**: periodically run a sample of production inputs (with consent and safe data handling) through your eval set, or re-check them with a stronger model.

Record **model, prompt version and provider** on every LLM span and stored result. When quality changes, the first question is "what changed?", and the answer is often "the prompt" or "the model behind the alias".

**Don't put prompts and outputs in general-purpose telemetry** unless you've decided that's acceptable for the data involved. OpenTelemetry's GenAI conventions define attributes for message content, but make capturing it opt-in for this reason. Specialised LLM observability tools (Langfuse, LangSmith, Arize Phoenix, Helicone and others) capture full prompts and responses for debugging and evaluation; treat them as a place where customer data lives, with the same controls. Chapter 9 goes deeper.

---

## 3.10 SLOs and alerting

### Alert on symptoms, not causes

An alert should mean **a user is affected, or soon will be, and a human needs to act**. Everything else is a dashboard or a ticket.

| Alert on (symptoms) | Don't page on (causes) |
|---|---|
| The site isn't responding | CPU at 85% |
| More than 2% of requests failing | One container restarted |
| Invoices waiting for extraction longer than 10 minutes | The database has 50 connections |
| LLM spend in the last hour far above normal | A single 500 error |

Causes are useful *while investigating*. As alerts, they fire when nothing is wrong (high CPU during a busy, healthy hour) and stay silent when something is (a bug that returns errors quickly and uses little CPU).

> **Alert fatigue is the failure mode.** When alerts fire often and usually don't matter, people mute them, and then miss the one that does. Every alert should be actionable, and every page should be rare enough to be taken seriously. If an alert fires and the right response is "ignore it", delete or fix the alert.

### SLIs, SLOs and error budgets

- **SLI** (service level *indicator*): a measurement of user experience. "The share of `/invoices` requests that succeed in under 1 second."
- **SLO** (service level *objective*): the target. "99.5% over 30 days."
- **Error budget**: what the SLO allows to go wrong. 99.5% over 30 days is about **3.6 hours** of failure. While budget remains, ship freely; when it's running out, slow down and fix reliability.

| | 99% | 99.5% | 99.9% | 99.99% |
|---|---|---|---|---|
| Allowed failure per 30 days | 7.2 hours | 3.6 hours | 43 minutes | 4.3 minutes |

Each extra nine costs a lot more (redundancy, on-call, slower change). **Pick the lowest target your users will actually be happy with.** An SLA (service level *agreement*) is a contractual promise, with penalties, to customers; it should always be looser than your internal SLO.

Ledgerline's first SLOs (implemented with alarms in part 2):

| SLI | SLO |
|---|---|
| Web requests (excluding health checks) answered without a 5xx | 99.5% over 30 days |
| Invoices reaching `needs_review` or `extraction_failed` within 2 minutes of upload | 95% |
| The site responds to an external check | 99.5% |

### Burn-rate alerts

"Alert when the error rate exceeds 0.5%" fires on every small blip. A **burn-rate** alert asks instead: *at the current error rate, how fast are we using the month's budget?* Burning it 14× faster than sustainable (the whole budget in about two days) is a page; 2× is a ticket. Google's SRE workbook describes the standard multi-window version. Small teams can start with two simple alerts — a fast one (high error rate over 5 minutes) and a slow one (elevated error rate over 6 hours) — and grow from there.

### Every alert has a runbook

A **runbook** says what the alert means, how to check whether it's real, and the first steps to take. It can be three lines in the repository. At 3 a.m., a runbook is the difference between following steps and guessing. Link it from the alert itself.

### On-call, by stage

- **MVP, early production:** alerts go to the founders' email or a chat channel. One person "owns" them each week, even informally.
- **Growth:** a paging tool (PagerDuty, incident.io, Grafana OnCall, Jira Service Management) with a rotation, escalation and a quiet process for handing over.
- **Enterprise:** follow-the-sun or tiered on-call, incident roles, status pages and post-incident reviews (Chapter 14).

---

## 3.11 Dashboards

Fewer, better dashboards beat many abandoned ones. A useful default, one per service:

1. **Golden signals:** request rate, error rate, p95 and p99 latency, by route.
2. **The core job:** for Ledgerline, invoices extracted by outcome, extraction duration, invoices waiting.
3. **Dependencies:** database latency and connections; LLM latency, errors and retries.
4. **Cost:** LLM spend per hour and per tenant; anything else metered.
5. **Deploys marked on the timeline**, so "it got worse after the deploy" is visible at a glance.

Put the SLOs at the top. A dashboard nobody opens unless an alert fires is fine; a dashboard that needs explaining isn't.

---

## 3.12 Tools: what companies use, and what they cost

What teams actually run, as of 2026:

- **Mid-size startups:** Sentry for errors almost universally. Datadog as the most common all-in-one platform, and its bill as a frequent complaint. Grafana Cloud or Honeycomb for cost-conscious teams. The cloud provider's tool (CloudWatch, Google Cloud Observability, Azure Monitor) for infrastructure basics.
- **Enterprises:** Datadog, Dynatrace, Splunk or New Relic; or self-run Prometheus and Grafana with Elastic or Loki for logs. Often several at once.
- **The common thread:** OpenTelemetry for instrumentation. In Grafana Labs' 2026 survey, 76% of organisations reported investing in it, and "freedom to switch vendors" was among the top reasons.

Approximate list prices (2026; all change often, and enterprise deals are negotiated, so check before budgeting):

| Tool | Model | Free tier | Paid, roughly |
|---|---|---|---|
| **AWS CloudWatch** | Pay per use | 5 GB logs, 10 custom metrics, 10 alarms, 3 dashboards per month | Logs ~$0.50/GB ingested (us-east-1); custom metrics ~$0.30 each per month; alarms ~$0.10 each per month; X-Ray $5 per million traces |
| **Grafana Cloud** | Per usage, $19/month platform fee on Pro | 10k metric series, 50 GB each of logs and traces, 14-day retention | ~$6.50 per 1,000 metric series; logs and traces ~$0.45/GB |
| **Datadog** | Per host plus per usage | 14-day trial | Infrastructure from ~$15/host/month; APM ~$31/host; logs $0.10/GB ingested plus ~$1.70 per million indexed events |
| **Sentry** | Per event volume | 5,000 errors/month, 1 user | Team ~$26/month; Business ~$80/month |
| **Honeycomb, New Relic, Dynatrace, Elastic, Splunk** | Varies (events, data ingested, hosts) | Most have a free tier or trial | Compare with your own data volumes |
| **Self-hosted** (Prometheus, Grafana, Loki, Tempo, OpenSearch) | Your servers and your time | Free software | Infrastructure plus the engineering time to run, upgrade and scale it |

How to choose:

1. **Instrument with OpenTelemetry regardless.** It's the part you don't want to redo.
2. **Start with your cloud's tool plus an error tracker.** Cheapest, nothing to run, good enough for early production.
3. **Move to a dedicated platform when debugging takes too long or the cloud tool's limits hurt** — typically at the growth stage, with several services.
4. **Estimate cost with your real volumes before committing.** Log volume and metric cardinality dominate every bill.
5. **Self-host only with a reason** (data residency, scale where SaaS pricing breaks, an existing platform team). Running a telemetry stack is a product of its own.

---

## 3.13 By stage

| | MVP | Early production | Growth | Enterprise |
|---|---|---|---|---|
| Logs | Platform's default | **Structured JSON with request and trace IDs, retention set** | Central log search across services; sampling of noisy logs | Retention by data class; access controls and audit on logs |
| Metrics | Platform's built-in | **Golden signals + core business metrics** | Per-service dashboards, SLOs | SLOs per customer tier; capacity planning |
| Traces | None, or automatic only | **OTel automatic + key manual spans** | Across services; tail sampling via a collector | Organisation-wide standards; sampling budgets |
| Errors | Error tracker free tier | **Error tracker with releases, new-issue alerts** | Ownership per team; triage rotation | Integrated with incident management |
| Alerting | "Is it up?" check | **A few symptom alerts to email or chat; runbooks** | Burn-rate alerts, paging rotation | Tiered on-call, incident process, status page |
| AI | Log cost per call | **Tokens, cost, failures, model version as telemetry** | Quality signals, online evals, per-tenant cost | Audit trails for model decisions; drift reviews |

---

## 3.14 By product type

- **B2B SaaS:** per-tenant views matter: "is it slow for everyone or just Acme?". Tenant ID on logs and traces (not as a metric label once you have many tenants). Enterprise customers will ask for your uptime history and incident process; SLOs give you the data.
- **AI / LLM products:** cost and quality are first-class signals, not afterthoughts. Watch the provider separately from your own app. Treat any tool that stores prompts as holding customer data.
- **Agentic workflows:** one task can involve many LLM calls and tool calls over minutes. Trace each step as a span, record which tools were called with which (non-sensitive) arguments, and alert on loops: steps per task and cost per task with hard limits (Chapter 10).
- **Internal tools:** often fine with error tracking plus an uptime check. Usage metrics help decide what's worth maintaining.
- **Consumer apps:** volume makes sampling and cost control essential from the start. Real user monitoring (performance measured in users' browsers and phones) matters, because the backend can be fast while the app is slow.

---

## 3.15 Challenges and watch-outs

- **The bill nobody decided on.** Logs at `DEBUG`, unlimited retention, a high-cardinality label, tracing every health check. Review observability cost monthly, like any other.
- **Personal data leaking into telemetry**, and from there to third parties. Decide what's allowed, configure SDKs conservatively, test it.
- **Silent instrumentation failure.** An exporter pointing at the wrong address, an instrumentation package that doesn't support your library version. Check that telemetry arrives after every significant change; alert on "no data" for key metrics.
- **Alert fatigue.** Too many alerts is worse than too few, because it trains people to ignore all of them.
- **Green dashboards, angry users.** Your metrics measure what your server sees. If users can't reach you (DNS, CDN, their network), or the frontend is broken, server metrics look fine. Add an external check and, for user-facing products, real user monitoring.
- **Averages.** They hide the slow minority. Use percentiles.
- **Too many tools.** Each tool needs access control, cost review and someone who knows it. One job per tool.
- **Signals you outgrow:** debugging regularly needs SSH or ad-hoc prints; nobody can say what the error rate was last week; you learn about incidents from customers; telemetry costs grow faster than traffic.

---

## 3.16 Exercise

For each, decide: **page** someone now, **ticket** for working hours, **dashboard** only, or **don't collect**.

1. The external uptime check has failed from three locations for 3 minutes.
2. Memory usage of the app container is at 80%.
3. A new kind of exception has appeared in Sentry for the first time, once.
4. LLM spend in the last hour is 8× the same hour last week.
5. The p99 latency of `/invoices` has slowly risen 20% over a month.
6. Adding the user's email address as a label on the request-duration metric, "to see who's affected".
7. 4% of extractions failed with `Invalid model output` today, up from 0.5%.

<details>
<summary>Suggested answers</summary>

1. **Page.** A real symptom, confirmed from several places, for long enough not to be a blip.
2. **Dashboard.** A cause, not a symptom. If it's climbing toward the limit over days, a ticket; if containers start being killed, the error rate alert will catch the effect.
3. **Ticket** (or a chat notification). New issues deserve a look the same day; one occurrence rarely deserves waking someone.
4. **Page, or an urgent notification**, depending on the amount. Runaway spend can be a loop, abuse or a leaked key, and it compounds every hour.
5. **Ticket.** A trend to investigate, not an emergency. This is what dashboards and weekly reviews are for.
6. **Don't collect.** Unbounded cardinality and personal data in a metric. Use `user_id` on traces and logs instead.
7. **Ticket, urgently**, and probably a chat alert. Quality is degrading without errors: check whether the model, the prompt or the input mix changed.

</details>

---

## 3.17 Cost

Rough monthly costs for Ledgerline's approach at each stage (approximate; volumes matter more than tool choice):

| Stage | Setup | Approx. monthly |
|---|---|---|
| MVP | Platform logs, error tracker free tier | $0 |
| Early production (this chapter) | CloudWatch logs (a few GB), a handful of alarms and custom metrics, a dashboard, an uptime check; Sentry free tier | ~$5–15 |
| Growth (several services, real traffic) | Grafana Cloud or Datadog for a few hosts, Sentry Team, a paging tool | ~$200–1,500 |
| Enterprise | Platform contract, retention requirements, many teams | Negotiated; often a significant line next to compute itself |

The things that move the number most: **log volume × retention**, **metric cardinality**, **trace sampling**, and **how many tools do the same job**.

---

## 3.18 Demo

The reference implementation is in [`ledgerline/`](../ledgerline/README.md). The local stack now includes Grafana's all-in-one OpenTelemetry image (a collector, Tempo for traces, Prometheus for metrics, Loki for logs, and Grafana).

```bash
cd ledgerline
# optional: add SENTRY_DSN=… to .env for error tracking
docker compose build
docker compose up -d
docker compose run --rm api pytest -q
```

Then:

1. **Correlated logs.** Log in at the app, upload an invoice, and read `docker compose logs api --tail 20`. Find the upload's access line and the "Extraction finished" line, and check they share a `request_id`. Send your own ID with `curl -H "X-Request-ID: my-test-1" http://localhost:8000/livez -i` and see it returned.
2. **Traces.** Open Grafana at `http://localhost:3000` → **Explore** → **Tempo** → **Search**. Open the `POST /invoices` trace, follow the link to its `extract_invoice` trace, and find the LLM span's tokens and cost and the SQL statements (with placeholders, not values).
3. **Metrics.** In **Explore → Prometheus**, query `ledgerline_invoices_extracted_total` and `histogram_quantile(0.95, sum by (le, http_route) (rate(http_server_request_duration_seconds_bucket[5m])))`. Note how OTel names become Prometheus names: dots become underscores, and units and `_total` are added.
4. **Errors.** Visit `/_debug/error`. In Sentry, open the new issue, find its `request_id` and `otel_trace_id` tags, and look both up in logs and Tempo.
5. **Break it on purpose.** Stop the observability container (`docker compose stop otel-lgtm`), use the app, and read the API logs. The app keeps working; the exporter reports it can't send. Telemetry must never take the app down.

**Next in this chapter:** part 2 takes this to staging on AWS: an OpenTelemetry collector alongside the app, a CloudWatch dashboard, alarms for the SLOs above that email you, an external uptime check, and the Sentry DSN in Parameter Store — all in Terraform.

---

## 3.19 Checklist

You're observable at the early-production stage when:

- [ ] Logs are structured (JSON), written to stdout, and every line carries a request ID and trace ID
- [ ] Responses return the request ID, so users can quote it
- [ ] Logs contain no secrets, request bodies or unnecessary personal data, and a test checks the important cases
- [ ] Log retention is set explicitly, and health checks don't flood logs or traces
- [ ] Golden-signal metrics exist for every user-facing endpoint, using route templates
- [ ] Core business metrics exist (for Ledgerline: extractions by outcome, extraction duration)
- [ ] No metric uses unbounded values as labels
- [ ] Requests, database queries and outbound calls are traced; background work is its own trace, linked to its trigger
- [ ] Instrumentation is OpenTelemetry, configured by environment variables, and off when not configured
- [ ] You've checked that telemetry actually arrives, and the start-up logs show no instrumentation warnings
- [ ] Errors go to an error tracker with releases and environments, with personal data sending off
- [ ] For AI features: tokens, cost, latency, failure types and model version are recorded per call; prompts and outputs are not in general telemetry
- [ ] SLOs are written down, and alerts are on symptoms, few, actionable, and each has a runbook
- [ ] You know what observability costs per month, and which tool owns which signal

---

**Next:** [Part 2, Observability on AWS](03b-observability-on-aws.md), then [Chapter 4 — Deployment models and infrastructure](04-deployment-models.md).
