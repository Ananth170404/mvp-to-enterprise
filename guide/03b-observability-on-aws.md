# Chapter 3, part 2: Observability on AWS

> **In this part:** taking [part 1](03-observability.md)'s logs, traces, metrics and errors to the staging environment from [Chapter 2](02b-staging-on-aws.md). It covers how OpenTelemetry reaches AWS in 2026, traces in CloudWatch Transaction Search, metrics from logs, SLO alarms with burn rates, an external uptime check, email alerts, a dashboard, saved log queries and runbooks. It's all in Terraform, and the app code barely changes.

Everything here reflects AWS as of October 2026. CloudWatch's OpenTelemetry support changed a lot during 2026, so check the current documentation for anything marked new.

---

## Contents

- [3B.1 What we're adding](#3b1-what-were-adding)
- [3B.2 How OpenTelemetry gets to AWS](#3b2-how-opentelemetry-gets-to-aws)
- [3B.3 The collector sidecar](#3b3-the-collector-sidecar)
- [3B.4 Traces: Transaction Search](#3b4-traces-transaction-search)
- [3B.5 Metrics: two kinds, two jobs](#3b5-metrics-two-kinds-two-jobs)
- [3B.6 SLO alarms](#3b6-slo-alarms)
- [3B.7 Uptime check](#3b7-uptime-check)
- [3B.8 Errors: Sentry on staging](#3b8-errors-sentry-on-staging)
- [3B.9 Dashboard and saved queries](#3b9-dashboard-and-saved-queries)
- [3B.10 Doing it](#3b10-doing-it)
- [3B.11 Runbooks](#runbooks)
- [3B.12 Configuring and extending](#3b12-configuring-and-extending)
- [3B.13 Pausing without false alarms](#3b13-pausing-without-false-alarms)
- [3B.14 Cost](#3b14-cost)
- [3B.15 Troubleshooting](#3b15-troubleshooting)
- [3B.16 Checklist](#3b16-checklist)

---

## 3B.1 What we're adding

```
                               ┌──────────── ECS task ─────────────┐
  stdout (JSON logs) ─────────────────────────────────────────────▶ CloudWatch Logs ──▶ metric filters ──▶ alarms ──▶ SNS ──▶ email
                               │  app ──OTLP──▶ CloudWatch agent   │                                         ▲
                               └──────────────────┬────────────────┘                                         │
                                                  ├──▶ X-Ray / Transaction Search (traces)                   │
                                                  └──▶ CloudWatch OTel metrics (PromQL)                      │
  load balancer ──▶ free ALB metrics (requests, 5xx, latency, healthy tasks) ────────────────────────────────┤
  Synthetics canary (every 15 min) ──HTTPS──▶ CloudFront ──▶ /readyz ──▶ SuccessPercent ─────────────────────┘
  app ──errors──▶ Sentry (its own alerts)
```

| Piece | Where it lives | Purpose |
|---|---|---|
| CloudWatch agent sidecar | [`app.tf`](../ledgerline/infra/staging/app.tf) | Receives OpenTelemetry from the app and forwards it to CloudWatch |
| Sentry DSN parameter | [`secrets.tf`](../ledgerline/infra/staging/secrets.tf) | Error tracking on staging |
| Everything else | [`observability.tf`](../ledgerline/infra/staging/observability.tf) | Alerts, log metrics, alarms, canary, dashboard, saved queries, the Transaction Search policy |
| Two log lines | [`app/extraction.py`](../ledgerline/app/extraction.py) | One structured line per LLM call and per extraction, for log-based metrics |

---

## 3B.2 How OpenTelemetry gets to AWS

The app speaks OTLP. Something has to deliver that to a backend. On AWS in 2026 there are four common routes:

| Route | How | Trade-offs |
|---|---|---|
| **CloudWatch agent as a sidecar** (chosen) | The agent receives OTLP on `localhost:4318` and forwards it to CloudWatch's OTLP endpoints, signing requests with the task role | AWS's documented path for ECS. No credentials in the app. Adds a container (~256 MB) |
| An OpenTelemetry Collector (contrib build, or AWS's ADOT distribution) as a sidecar | Same idea, with the `sigv4auth` extension and `otlphttp` exporters configured by hand | Full control: filtering, tail sampling, sending to two backends. More configuration to own |
| The SDK straight to CloudWatch's OTLP endpoints | `https://xray.<region>.amazonaws.com/v1/traces` and so on | The endpoints require AWS Signature V4 signing (traces accept nothing else). Python's OTLP exporter doesn't sign, so you'd need extra code. Not worth it |
| A third-party backend (Grafana Cloud, Honeycomb, Datadog) | The SDK sends OTLP with an API key header; no sidecar | Simplest wiring; data leaves AWS; a separate bill and account |

The application code is identical in every case. Only `OTEL_EXPORTER_OTLP_ENDPOINT` (and perhaps a header) changes. That's the vendor-neutral promise from part 1, delivered.

---

## 3B.3 The collector sidecar

A **sidecar** is a second container in the same task. Containers in one Fargate task share a network namespace, so `localhost` in the app reaches the agent.

```hcl
{
  name              = "cloudwatch-agent"
  image             = var.cloudwatch_agent_image      # public.ecr.aws/cloudwatch-agent/cloudwatch-agent
  essential         = false                           # if the agent dies, the app keeps serving
  memoryReservation = 256
  environment = [
    # Receive OTLP on :4317 (gRPC) and :4318 (HTTP); forward traces and metrics to CloudWatch.
    { name = "CW_CONFIG_CONTENT", value = jsonencode({ opentelemetry = { collect = { otlp = {} } } }) },
  ]
}
```

And in the app container:

```hcl
dependsOn = [{ containerName = "cloudwatch-agent", condition = "START" }]
environment = [
  { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = "http://localhost:4318" },
  { name = "OTEL_SERVICE_NAME",           value = "ledgerline" },
  ...
]
```

Decisions worth understanding:

- **`essential = false`.** If an essential container stops, ECS stops the whole task. A telemetry agent crash must never take the app down. If it dies, the app's exporter logs warnings and carries on.
- **`condition = "START"`, not `"HEALTHY"`.** The agent image has no health check, so waiting for it to be healthy would hang every deploy.
- **The task role signs the telemetry.** The app task's role gets AWS's `CloudWatchAgentServerPolicy`. The app itself never holds credentials for telemetry.
- **Memory.** The task grows from 512 MB to 1024 MB to fit both containers (Fargate allows 512, 1024 or 2048 MB at 0.25 vCPU).
- **The image tag.** AWS's own examples use `:latest`. That's convenient, but it means a deploy can silently pick up a new agent version. Once it works, pin the tag or digest you saw: `aws ecs describe-tasks` shows the image digest that ran.
- **The deploy script only swaps our image.** `deploy_ecs.sh` used to replace the first container's image. With a sidecar, it now replaces only containers running the `ledgerline` image, so the agent keeps its own.

The migration task has no sidecar and no OTLP endpoint, so its telemetry export stays off. A 20-second one-off task doesn't need traces.

---

## 3B.4 Traces: Transaction Search

Traces sent to X-Ray land in **Transaction Search**:

- **Every span** is stored as structured log data in the `aws/spans` log group, billed like logs.
- **1% of spans** are indexed as trace summaries for free (for X-Ray's trace list and analytics). Because all spans are stored, any trace can still be opened in full.
- Spans use OpenTelemetry's semantic conventions and W3C trace IDs, so the `trace_id` in our JSON logs is the same ID you search for.

Enabling it is a **one-time account setting**. X-Ray needs permission to write the logs (that's in Terraform), and then the destination is switched with one CLI command:

```bash
aws xray update-trace-segment-destination --destination CloudWatchLogs
aws xray get-trace-segment-destination     # Status: ACTIVE (can take ~10 minutes)
```

Traces sent before it's enabled aren't searchable. Then, in the CloudWatch console, open **Transaction Search** and search by `trace_id`, by `attributes.http.route`, or by span name, such as `extract_invoice`.

---

## 3B.5 Metrics: two kinds, two jobs

As of 2026, CloudWatch has two metric systems:

| | **Classic CloudWatch metrics** | **CloudWatch OpenTelemetry metrics** (GA June 2026) |
|---|---|---|
| Sent by | AWS services (free "vended" metrics), `PutMetricData`, log metric filters | OTLP |
| Queried with | Metric math, dashboards, alarms | PromQL (CloudWatch Query Studio) |
| Priced | Per metric per month (~$0.30, first 10 free) | Per GB ingested, 15 months' storage included |
| Labels | Up to 30 dimensions; each combination is a billed metric | High cardinality is queryable |
| Used here for | **Alarms and the dashboard** | **Exploring** the app's OTel metrics |

Ledgerline alarms on classic metrics because alarms, dashboards and Terraform all fully support them today, and they come from two cheap sources:

**1. Metrics AWS already collects, for free.** The load balancer publishes request counts, 5xx counts (from the app and from the load balancer itself), response time percentiles and healthy target counts. Those cover the golden signals for the web tier.

**2. Metrics extracted from the app's JSON logs.** A **metric filter** matches log lines and turns them into a metric. Part 1 made every log line structured, so this is a one-line pattern:

```hcl
resource "aws_cloudwatch_log_metric_filter" "extraction_duration" {
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"extraction_finished\" }"
  metric_transformation {
    namespace = "Ledgerline/Staging"
    name      = "ExtractionDuration"
    value     = "$.duration_ms"
    unit      = "Milliseconds"
  }
}
```

The app logs one line per extraction (`event: extraction_finished`, with `outcome` and `duration_ms`) and one per LLM call (`event: llm_call`, with `llm_cost_usd`, tokens and `duration_ms`). Five filters give: extractions total, extractions failed, extraction duration, LLM cost, and ERROR log lines.

> **Logs are the most portable metrics source.** Every platform can count and measure structured log lines. When a metrics pipeline is new, changing or broken, the log-based metric still works. The trade-off is cost at high volume, since you pay to ingest the log line anyway, and no backfill: a filter only counts lines logged after it was created.

Note what the filters **don't** use as dimensions: tenant IDs and invoice IDs. In classic CloudWatch, every distinct dimension value is a separately billed metric. Per-tenant breakdowns come from Logs Insights queries instead (section 3B.9).

---

## 3B.6 SLO alarms

Part 1 set Ledgerline's SLOs. Here they become alarms.

### Availability: burn-rate alarms

**SLO:** 99.5% of requests without a 5xx, over 30 days. The error budget is 0.5%.

A burn rate of 1× uses exactly the month's budget. Two alarms watch for budget burning too fast:

| Alarm | Window | Fires when the 5xx ratio exceeds | Meaning |
|---|---|---|---|
| Fast burn | 5 minutes | 14.4 × 0.5% = **7.2%** | At this rate the month's budget is gone in about 2 days. Act now |
| Slow burn | 1 hour | 6 × 0.5% = **3%** | Gone in about 5 days. Act today |

The ratio is computed with **metric math** over three free ALB metrics:

```
IF(requests >= 10, (app5xx + lb5xx) / requests, 0)
```

- **`app5xx + lb5xx`**: errors from the app *and* from the load balancer itself. The load balancer returns 502 and 503 when there's no healthy app task. Those are the outages that matter most, and the app never sees them.
- **`requests >= 10`**: at staging's traffic, one error in three requests is a 33% ratio and would page someone for nothing. Below the minimum, the ratio counts as 0. A production service sets the minimum, and the windows, to suit its traffic.

### The other alarms

| Alarm | Signal | Fires when |
|---|---|---|
| `latency-p95` | ALB `TargetResponseTime` p95 | Above 2 s for 15 minutes |
| `no-healthy-targets` | ALB `HealthyHostCount` minimum | Below 1 for 3 minutes. Missing data counts as breaching |
| `extraction-p95-slow` | `ExtractionDuration` p95 (from logs) | Above 2 minutes over an hour (the extraction SLO) |
| `extraction-failure-rate` | `ExtractionsFailed / ExtractionsTotal` | Above 20% over an hour, with at least 5 extractions |
| `llm-cost-per-hour` | `LlmCostUsd` sum | Above $1 in an hour (`llm_cost_alarm_usd_per_hour`) |
| `error-logs` | `ErrorLogLines` | More than 10 in 15 minutes |
| `uptime` | Canary `SuccessPercent` | Two failed checks in a row (section 3B.7) |

Every alarm sends to one SNS topic, which emails you. Most also send an **OK** notification when they recover, so you know when a problem has resolved itself. Each alarm's description says what it means and links to its [runbook](#runbooks).

`treat_missing_data` is decided per alarm, deliberately. For ratios and latencies, *no data* means *no traffic*, which isn't an outage (`notBreaching`). For healthy targets and the uptime check, *no data* means something stopped reporting, which is suspicious (`breaching`).

---

## 3B.7 Uptime check

Every alarm so far watches the system from the inside. None of them would notice if CloudFront were misconfigured, the certificate expired, or DNS broke, because the app would be healthy and simply unreachable. An **external check** calls the site the way a user does.

**CloudWatch Synthetics** runs a small script (a *canary*) on a schedule. Ledgerline's calls `/readyz` through CloudFront every 15 minutes:

```javascript
exports.handler = async () => {
  const url = `${process.env.TARGET_URL}/readyz`;
  const response = await fetch(url, { signal: AbortSignal.timeout(10_000) });
  if (response.status !== 200) throw new Error(`${url} returned ${response.status}`);
  return "ok";
};
```

- **`/readyz`, not `/livez`:** readiness also checks the database, so a passing check means a user could actually use the site. It's outside the staging password gate, so the canary needs no credentials.
- **No browser:** Synthetics runtimes include a headless browser for page-flow tests. A single HTTP check doesn't need one, and is faster and cheaper.
- **The runtime is looked up, not hard-coded:** `data "aws_synthetics_runtime_version"` with `latest = true`. AWS deprecates runtimes regularly (several were retired in early 2026), and a hard-coded version eventually stops being accepted.
- **Artifacts expire:** each run writes results to S3. The bucket deletes them after 14 days, using a new `expire_after_days` option on the `private_bucket` module.
- **Cost scales with frequency:** about $0.0012 per run. Every 15 minutes is about $3.50 a month; every 5 minutes about $10; every minute about $50. Choose the frequency by how fast you need to know.

Alternatives: a scheduled job elsewhere (a GitHub Actions cron, an independent uptime service such as Better Stack or UptimeRobot, often with free tiers). Checking from *outside* your cloud provider also catches provider-wide problems; checking from inside is simpler to wire into alarms.

---

## 3B.8 Errors: Sentry on staging

Same pattern as the Gemini key: Terraform creates the parameter with a placeholder, and you set the real value from the CLI, so it never appears in code or state:

```bash
aws ssm put-parameter --name /ledgerline/staging/sentry_dsn --type SecureString --overwrite --value "<your DSN>"
```

ECS injects it as `SENTRY_DSN`. Two details:

- **A placeholder must not break start-up.** `setup_sentry()` treats any value that isn't an `https://` URL as "Sentry off" and logs a warning. Without that, the parameter's initial `not-set` value would crash the app at start-up.
- **Environment and release are automatic.** `environment` comes from `APP_ENV` (`staging`), and `release` from the git SHA baked into the image. In Sentry you can filter by environment and see which deploy introduced an issue.

Sentry has its own alerting: by default, it emails you when a new issue appears. That overlaps a little with the `error-logs` alarm, deliberately. Sentry says *which* error is new; the alarm says *how many* errors are happening, whatever their type.

---

## 3B.9 Dashboard and saved queries

**One dashboard**, `ledgerline-staging`, with the SLOs written at the top. Its rows:

1. Requests and 5xx; response time p50/p95/p99 with the alarm line; uptime check and healthy tasks.
2. Extractions per hour (finished and failed); extraction time p50/p95 with the SLO line; LLM spend per hour.
3. Recent ERROR log lines, as a live Logs Insights table.

`terraform output dashboard_url` prints its link.

**Saved Logs Insights queries** (CloudWatch → Logs Insights → Saved queries → `Ledgerline/…`) answer the questions dashboards can't, especially per tenant:

| Query | Answers |
|---|---|
| `Errors` | The latest 50 ERROR lines, with request and trace IDs |
| `Everything for one request` | Every line for a request ID (paste it from a bug report or a Sentry tag) |
| `Slowest routes` | Requests, average and p95 duration per route |
| `Extractions by tenant and outcome` | Is one customer's extraction failing or slow? |
| `LLM spend by model and tenant` | Who is using the model, and what it costs |

Logs Insights charges per GB scanned. At staging volumes that's fractions of a cent per query.

---

## 3B.10 Doing it

Commands are PowerShell; in bash, replace the `@aws` splat with `--profile ledgerline --region ap-southeast-2`. If staging is paused, resume it first (section 3B.13), and if AWS answers *"session has expired"* or Terraform says *"No valid credential sources found"*, run `aws login --profile ledgerline` again: sessions last about 12 hours.

```powershell
$aws = @('--profile','ledgerline','--region','ap-southeast-2')   # reused below
```

**1. Your alert email.** It goes in a git-ignored file, not in code:

```powershell
cd ledgerline\infra\staging
copy terraform.tfvars.example terraform.tfvars    # then EDIT it: alert_email = "you@example.com"
```

`copy` only creates the file; Terraform will happily subscribe `you@example.com` if you forget to edit it.

**2. Plan and apply.** The new `archive` provider (it zips the canary script) needs `init -upgrade` first:

```powershell
terraform init "-backend-config=backend.hcl" -upgrade
terraform plan
```

Ledgerline's plan read **`Plan: 36 to add, 1 to change, 1 to destroy`**. Before applying, check what the destroy and change are:

```powershell
terraform plan -no-color | Select-String "must be replaced|will be destroyed|will be updated"
```

- `aws_ecs_task_definition.app` **must be replaced**: task definitions are immutable, so any change registers a new revision and retires the old one. That's the "1 destroy" (and one of the 36 adds). Safe: the running service keeps its revision until the next deploy.
- `aws_iam_role_policy.task_execution_secrets` **updated in place**: it can now read the Sentry parameter.

Anything else being destroyed or replaced, especially the database or a bucket, is a reason to stop and understand why. Then `terraform apply`.

**3. Confirm the email subscription.** AWS sends *"AWS Notification - Subscription Confirmation"* (check spam). Click the link. Until you do, alarms reach nobody:

```powershell
$topic = terraform output -raw alerts_topic_arn
aws sns list-subscriptions-by-topic --topic-arn $topic --query "Subscriptions[].SubscriptionArn" @aws
# a long ARN = confirmed; "PendingConfirmation" = not yet
```

**4. Turn on Transaction Search** (once per account and region):

```powershell
aws xray update-trace-segment-destination --destination CloudWatchLogs @aws    # Status: PENDING
aws xray get-trace-segment-destination @aws                                   # ~10 minutes later: ACTIVE
```

While it's `PENDING`, running the update again fails with *"Updates are not allowed while the current status is PENDING"*. That only means the first one is still being applied.

**5. Set the Sentry DSN:**

```powershell
aws ssm put-parameter --name /ledgerline/staging/sentry_dsn --type SecureString --overwrite --value "https://…your DSN…" @aws
```

Each `put-parameter --overwrite` creates a new **version**; containers read the latest version when they start. Paste the real value: setting the literal text `<your DSN>` is valid as far as AWS is concerned, and the app will just log *"SENTRY_DSN is not a URL"* and run without Sentry.

**6. Deploy.** Task definition changes and new parameter values take effect on the next deploy (Chapter 2's split: Terraform defines *how*, CI decides *which image*). Push the code changes, or `git commit --allow-empty -m "Redeploy"; git push`, then `gh run watch`.

**7. Check each signal.** First make some traffic: open the staging URL (`terraform output -raw staging_url`), enter the site password, log in, **upload an invoice**, and open `/_debug/error` once (it shows "Internal Server Error", as it should).

```powershell
# Logs: JSON from the app; the agent's own output should show no repeated errors
aws logs tail /ecs/ledgerline-staging --since 15m --log-stream-name-prefix app @aws
aws logs tail /ecs/ledgerline-staging --since 15m --log-stream-name-prefix cwagent @aws

# Traces: spans arrive in the aws/spans log group
aws logs filter-log-events --log-group-name aws/spans --filter-pattern '"extract_invoice"' --max-items 2 --query "events[].message" @aws

# Metrics from logs (appear a few minutes after matching lines)
aws cloudwatch list-metrics --namespace Ledgerline/Staging --query "Metrics[].MetricName" @aws

# Alarms: OK, or INSUFFICIENT_DATA for the first hour
aws cloudwatch describe-alarms --alarm-name-prefix ledgerline-staging --query "MetricAlarms[].[AlarmName,StateValue]" --output table @aws

# Uptime canary: PASSED every 15 minutes; StateReason explains a failure
aws synthetics get-canary-runs --name ll-staging-uptime --max-results 3 --query "CanaryRuns[].[Status.State,Status.StateReason,Timeline.Started]" --output table @aws

# Dashboard
start (terraform output -raw dashboard_url)
```

In the console:

| Signal | Where |
|---|---|
| Traces | CloudWatch → **Transaction Search**: search `extract_invoice`, open a trace, find the LLM span and its link back to the upload |
| OTel metrics | CloudWatch → **Query Studio** (PromQL): search for `ledgerline` |
| Errors | Sentry → Issues: the `RuntimeError` with `environment: staging`, `release` = the deployed git SHA |

**8. Fire drill (optional, ~30 minutes).** An alert you've never seen fire is an alert you don't know works:

```powershell
aws ecs update-service --cluster ledgerline-staging --service ledgerline-staging-app --desired-count 0 @aws
# ~5 min: "ALARM: ledgerline-staging-no-healthy-targets" email; ~30 min: "...-uptime"
aws ecs update-service --cluster ledgerline-staging --service ledgerline-staging-app --desired-count 1 @aws
# a few minutes later: "OK" emails
```

> **Worked example: the canary's first run failed, and that was good news.** Ledgerline's first canary run failed at 21:46 with *"returned 503: 503 Service Temporarily Unavailable"*, an HTML page from the **load balancer**: staging had just been resumed and no app task was healthy yet. `StateReason` showed it immediately; `curl.exe https://…/readyz` returned 200 a few minutes later, and the following runs (including a manual one, `aws synthetics start-canary-dry-run --name ll-staging-uptime`) passed. The first failure proved the whole chain (script, runtime, permissions, metric) works; a check that has only ever passed hasn't proved that. It also showed why resuming staging should start the canary *after* the deploy (section 3B.13).

---

<a id="runbooks"></a>

## 3B.11 Runbooks

Short on purpose. Each alarm's description links here.

**`availability-fast-burn` / `availability-slow-burn`: too many 5xx responses**

1. Dashboard: are the 5xx from the **app** or the **load balancer**? Load-balancer 5xx (502/503) mean no healthy task; go to `no-healthy-targets`.
2. App 5xx: Sentry for new issues; Logs Insights `Errors` for request IDs.
3. Did it start with a deploy? Compare with the time of the last CI deploy. If so, roll back (Chapter 2, part 2: [Roll back](02b-staging-on-aws.md#roll-back)).

**`no-healthy-targets`: no task is passing `/readyz`**

1. `aws ecs describe-services --cluster ledgerline-staging --services ledgerline-staging-app`: running count, events, deployment state.
2. Is the database up? (`aws rds describe-db-instances`). A stopped database is the usual cause on staging.
3. App logs from the latest task: start-up errors, configuration refusals.

**`latency-p95`: slow responses**

1. Logs Insights `Slowest routes`: one route or all?
2. Transaction Search: open a slow trace for that route and look for the long span (a query, the S3 call).
3. All routes slow: check the database's CPU and connections in its RDS metrics.

**`extraction-p95-slow` / `extraction-failure-rate`**

1. Logs: `event = "llm_call"` lines with `llm_error_type`: is the provider rate-limiting (retryable) or rejecting (permanent)?
2. Did the model, prompt version or provider change? `extraction_runs` records them per attempt.
3. The provider's status page.

**`llm-cost-per-hour`**

1. Logs Insights `LLM spend by model and tenant`: one tenant? One model?
2. Many calls per invoice suggests a retry loop; many invoices from one tenant suggests heavy use or abuse.
3. If the key may have leaked: rotate it (Chapter 2, part 2: [Rotate secrets](02b-staging-on-aws.md#rotate-secrets)).

**`error-logs`**: open Sentry, find the issue, then follow its request ID into the logs.

**`uptime`: users can't reach the site**

1. Open the site yourself. Then check the canary's last run (Synthetics → run details → logs) for the status code or error.
2. If the app's own alarms are quiet, look between the user and the app: CloudFront (distribution status), the certificate, DNS.

---

## 3B.12 Configuring and extending

### Terraform variables

Set them in `terraform.tfvars` (git-ignored; start from `terraform.tfvars.example`) or with `-var name=value`:

| Variable | Default | What it controls |
|---|---|---|
| `alert_email` | *(required)* | Where alarm notifications go |
| `alarms_enabled` | `true` | `false` silences all alarm notifications (alarms still change state) |
| `llm_cost_alarm_usd_per_hour` | `1` | Threshold for the LLM spend alarm |
| `cloudwatch_agent_image` | `…/cloudwatch-agent:latest` | The sidecar image; pin a version or digest once it works |
| `llm_provider` | `fake` | `gemini` once the API key is in Parameter Store |

Thresholds that aren't variables (SLO targets, burn rates, minimum request counts, the canary schedule) are plain values in [`observability.tf`](../ledgerline/infra/staging/observability.tf), each with a comment explaining it. Changing them is a reviewed code change, which is what you want for the definition of "broken".

> **Gitignore comments go on their own line.** `terraform.tfvars   # personal values` in `.gitignore` is *not* a pattern plus a comment: Git treats the whole line, spaces and `#` included, as the pattern, and ignores nothing. Check any ignore rule with `git check-ignore -v <path>`.

### Recipes

**Another email recipient.** Add a subscription; each recipient confirms their own:

```hcl
resource "aws_sns_topic_subscription" "oncall_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = "oncall@example.com"
}
```

**Slack or Microsoft Teams.** AWS's chat integration (*Amazon Q Developer in chat applications*, formerly AWS Chatbot) subscribes a channel to the same SNS topic. Authorise the workspace once in its console, then manage the channel in Terraform (`aws_chatbot_slack_channel_configuration` with `sns_topic_arns = [aws_sns_topic.alerts.arn]`). Alternatives: a paging tool's email or webhook integration (PagerDuty, incident.io), subscribed the same way.

**A new metric from logs, with an alarm.** Log a structured line with an `event` field, then match it:

```hcl
resource "aws_cloudwatch_log_metric_filter" "approvals" {
  name           = "invoices-approved"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"invoice_approved\" }"
  metric_transformation {
    namespace     = local.metric_namespace
    name          = "InvoicesApproved"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "no_approvals" {
  alarm_name          = "${local.name}-no-approvals-today"
  alarm_description   = "No invoices approved in 24 hours on a working day. Is the review screen broken?"
  namespace           = local.metric_namespace
  metric_name         = "InvoicesApproved"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  actions_enabled     = var.alarms_enabled
  alarm_actions       = local.alarm_actions
}
```

Absence alarms like this ("nothing happened when something should have") catch failures that produce no errors at all.

**Test a pattern before deploying it:**

```powershell
aws logs test-metric-filter --filter-pattern '{ $.event = "extraction_finished" }' --log-event-messages '{"event":"extraction_finished","outcome":"needs_review","duration_ms":412}' @aws
```

**Canary frequency.** Change `rate(15 minutes)` in `aws_synthetics_canary.uptime`. Cost is per run (~$0.0012): every 5 minutes is about $10 a month.

**More of the trace index.** 1% of spans are indexed for free; all spans are stored regardless. To index more:

```powershell
aws xray update-indexing-rule --name Default --rule '{\"Probabilistic\": {\"DesiredSamplingPercentage\": 10}}' @aws
```

**A saved query.** Copy an `aws_cloudwatch_query_definition` block; the name's `/` makes a folder in the console.

**Pin the agent image.** After a deploy that works, find the digest that ran and set `cloudwatch_agent_image` to `public.ecr.aws/cloudwatch-agent/cloudwatch-agent@sha256:…`:

```powershell
$task = aws ecs list-tasks --cluster ledgerline-staging --query "taskArns[0]" --output text @aws
aws ecs describe-tasks --cluster ledgerline-staging --tasks $task --query "tasks[0].containers[?name=='cloudwatch-agent'].imageDigest" --output text @aws
```

**Silence everything for a while.** `terraform apply -var alarms_enabled=false`, or the CLI commands in 3B.13.

---

## 3B.13 Pausing without false alarms

Pausing staging (Chapter 2) now has two more steps. Otherwise the uptime and healthy-target alarms correctly report that the site is down, every time.

```powershell
# Pause
$alarms = (aws cloudwatch describe-alarms --alarm-name-prefix ledgerline-staging --query "MetricAlarms[].AlarmName" --output text --profile ledgerline --region ap-southeast-2) -split "\s+"
aws cloudwatch disable-alarm-actions --alarm-names $alarms --profile ledgerline --region ap-southeast-2
aws synthetics stop-canary --name ll-staging-uptime --profile ledgerline --region ap-southeast-2
aws ecs update-service --cluster ledgerline-staging --service ledgerline-staging-app --desired-count 0 --profile ledgerline --region ap-southeast-2
aws rds stop-db-instance --db-instance-identifier ledgerline-staging --profile ledgerline --region ap-southeast-2

# Resume: database first, then deploy, then the checks
aws rds start-db-instance --db-instance-identifier ledgerline-staging --profile ledgerline --region ap-southeast-2
aws rds wait db-instance-available --db-instance-identifier ledgerline-staging --profile ledgerline --region ap-southeast-2
git commit --allow-empty -m "Resume staging"; git push
# after the deploy finishes:
aws synthetics start-canary --name ll-staging-uptime --profile ledgerline --region ap-southeast-2
aws cloudwatch enable-alarm-actions --alarm-names $alarms --profile ledgerline --region ap-southeast-2
```

This is a small version of a real practice: **maintenance windows**. Planned downtime shouldn't page anyone, and alerting must be switched back on afterwards. Forgetting the second half is a classic way to miss the next real outage.

Logs Insights queries, dashboards and the SNS topic cost nothing while paused. The canary costs nothing while stopped.

---

## 3B.14 Cost

Approximate monthly additions for staging (Sydney, October 2026; check the CloudWatch pricing page):

| Item | Approx. per month |
|---|---|
| Extra 512 MB of Fargate memory for the sidecar | ~$2 |
| Synthetics canary every 15 minutes (~2,900 runs) | ~$3.50 |
| Alarms: about 14 alarm metrics (metric-math alarms count each metric); first 10 free | ~$0.40 |
| Log-based custom metrics: 5, within the free 10 | $0 |
| Logs, spans and OTel metrics at staging volumes (a few hundred MB) | <$1 |
| Dashboard (first 3 free), SNS email, saved queries | $0 |
| **Total** | **~$6–8** |

The numbers that would grow in production: **log volume** (every request writes an access line), **span volume** (sample at the SDK or collector), and **canary frequency** times the number of checks.

---

## 3B.15 Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No alarm emails | Subscription not confirmed, or in the spam folder | Click the link in "Subscription Confirmation"; check SNS → Subscriptions shows *Confirmed* |
| Alarms stuck in *Insufficient data* | No traffic yet, or the metric doesn't exist yet | Use the site; log-based metrics appear after the first matching line |
| No traces in Transaction Search | Not enabled, enabled under 10 minutes ago, or the agent isn't receiving | `aws xray get-trace-segment-destination`; read the `cwagent/…` log stream; check the app's logs for OTLP export errors |
| App logs show "Failed to export" | Agent not started or crashed | Its log stream; the task's stopped-container reason in ECS |
| Metric filters show nothing | Filters don't backfill; the field names don't match | Make new requests; compare the pattern with a real log line |
| Canary fails but the site works | Runtime or packaging issue, or staging paused | Synthetics → canary → run logs. A paused staging fails by design |
| Sentry shows nothing from staging | DSN still `not-set` (the app logs a warning), or not redeployed since setting it | Set the parameter, then deploy |
| `terraform plan` fails on the runtime data source or archive | Providers not upgraded | `terraform init -upgrade` |
| *"session has expired"* / *"No valid credential sources found"* | The `aws login` session (about 12 hours) ended | `aws login --profile ledgerline`, then rerun |
| *"Updates are not allowed while the current status is PENDING"* | Transaction Search is still being enabled | Wait ~10 minutes; `aws xray get-trace-segment-destination` |
| The canary's first run failed with an HTML 503 | No healthy task at that moment (staging starting or paused); the 503 page is the load balancer's | `curl.exe …/readyz`; `aws synthetics start-canary-dry-run --name ll-staging-uptime`; start the canary after deploys when resuming |
| An alarm email you didn't expect after resuming | Alarms correctly saw the downtime | Follow 3B.13's order: alarms back on last |

---

## 3B.16 Checklist

Staging is observable when:

- [ ] Telemetry reaches the backend through a collector the app doesn't depend on (`essential = false`), with no credentials in the app
- [ ] Traces are searchable by the `trace_id` that appears in logs and Sentry
- [ ] Each SLO has an alarm: availability as fast and slow burn rates, latency, and the core job (extraction time)
- [ ] Load-balancer-generated errors count toward availability, not just the app's
- [ ] Ratio alarms ignore windows with too little traffic to mean anything
- [ ] An external check exercises the full path users take, and it alarms
- [ ] Cost and AI-specific alarms exist (LLM spend per hour, extraction failure rate)
- [ ] Every alarm notifies a confirmed destination, has a description, and links to a runbook
- [ ] You've watched at least one alarm fire and recover (a fire drill)
- [ ] Pausing staging silences alerts, and resuming turns them back on
- [ ] Per-tenant questions are answered by log queries, not metric dimensions
- [ ] The monthly cost of observability is known

---

**Next:** [Chapter 4, Deployment models and infrastructure](04-deployment-models.md): containers on ECS were one choice of many. When do you pick a PaaS, a VM, Kubernetes or serverless, and what does each cost to run and to operate?
