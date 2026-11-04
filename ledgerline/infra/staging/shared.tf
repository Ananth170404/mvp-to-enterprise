# Account-wide pieces, used by every environment. They live here because
# staging was first; a larger setup would give them their own root ("shared"
# or "platform") applied before any environment.

# ---------------------------------------------------------------- registry

# Where CI pushes the runtime image. Tags are IMMUTABLE: once an image is
# pushed as a commit SHA, that tag can never point at different code. That's
# what makes "build once, promote the same image" trustworthy.
resource "aws_ecr_repository" "app" {
  name                 = "ledgerline"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true # basic vulnerability scan of every pushed image
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Keep the 20 most recent images; older ones are deleted automatically.
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 20 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}

# ---------------------------------------------------------------- traces

# Transaction Search stores every span sent to X-Ray as structured logs in the
# aws/spans log group, and indexes 1% for free as trace summaries. X-Ray needs
# permission to write those logs. Enabling the destination itself is a
# one-time account setting, done with the CLI (guide 3B.10, step 4):
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
