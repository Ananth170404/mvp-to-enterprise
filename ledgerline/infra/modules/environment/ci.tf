# What a CI identity needs to deploy THIS environment (scripts/deploy_ecs.sh),
# scoped to its cluster, service, task families and roles. Each root attaches
# it to that environment's CI identity: the production policy grants nothing
# in staging, and vice versa.
locals {
  ecs_arn_prefix = "arn:aws:ecs:${var.region}:${local.account_id}"
}

data "aws_iam_policy_document" "ci_deploy" {
  statement {
    sid = "TaskDefinitions" # these two can't be limited to one family
    actions = [
      "ecs:DescribeTaskDefinition",
      "ecs:RegisterTaskDefinition",
    ]
    resources = ["*"]
  }
  statement {
    sid     = "RunMigration"
    actions = ["ecs:RunTask"]
    resources = [
      "${local.ecs_arn_prefix}:task-definition/${aws_ecs_task_definition.migrate.family}:*",
    ]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }
  statement {
    sid       = "WatchTasks"
    actions   = ["ecs:DescribeTasks"]
    resources = ["${local.ecs_arn_prefix}:task/${aws_ecs_cluster.main.name}/*"]
  }
  statement {
    sid       = "RollService"
    actions   = ["ecs:DescribeServices", "ecs:UpdateService"]
    resources = [aws_ecs_service.app.id]
  }
  statement {
    sid       = "ReadMigrationLogs" # printed in CI when a migration fails
    actions   = ["logs:GetLogEvents"]
    resources = ["${aws_cloudwatch_log_group.app.arn}:log-stream:migrate/*"]
  }
  statement {
    sid       = "HandRolesToEcs" # a task definition names its roles; ECS must be allowed to use them
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.task_execution.arn, aws_iam_role.task.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

output "ci_deploy_policy_json" {
  value = data.aws_iam_policy_document.ci_deploy.json
}
