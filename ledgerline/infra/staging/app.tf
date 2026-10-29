# Step 6: the app on ECS Fargate, behind a load balancer and HTTPS.
#
#   browser --HTTPS--> CloudFront --HTTP--> load balancer --> app containers --> RDS
#                      (free *.cloudfront.net certificate)    (ECS Fargate)      S3
#
# Why each piece:
#   ECS Fargate      runs containers without servers to patch. You say "run 1
#                    copy of this image"; it replaces copies that die.
#   load balancer    one stable address in front of containers that come and
#                    go; health checks decide which ones get traffic.
#   CloudFront       HTTPS without owning a domain. The staging password (HTTP
#                    Basic auth) would otherwise cross the internet in clear text.
#
# Who decides what:
#   Terraform defines HOW the app runs (CPU, memory, settings, secrets).
#   CI decides WHICH IMAGE runs: each deploy copies the latest task definition,
#   swaps in the new image, runs the migration task, then rolls the service
#   (scripts/deploy_ecs.sh). So Terraform ignores the service's task definition
#   and desired count, which CI changes on every deploy. A settings change made
#   here takes effect on the next deploy.

variable "llm_provider" {
  description = "fake (no API calls) or gemini (set the gemini_api_key parameter first)."
  type        = string
  default     = "fake"
}

locals {
  container_port = 8000 # what uvicorn listens on inside the image (Dockerfile)
  # Two subnets: the minimum a load balancer needs. Each costs a public IPv4 address.
  alb_subnets = slice(sort(data.aws_subnets.default.ids), 0, 2)

  # Placeholder image tag. CI replaces it on every deploy; Terraform's revision
  # of the task definition is only the template CI copies.
  template_image = "${aws_ecr_repository.app.repository_url}:template"

  db_secret_arn = aws_db_instance.main.master_user_secret[0].secret_arn

  # Settings shared by the app and the migration task.
  common_environment = [
    { name = "APP_ENV", value = var.environment },
    { name = "AWS_REGION", value = var.region },
    { name = "DB_HOST", value = aws_db_instance.main.address },
    { name = "DB_NAME", value = aws_db_instance.main.db_name },
    { name = "DB_USER", value = "ledgerline_app" },
    { name = "STORAGE_BACKEND", value = "s3" },
    { name = "S3_BUCKET", value = module.documents.name },
  ]
}

# ---------------------------------------------------------------- logs

# Everything the containers print goes here. 14 days is plenty for staging;
# unlimited retention is a classic slow-growing bill.
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 14
}

# ---------------------------------------------------------------- IAM

# Two roles, two jobs (a common point of confusion):
#   execution role  used by ECS itself, BEFORE your code runs: pull the image,
#                   fetch secrets, create the log stream.
#   task role       used by YOUR CODE while it runs: read and write S3.

data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition { # only tasks in this account (prevents "confused deputy" misuse)
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  name               = "${local.name}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy" # pull images, write logs
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    sid     = "ParameterStore"
    actions = ["ssm:GetParameters"]
    resources = [
      aws_ssm_parameter.app_db_password.arn,
      aws_ssm_parameter.site_password.arn,
      aws_ssm_parameter.gemini_api_key.arn,
    ]
  }
  statement {
    sid       = "DatabaseOwnerPassword"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.db_secret_arn]
  }
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  name   = "read-secrets"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

data "aws_iam_policy_document" "documents_bucket" {
  statement {
    sid       = "ReadWriteDocuments"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${module.documents.arn}/*"]
  }
  statement {
    sid       = "ListForMissingObjects" # without it, a missing file is 403 instead of 404
    actions   = ["s3:ListBucket"]
    resources = [module.documents.arn]
  }
}

resource "aws_iam_role_policy" "task_documents" {
  name   = "documents-bucket"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.documents_bucket.json
}

# ---------------------------------------------------------------- task definitions

# The web app.
resource "aws_ecs_task_definition" "app" {
  family                   = "${local.name}-app"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256 # 0.25 vCPU
  memory                   = 512 # MiB
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64" # CI builds on x86 runners
  }

  container_definitions = jsonencode([{
    name      = "app"
    image     = local.template_image
    essential = true

    portMappings = [{ containerPort = local.container_port, protocol = "tcp" }]

    environment = concat(local.common_environment, [
      { name = "LLM_PROVIDER", value = var.llm_provider },
      { name = "DEV_LOGIN", value = "true" }, # demo login; requires SITE_PASSWORD (enforced by the app)
    ])

    # Injected as environment variables when the container starts. The values
    # never appear in the task definition, the console or Terraform state.
    secrets = [
      { name = "DB_PASSWORD", valueFrom = aws_ssm_parameter.app_db_password.arn },
      { name = "SITE_PASSWORD", valueFrom = aws_ssm_parameter.site_password.arn },
      { name = "GEMINI_API_KEY", valueFrom = aws_ssm_parameter.gemini_api_key.arn },
    ]

    # Liveness (guide 2.8): restart the container if the process stops answering.
    # Readiness is the load balancer's job (/readyz, below).
    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${local.container_port}/livez', timeout=3)"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 20
    }

    stopTimeout = 30 # uvicorn finishes in-flight requests within 25 s (Dockerfile)

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "app"
      }
    }
  }])
}

# The one-off migration task: runs before every deploy, then exits (guide 2.6).
# It is the only thing that uses the schema owner's password.
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "migrate"
    image     = local.template_image
    essential = true
    # 1. schema changes, as the owner  2. create/refresh the restricted app user
    # 3. demo tenants and users for the dev login (refuses in production)
    command = ["sh", "-c", "alembic upgrade head && python -m app.db_roles && python -m app.seed"]

    environment = concat(local.common_environment, [
      { name = "MIGRATION_DB_USER", value = aws_db_instance.main.username },
      { name = "LLM_PROVIDER", value = "fake" },
      { name = "DEV_LOGIN", value = "false" },
    ])

    secrets = [
      # RDS keeps the owner's credentials as JSON; ":password::" picks one key.
      { name = "MIGRATION_DB_PASSWORD", valueFrom = "${local.db_secret_arn}:password::" },
      { name = "DB_PASSWORD", valueFrom = aws_ssm_parameter.app_db_password.arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "migrate"
      }
    }
  }])
}

# ---------------------------------------------------------------- load balancer

# Only CloudFront may reach the load balancer. AWS publishes CloudFront's
# addresses as a managed prefix list, so this stays correct as they change.
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Ledgerline load balancer"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from CloudFront only"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
}

resource "aws_vpc_security_group_egress_rule" "alb_to_app" {
  security_group_id            = aws_security_group.alb.id
  description                  = "To the app containers"
  ip_protocol                  = "tcp"
  from_port                    = local.container_port
  to_port                      = local.container_port
  referenced_security_group_id = aws_security_group.app.id
}

resource "aws_vpc_security_group_ingress_rule" "app_from_alb" {
  security_group_id            = aws_security_group.app.id
  description                  = "App port from the load balancer only"
  ip_protocol                  = "tcp"
  from_port                    = local.container_port
  to_port                      = local.container_port
  referenced_security_group_id = aws_security_group.alb.id
}

resource "aws_lb" "app" {
  name                       = local.name
  load_balancer_type         = "application"
  internal                   = false
  subnets                    = local.alb_subnets
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true # reject malformed headers (request smuggling defence)
}

resource "aws_lb_target_group" "app" {
  name        = "${local.name}-app"
  port        = local.container_port
  protocol    = "HTTP"
  target_type = "ip" # Fargate tasks are registered by IP address
  vpc_id      = data.aws_vpc.default.id

  # Readiness (guide 2.8): a task gets traffic only while it can reach the database.
  health_check {
    path                = "/readyz"
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # On a deploy, give in-flight requests time to finish before the old task is
  # removed. Matches uvicorn's graceful shutdown (Dockerfile).
  deregistration_delay = 30
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# ---------------------------------------------------------------- ECS

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "disabled" # extra metrics cost money; Chapter 3 decides what to watch
  }
}

resource "aws_ecs_service" "app" {
  name            = "${local.name}-app"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  launch_type     = "FARGATE"

  # Starts at 0: the first deploy (CI) runs the migration, THEN starts the app.
  desired_count = 0

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true # to reach ECR, S3 and Gemini without a NAT gateway (~$40/month)
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = local.container_port
  }

  health_check_grace_period_seconds = 60

  # Rolling deploy: start the new task, wait until healthy, then stop the old
  # one. If new tasks keep failing, roll back to the last working version.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  propagate_tags = "SERVICE"

  lifecycle {
    ignore_changes = [task_definition, desired_count] # CI owns these
  }

  depends_on = [aws_lb_listener.http]
}

# ---------------------------------------------------------------- HTTPS

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled" # a dynamic, logged-in app: cache nothing
}

data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer" # forward everything, including the Authorization header
}

resource "aws_cloudfront_distribution" "app" {
  enabled     = true
  comment     = "${local.name} (HTTPS in front of the load balancer)"
  price_class = "PriceClass_All"

  origin {
    origin_id   = "alb"
    domain_name = aws_lb.app.dns_name
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only" # the ALB has no certificate (no domain yet)
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 60
    }
  }

  default_cache_behavior {
    target_origin_id         = "alb"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

# ---------------------------------------------------------------- outputs

output "staging_url" {
  description = "Open this. Username: anything; password: the site_password parameter."
  value       = "https://${aws_cloudfront_distribution.app.domain_name}"
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}
