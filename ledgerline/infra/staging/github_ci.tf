# Step 5: let GitHub Actions push images to ECR.
#
# The best way is OIDC: GitHub gives each run a short-lived signed token and
# AWS swaps it for temporary credentials. No stored keys at all. But accounts
# created with the new AWS sign-up experience deny iam:*Provider* in a service
# control policy you can't change (it lifts only after "activate advanced
# features", which is irreversible). So this account uses the fallback:
#
#   an IAM user that can do exactly one thing (push to the ledgerline
#   repository), whose access key lives only in GitHub's encrypted secrets.
#
# The key is created by hand with the AWS CLI, NOT by Terraform: an
# aws_iam_access_key resource would store the secret in Terraform state.
#
#   aws iam create-access-key --user-name ledgerline-staging-github-ci --profile ledgerline
#
# Rotate it: create a new key, update the GitHub secrets, delete the old key.
# When the account can use OIDC, replace this user with a role (guide 2.x).

resource "aws_iam_user" "github_ci" {
  name = "${local.name}-github-ci"
  path = "/ci/"
}

# What the user may do: push images to the ledgerline repository and deploy
# this one environment. Nothing else.
# Deploy permissions (Step 6) are further down.
data "aws_iam_policy_document" "github_ci_ecr" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"] # can't be limited to one repository
    resources = ["*"]
  }
  statement {
    sid = "PushToLedgerlineRepository"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.app.arn]
  }
}

resource "aws_iam_user_policy" "github_ci_ecr" {
  name   = "push-images"
  user   = aws_iam_user.github_ci.name
  policy = data.aws_iam_policy_document.github_ci_ecr.json
}

# Step 6: deploy. Everything scripts/deploy_ecs.sh does, scoped to this
# environment's cluster, service, task families and roles.
locals {
  ecs_arn_prefix = "arn:aws:ecs:${var.region}:${local.account_id}"
}

data "aws_iam_policy_document" "github_ci_deploy" {
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

resource "aws_iam_user_policy" "github_ci_deploy" {
  name   = "deploy-staging"
  user   = aws_iam_user.github_ci.name
  policy = data.aws_iam_policy_document.github_ci_deploy.json
}

output "github_ci_user" {
  description = "Create its access key with the AWS CLI and store it as GitHub secrets."
  value       = aws_iam_user.github_ci.name
}
