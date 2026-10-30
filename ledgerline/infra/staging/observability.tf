# Chapter 3: observability for staging.
#
#   alerts      SNS topic -> your email. Every alarm below notifies it.
#   metrics     classic CloudWatch metrics for alarms: free ALB metrics, plus
#               metrics extracted from the app's JSON logs (metric filters)
#   alarms      the SLOs (availability, latency, extraction time) as burn-rate
#               and threshold alarms, plus cost and error alarms
#   uptime      a Synthetics canary calling /readyz through CloudFront
#   dashboard   one page with the SLOs at the top
#   queries     saved CloudWatch Logs Insights queries
#   traces      X-Ray Transaction Search (the agent sidecar is in app.tf)
#
# Why alarms use classic metrics when the app also sends OpenTelemetry metrics:
# as of October 2026 CloudWatch's OTel metric store (PromQL) is new; alarms,
# dashboards and Terraform all fully support classic metrics. OTel metrics and
# traces are for exploring; these are for waking you up.

variable "alert_email" {
  description = "Where alarm notifications go. Put it in terraform.tfvars (git-ignored), not in code."
  type        = string
}

variable "alarms_enabled" {
  description = "false silences notifications (e.g. while staging is paused). Alarms still change state."
  type        = bool
  default     = true
}

variable "llm_cost_alarm_usd_per_hour" {
  description = "Alert when estimated LLM spend in one hour exceeds this."
  type        = number
  default     = 1
}

locals {
  metric_namespace = "Ledgerline/${title(var.environment)}"
  alarm_actions    = [aws_sns_topic.alerts.arn]
  alb_dimensions   = { LoadBalancer = aws_lb.app.arn_suffix }
  tg_dimensions    = { LoadBalancer = aws_lb.app.arn_suffix, TargetGroup = aws_lb_target_group.app.arn_suffix }
}

# ---------------------------------------------------------------- alerts

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
  # Not encrypted with a KMS key on purpose: alarm messages contain no data,
  # and CloudWatch can only publish to an encrypted topic with a custom key policy.
}

# AWS emails a confirmation link; nothing arrives until you click it.
resource "aws_sns_topic_subscription" "alert_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------------------------------------------------------------- metrics from logs

# Metric filters turn matching JSON log lines into CloudWatch metrics. The
# app logs one line per extraction and per LLM call (app/extraction.py) with
# exactly the fields these patterns read. Filters only see lines logged AFTER
# they're created; they don't backfill.

resource "aws_cloudwatch_log_metric_filter" "extractions_total" {
  name           = "extractions-total"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"extraction_finished\" }"
  metric_transformation {
    namespace     = local.metric_namespace
    name          = "ExtractionsTotal"
    value         = "1"
    default_value = "0" # report 0, not "no data", in quiet periods
  }
}

resource "aws_cloudwatch_log_metric_filter" "extractions_failed" {
  name           = "extractions-failed"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"extraction_finished\" && $.outcome = \"extraction_failed\" }"
  metric_transformation {
    namespace     = local.metric_namespace
    name          = "ExtractionsFailed"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "extraction_duration" {
  name           = "extraction-duration"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"extraction_finished\" }"
  metric_transformation {
    namespace = local.metric_namespace
    name      = "ExtractionDuration"
    value     = "$.duration_ms"
    unit      = "Milliseconds"
  }
}

resource "aws_cloudwatch_log_metric_filter" "llm_cost" {
  name           = "llm-cost"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"llm_call\" && $.llm_cost_usd >= 0 }"
  metric_transformation {
    namespace     = local.metric_namespace
    name          = "LlmCostUsd"
    value         = "$.llm_cost_usd"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "app_errors" {
  name           = "app-errors"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.level = \"ERROR\" }"
  metric_transformation {
    namespace     = local.metric_namespace
    name          = "ErrorLogLines"
    value         = "1"
    default_value = "0"
  }
}

# ---------------------------------------------------------------- SLO alarms

# SLO: 99.5% of requests succeed (no 5xx) over 30 days -> 0.5% error budget.
# Burn rate = error ratio / 0.5%. Two windows (Google SRE workbook, simplified):
#   fast: 14.4x for 5 minutes -> at this rate the month's budget is gone in ~2 days
#   slow: 6x for 1 hour       -> gone in ~5 days
# Below 10 requests in the window the ratio is meaningless (1 error in 3
# requests = 33%), so it's treated as 0. Staging traffic is light; production
# would use longer windows and a minimum that matches its traffic.
locals {
  slo_error_budget = 0.005
  burn_alarms = {
    fast = { period = 300, burn_rate = 14.4, description = "Fast burn: at this error rate the 30-day error budget is gone in ~2 days." }
    slow = { period = 3600, burn_rate = 6, description = "Slow burn: at this error rate the 30-day error budget is gone in ~5 days." }
  }
}

resource "aws_cloudwatch_metric_alarm" "availability_burn" {
  for_each = local.burn_alarms

  alarm_name          = "${local.name}-availability-${each.key}-burn"
  alarm_description   = "${each.value.description} SLO: 99.5% of requests without a 5xx. Runbook: guide/03b-observability-on-aws.md#runbooks"
  comparison_operator = "GreaterThanThreshold"
  threshold           = each.value.burn_rate * local.slo_error_budget
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  actions_enabled     = var.alarms_enabled
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  metric_query {
    id          = "ratio"
    expression  = "IF(requests >= 10, (app5xx + lb5xx) / requests, 0)"
    label       = "5xx ratio"
    return_data = true
  }
  metric_query {
    id = "requests"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "RequestCount"
      dimensions  = local.alb_dimensions
      period      = each.value.period
      stat        = "Sum"
    }
  }
  metric_query {
    id = "app5xx" # errors returned by the app
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_Target_5XX_Count"
      dimensions  = local.alb_dimensions
      period      = each.value.period
      stat        = "Sum"
    }
  }
  metric_query {
    id = "lb5xx" # errors from the load balancer itself: no healthy targets, timeouts
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_ELB_5XX_Count"
      dimensions  = local.alb_dimensions
      period      = each.value.period
      stat        = "Sum"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "latency_p95" {
  alarm_name          = "${local.name}-latency-p95"
  alarm_description   = "95th percentile response time above 2 s for 15 minutes."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  dimensions          = local.alb_dimensions
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 2
  treat_missing_data  = "notBreaching"
  actions_enabled     = var.alarms_enabled
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "no_healthy_targets" {
  alarm_name          = "${local.name}-no-healthy-targets"
  alarm_description   = "No app task is passing /readyz: the site is down or can't reach the database."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  dimensions          = local.tg_dimensions
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  actions_enabled     = var.alarms_enabled
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# SLO: 95% of invoices reach a result within 2 minutes of upload.
resource "aws_cloudwatch_metric_alarm" "extraction_slow" {
  alarm_name          = "${local.name}-extraction-p95-slow"
  alarm_description   = "95th percentile extraction time above 2 minutes over the last hour (SLO: 95% within 2 minutes)."
  namespace           = local.metric_namespace
  metric_name         = "ExtractionDuration"
  extended_statistic  = "p95"
  period              = 3600
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 120000
  treat_missing_data  = "notBreaching"
  actions_enabled     = var.alarms_enabled
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "extraction_failures" {
  alarm_name          = "${local.name}-extraction-failure-rate"
  alarm_description   = "More than 20% of extractions failed in the last hour (at least 5 extractions). Check the provider, the model and recent prompt changes."
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0.2
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  actions_enabled     = var.alarms_enabled
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  metric_query {
    id          = "rate"
    expression  = "IF(total >= 5, failed / total, 0)"
    label       = "Extraction failure rate"
    return_data = true
  }
  metric_query {
    id = "total"
    metric {
      namespace   = local.metric_namespace
      metric_name = "ExtractionsTotal"
      period      = 3600
      stat        = "Sum"
    }
  }
  metric_query {
    id = "failed"
    metric {
      namespace   = local.metric_namespace
      metric_name = "ExtractionsFailed"
      period      = 3600
      stat        = "Sum"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "llm_cost" {
  alarm_name          = "${local.name}-llm-cost-per-hour"
  alarm_description   = "Estimated LLM spend in the last hour is above USD ${var.llm_cost_alarm_usd_per_hour}. Possible loop, abuse or leaked key."
  namespace           = local.metric_namespace
  metric_name         = "LlmCostUsd"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.llm_cost_alarm_usd_per_hour
  treat_missing_data  = "notBreaching"
  actions_enabled     = var.alarms_enabled
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "error_logs" {
  alarm_name          = "${local.name}-error-logs"
  alarm_description   = "More than 10 ERROR log lines in 15 minutes. Details: Sentry, or the 'Ledgerline/Errors' saved query."
  namespace           = local.metric_namespace
  metric_name         = "ErrorLogLines"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 10
  treat_missing_data  = "notBreaching"
  actions_enabled     = var.alarms_enabled
  alarm_actions       = local.alarm_actions
}

# ---------------------------------------------------------------- uptime canary

# Artifacts (logs, results) of each canary run; kept 14 days.
module "canary_artifacts" {
  source                  = "../modules/private_bucket"
  name                    = "${local.name}-canary-${local.account_id}"
  noncurrent_version_days = 1
  expire_after_days       = 14
  force_destroy           = true # only test artifacts; deletable with the environment
}

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "canary" {
  name               = "${local.name}-canary"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "canary" {
  statement {
    sid       = "WriteArtifacts"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${module.canary_artifacts.arn}/*"]
  }
  statement {
    sid       = "FindArtifactBucket"
    actions   = ["s3:GetBucketLocation"]
    resources = [module.canary_artifacts.arn]
  }
  statement {
    sid       = "ListBuckets" # required by the Synthetics runtime
    actions   = ["s3:ListAllMyBuckets"]
    resources = ["*"]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/cwsyn-*"]
  }
  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["CloudWatchSynthetics"]
    }
  }
}

resource "aws_iam_role_policy" "canary" {
  name   = "canary"
  role   = aws_iam_role.canary.id
  policy = data.aws_iam_policy_document.canary.json
}

data "archive_file" "canary" {
  type        = "zip"
  source_file = "${path.module}/canary/uptime.js"
  output_path = "${path.module}/.terraform/canary-uptime.zip"
}

# The newest Node.js runtime, looked up at plan time. Runtimes are deprecated
# regularly; a hard-coded version eventually stops being accepted.
data "aws_synthetics_runtime_version" "node" {
  prefix = "syn-nodejs-puppeteer"
  latest = true
}

resource "aws_synthetics_canary" "uptime" {
  name                 = "ll-${var.environment}-uptime" # canary names are short (21 characters, lowercase)
  artifact_s3_location = "s3://${module.canary_artifacts.name}/"
  execution_role_arn   = aws_iam_role.canary.arn
  runtime_version      = data.aws_synthetics_runtime_version.node.version_name
  handler              = "uptime.handler" # runtimes 11.0+ accept the script at the zip's root
  zip_file             = data.archive_file.canary.output_path
  start_canary         = true
  delete_lambda        = true

  schedule {
    expression = "rate(15 minutes)" # ~2,900 runs/month, ~$3.50; every 5 minutes would be ~$10
  }

  run_config {
    timeout_in_seconds = 30
    environment_variables = {
      TARGET_URL = "https://${aws_cloudfront_distribution.app.domain_name}"
    }
  }

  success_retention_period = 7
  failure_retention_period = 14

  depends_on = [aws_iam_role_policy.canary]
}

resource "aws_cloudwatch_metric_alarm" "uptime" {
  alarm_name          = "${local.name}-uptime"
  alarm_description   = "The external check of /readyz through CloudFront failed twice in a row. Users can't reach the site."
  namespace           = "CloudWatchSynthetics"
  metric_name         = "SuccessPercent"
  dimensions          = { CanaryName = aws_synthetics_canary.uptime.name }
  statistic           = "Average"
  period              = 900
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "LessThanThreshold"
  threshold           = 100
  treat_missing_data  = "breaching" # a canary that stopped running is a problem too
  actions_enabled     = var.alarms_enabled
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# ---------------------------------------------------------------- traces

# Transaction Search stores every span sent to X-Ray as structured logs in the
# aws/spans log group, and indexes 1% for free as trace summaries. X-Ray needs
# permission to write those logs. Enabling the destination itself is a
# one-time account setting, done with the CLI (guide 3B, step 4):
#   aws xray update-trace-segment-destination --destination CloudWatchLogs
resource "aws_cloudwatch_log_resource_policy" "xray_spans" {
  policy_name = "transaction-search-xray"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "TransactionSearchXRayAccess"
      Effect    = "Allow"
      Principal = { Service = "xray.amazonaws.com" }
      Action    = "logs:PutLogEvents"
      Resource = [
        "arn:aws:logs:${var.region}:${local.account_id}:log-group:aws/spans:*",
        "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/application-signals/data:*",
      ]
      Condition = {
        ArnLike      = { "aws:SourceArn" = "arn:aws:xray:${var.region}:${local.account_id}:*" }
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

# ---------------------------------------------------------------- saved queries

resource "aws_cloudwatch_query_definition" "errors" {
  name            = "Ledgerline/Errors"
  log_group_names = [aws_cloudwatch_log_group.app.name]
  query_string    = <<-EOT
    fields @timestamp, logger, message, request_id, tenant_id, trace_id
    | filter level = "ERROR"
    | sort @timestamp desc
    | limit 50
  EOT
}

resource "aws_cloudwatch_query_definition" "request" {
  name            = "Ledgerline/Everything for one request (edit the ID)"
  log_group_names = [aws_cloudwatch_log_group.app.name]
  query_string    = <<-EOT
    fields @timestamp, level, logger, message, http_status, duration_ms, outcome
    | filter request_id = "PASTE-REQUEST-ID-HERE"
    | sort @timestamp asc
  EOT
}

resource "aws_cloudwatch_query_definition" "slow_routes" {
  name            = "Ledgerline/Slowest routes"
  log_group_names = [aws_cloudwatch_log_group.app.name]
  query_string    = <<-EOT
    filter logger = "ledgerline.access"
    | stats count(*) as requests, avg(duration_ms) as avg_ms, pct(duration_ms, 95) as p95_ms by http_route
    | sort p95_ms desc
  EOT
}

resource "aws_cloudwatch_query_definition" "extractions_by_tenant" {
  name            = "Ledgerline/Extractions by tenant and outcome"
  log_group_names = [aws_cloudwatch_log_group.app.name]
  query_string    = <<-EOT
    filter event = "extraction_finished"
    | stats count(*) as extractions, pct(duration_ms, 95) as p95_ms by tenant_id, outcome
    | sort extractions desc
  EOT
}

resource "aws_cloudwatch_query_definition" "llm_spend" {
  name            = "Ledgerline/LLM spend by model and tenant"
  log_group_names = [aws_cloudwatch_log_group.app.name]
  query_string    = <<-EOT
    filter event = "llm_call"
    | stats count(*) as calls, sum(llm_cost_usd) as usd, sum(input_tokens) as input_tokens,
            sum(output_tokens) as output_tokens by llm_model, tenant_id
    | sort usd desc
  EOT
}

# ---------------------------------------------------------------- dashboard

locals {
  alb_metric = { namespace = "AWS/ApplicationELB", dims = ["LoadBalancer", aws_lb.app.arn_suffix] }
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = local.name
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "text", x = 0, y = 0, width = 24, height = 2
        properties = {
          markdown = "## Ledgerline ${var.environment}\n**SLOs:** 99.5% of requests without a 5xx (30 days) · 95% of extractions finish within 2 minutes · the site answers an external check. Alarms email the alerts topic. Traces: CloudWatch console → Transaction Search. Errors: Sentry. Runbooks: guide/03b-observability-on-aws.md"
        }
      },
      {
        type = "metric", x = 0, y = 2, width = 8, height = 6
        properties = {
          title = "Requests and 5xx (per 5 min)", region = var.region, stat = "Sum", period = 300
          metrics = [
            [local.alb_metric.namespace, "RequestCount", local.alb_metric.dims[0], local.alb_metric.dims[1], { label = "Requests" }],
            [".", "HTTPCode_Target_5XX_Count", ".", ".", { label = "5xx from app" }],
            [".", "HTTPCode_ELB_5XX_Count", ".", ".", { label = "5xx from load balancer" }],
          ]
        }
      },
      {
        type = "metric", x = 8, y = 2, width = 8, height = 6
        properties = {
          title = "Response time (s)", region = var.region, period = 300
          metrics = [
            [local.alb_metric.namespace, "TargetResponseTime", local.alb_metric.dims[0], local.alb_metric.dims[1], { stat = "p50", label = "p50" }],
            ["...", { stat = "p95", label = "p95" }],
            ["...", { stat = "p99", label = "p99" }],
          ]
          annotations = { horizontal = [{ value = 2, label = "Alarm (p95)" }] }
        }
      },
      {
        type = "metric", x = 16, y = 2, width = 8, height = 6
        properties = {
          title = "Uptime check and healthy tasks", region = var.region, period = 900
          metrics = [
            ["CloudWatchSynthetics", "SuccessPercent", "CanaryName", aws_synthetics_canary.uptime.name, { stat = "Average", label = "Uptime check %" }],
            ["AWS/ApplicationELB", "HealthyHostCount", "TargetGroup", aws_lb_target_group.app.arn_suffix, "LoadBalancer", aws_lb.app.arn_suffix, { stat = "Minimum", label = "Healthy tasks", yAxis = "right" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 8, width = 8, height = 6
        properties = {
          title = "Extractions per hour", region = var.region, stat = "Sum", period = 3600
          metrics = [
            [local.metric_namespace, "ExtractionsTotal", { label = "Finished" }],
            [".", "ExtractionsFailed", { label = "Failed" }],
          ]
        }
      },
      {
        type = "metric", x = 8, y = 8, width = 8, height = 6
        properties = {
          title = "Extraction time (ms)", region = var.region, period = 3600
          metrics = [
            [local.metric_namespace, "ExtractionDuration", { stat = "p50", label = "p50" }],
            ["...", { stat = "p95", label = "p95" }],
          ]
          annotations = { horizontal = [{ value = 120000, label = "SLO: 2 minutes" }] }
        }
      },
      {
        type = "metric", x = 16, y = 8, width = 8, height = 6
        properties = {
          title = "LLM spend (USD per hour)", region = var.region, stat = "Sum", period = 3600
          metrics = [[local.metric_namespace, "LlmCostUsd", { label = "Estimated spend" }]]
        }
      },
      {
        type = "log", x = 0, y = 14, width = 24, height = 6
        properties = {
          title  = "Recent errors"
          region = var.region
          query  = "SOURCE '${aws_cloudwatch_log_group.app.name}' | fields @timestamp, logger, message, request_id, trace_id | filter level = \"ERROR\" | sort @timestamp desc | limit 20"
          view   = "table"
        }
      },
    ]
  })
}

# ---------------------------------------------------------------- outputs

output "dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.main.dashboard_name}"
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "canary_name" {
  value = aws_synthetics_canary.uptime.name
}
