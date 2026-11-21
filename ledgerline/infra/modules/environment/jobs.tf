# Step 8 (Chapter 6): the job queue between the web app and the workers.
#
#   app ──SendMessage──> extraction queue ──ReceiveMessage──> worker tasks
#                              │
#                              └── a job that fails 3 times ──> dead-letter queue ──> alarm
#
# SQS is managed, has no servers or minimum charge, and is effectively free at
# this volume (the first million requests a month are free). Local development
# uses ElasticMQ, which speaks the same API (docker-compose.yml).

locals {
  # A received job is hidden from other workers for this long. The worker
  # extends it every half-period while a job runs (app/worker.py), so it only
  # matters when a worker dies: its job reappears after at most this long.
  job_visibility_seconds = 120
}

resource "aws_sqs_queue" "jobs_dlq" {
  name                      = "${local.name}-extraction-dlq"
  message_retention_seconds = 1209600 # 14 days, the maximum: time for a person to look
  sqs_managed_sse_enabled   = true    # encrypted at rest (job messages hold IDs, not documents)
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${local.name}-extraction"
  visibility_timeout_seconds = local.job_visibility_seconds
  receive_wait_time_seconds  = 20     # long polling: an idle worker waits on the queue, not in a busy loop
  message_retention_seconds  = 345600 # 4 days: a job older than that is better retried by a person
  sqs_managed_sse_enabled    = true

  # Three deliveries, then the message moves to the dead-letter queue instead
  # of being retried forever (or blocking the workers with a poison message).
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    maxReceiveCount     = 3
  })
}

# Only the job queue may send to the dead-letter queue.
resource "aws_sqs_queue_redrive_allow_policy" "jobs_dlq" {
  queue_url = aws_sqs_queue.jobs_dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.jobs.arn]
  })
}

# ---------------------------------------------------------------- alarms

# Something failed three times and needs a person. Every message here is an
# invoice stuck until someone looks (runbook: guide 6B, "Dead letters").
resource "aws_cloudwatch_metric_alarm" "jobs_dead_letters" {
  alarm_name          = "${local.name}-jobs-dead-letters"
  alarm_description   = "Jobs failed 3 times and were moved to the dead-letter queue. Read the worker's job_failed / job_poison logs, fix the cause, then redrive. Runbook: guide/06b-architecture-demos.md"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.jobs_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  actions_enabled     = var.alarms_enabled
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# Workers aren't keeping up (or aren't running): users are waiting.
resource "aws_cloudwatch_metric_alarm" "jobs_backlog_age" {
  alarm_name          = "${local.name}-jobs-oldest-waiting"
  alarm_description   = "The oldest waiting job is over 15 minutes old: workers are down, failing to start, or too few. Check the worker service's events and tasks. Runbook: guide/06b-architecture-demos.md"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.jobs.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 900
  treat_missing_data  = "notBreaching"
  actions_enabled     = var.alarms_enabled
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}
