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

# What the user may do: push images to the ledgerline repository, nothing else.
# Deploy permissions are added in the next step, when there is something to deploy.
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

output "github_ci_user" {
  description = "Create its access key with the AWS CLI and store it as GitHub secrets."
  value       = aws_iam_user.github_ci.name
}
