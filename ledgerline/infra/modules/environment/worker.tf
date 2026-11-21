# Step 9 (Chapter 6): the worker service. Same image as the web app, a
# different command (python -m app.worker), no load balancer: nothing calls a
# worker; it pulls work from the queue.
#
# It scales on the queue, not on CPU: a worker waiting on a slow model uses
# almost no CPU while users wait. Scaling (below):
#   jobs waiting                       → add a worker (up to worker_max_count)
#   queue empty for 10 minutes         → back to worker_min_count (0 in staging)

# ---------------------------------------------------------------- IAM

# The worker's own role: read documents, consume jobs. It can't write
# documents or send jobs; the web app's role can't consume them.
resource "aws_iam_role" "worker" {
  name               = "${local.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

data "aws_iam_policy_document" "worker" {
  statement {
    sid       = "ReadDocuments"
    actions   = ["s3:GetObject"]
    resources = ["${module.documents.arn}/*"]
  }
  statement {
    sid       = "ListForMissingObjects"
    actions   = ["s3:ListBucket"]
    resources = [module.documents.arn]
  }
  statement {
    sid = "ConsumeJobs"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility", # the heartbeat
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }
}

resource "aws_iam_role_policy" "worker" {
  name   = "worker"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

resource "aws_iam_role_policy_attachment" "worker_cloudwatch_agent" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

# The web app may only send jobs.
data "aws_iam_policy_document" "app_send_jobs" {
  statement {
    sid       = "SendJobs"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.jobs.arn]
  }
}

resource "aws_iam_role_policy" "app_send_jobs" {
  name   = "send-jobs"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.app_send_jobs.json
}

# ---------------------------------------------------------------- task definition

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 1024
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.worker.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "worker"
    image     = local.template_image # CI swaps in the real image, like the app's
    essential = true
    command   = ["python", "-m", "app.worker"]

    dependsOn = [{ containerName = "cloudwatch-agent", condition = "START" }]

    environment = concat(local.common_environment, [
      { name = "LLM_PROVIDER", value = var.llm_provider },
      { name = "FAKE_LLM_DELAY_SECONDS", value = tostring(var.fake_llm_delay_seconds) },
      { name = "DEV_LOGIN", value = "false" }, # a worker serves no pages
      { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = "http://localhost:4318" },
      { name = "OTEL_SERVICE_NAME", value = "ledgerline-worker" },
    ])

    secrets = [
      { name = "DB_PASSWORD", valueFrom = aws_ssm_parameter.app_db_password.arn },
      { name = "GEMINI_API_KEY", valueFrom = aws_ssm_parameter.gemini_api_key.arn },
      { name = "SENTRY_DSN", valueFrom = aws_ssm_parameter.sentry_dsn.arn },
    ]

    # Alive = the loop touched its heartbeat file recently (app/worker.py).
    # A worker stuck forever gets replaced; its job goes back on the queue.
    healthCheck = {
      command     = ["CMD", "python", "-m", "app.worker", "--check"]
      interval    = 30
      timeout     = 10
      retries     = 3
      startPeriod = 30
    }

    # On a deploy or scale-in, ECS sends SIGTERM; the worker finishes its
    # current job and stops. 120 s is Fargate's maximum. A job still running
    # after that is retried on another worker.
    stopTimeout = 120

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "worker"
      }
    }
    }, {
    name              = "cloudwatch-agent"
    image             = var.cloudwatch_agent_image
    essential         = false
    memoryReservation = 256
    environment = [
      { name = "CW_CONFIG_CONTENT", value = jsonencode({ opentelemetry = { collect = { otlp = {} } } }) },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "cwagent-worker"
      }
    }
  }])
}

# ---------------------------------------------------------------- service

resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  launch_type     = "FARGATE"
  desired_count   = 0 # autoscaling decides from here on

  network_configuration {
    subnets          = var.app_subnet_ids
    security_groups  = [aws_security_group.app.id] # same database access as the app
    assign_public_ip = var.assign_public_ip
  }

  # Start new workers before stopping old ones; roll back if they keep failing.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  propagate_tags = "SERVICE"

  lifecycle {
    ignore_changes = [task_definition, desired_count] # CI owns the image; autoscaling owns the count
  }
}

# ---------------------------------------------------------------- autoscaling on queue depth

resource "aws_appautoscaling_target" "worker" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.worker_min_count
  max_capacity       = var.worker_max_count
}

# Jobs waiting → one more worker, then another every 3 minutes while jobs keep
# waiting; two at a time if the backlog is large. The 3-minute cooldown is
# roughly how long a new task takes to start and begin taking jobs: without
# it, one job waiting during that start-up would add a worker every minute.
resource "aws_appautoscaling_policy" "worker_out" {
  name               = "${local.name}-worker-out"
  service_namespace  = aws_appautoscaling_target.worker.service_namespace
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  policy_type        = "StepScaling"

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 180
    metric_aggregation_type = "Maximum"
    step_adjustment { # 1-19 waiting (bounds are relative to the alarm threshold of 1)
      metric_interval_lower_bound = 0
      metric_interval_upper_bound = 19
      scaling_adjustment          = 1
    }
    step_adjustment { # 20 or more waiting
      metric_interval_lower_bound = 19
      scaling_adjustment          = 2
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_out" {
  alarm_name          = "${local.name}-worker-scale-out"
  alarm_description   = "Jobs are waiting: add workers. Drives autoscaling; not a page."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.jobs.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_appautoscaling_policy.worker_out.arn] # scaling actions stay on when notifications are paused
}

# Nothing waiting AND nothing in progress for 10 minutes → back to the minimum.
# "In progress" matters: scaling in while a worker holds a job would stop it
# part-way (it would be retried, but wastefully).
resource "aws_appautoscaling_policy" "worker_in" {
  name               = "${local.name}-worker-in"
  service_namespace  = aws_appautoscaling_target.worker.service_namespace
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  policy_type        = "StepScaling"

  step_scaling_policy_configuration {
    adjustment_type         = "ExactCapacity"
    cooldown                = 60
    metric_aggregation_type = "Maximum"
    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = var.worker_min_count
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_in" {
  alarm_name          = "${local.name}-worker-scale-in"
  alarm_description   = "The queue has been empty for 10 minutes: scale workers back to the minimum. Drives autoscaling; not a page."
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  evaluation_periods  = 10
  # SQS stops publishing metrics for a queue that has been inactive for a few
  # hours. No data then means "empty", which is the state this alarm looks for.
  treat_missing_data = "breaching"
  alarm_actions      = [aws_appautoscaling_policy.worker_in.arn]

  metric_query {
    id          = "total"
    expression  = "waiting + inflight"
    label       = "Jobs waiting or in progress"
    return_data = true
  }
  metric_query {
    id = "waiting"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      dimensions  = { QueueName = aws_sqs_queue.jobs.name }
      stat        = "Maximum"
      period      = 60
    }
  }
  metric_query {
    id = "inflight"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesNotVisible"
      dimensions  = { QueueName = aws_sqs_queue.jobs.name }
      stat        = "Maximum"
      period      = 60
    }
  }
}
