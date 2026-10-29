# Step 1: somewhere safe to keep Terraform's state.
#
# Terraform records everything it creates in a "state" file. Kept on a laptop,
# it gets lost, can't be shared with CI, and two people running Terraform at
# once can corrupt it. So we keep it in S3: versioned (every change can be
# rolled back), encrypted, private, and locked while someone is applying.
#
# This folder is the one exception: it creates the bucket the other folders
# store their state in, so its own (tiny) state stays local. Run it once.
#
#   cd infra/bootstrap
#   terraform init
#   terraform apply

terraform {
  required_version = ">= 1.15"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.23" # 6.23+ understands `aws login` profiles directly
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  # Every resource gets these tags, so cost reports and the console can show
  # what belongs to Ledgerline and how it was created.
  default_tags {
    tags = {
      project    = "ledgerline"
      managed_by = "terraform"
      stack      = "bootstrap"
    }
  }
}

variable "region" {
  description = "AWS Region. Projects created with the new AWS sign-up get one assigned; check AWS Settings > Projects > Additional info."
  type        = string
  default     = "ap-southeast-2"
}

variable "aws_profile" {
  description = "AWS CLI profile to use (the one you ran `aws login --profile ...` with). Set to null in CI."
  type        = string
  default     = "ledgerline"
}

data "aws_caller_identity" "current" {}

locals {
  # Bucket names are global across all AWS customers; the account ID makes ours unique.
  state_bucket = "ledgerline-tfstate-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket

  # Refuse `terraform destroy` on this bucket: losing state means Terraform
  # forgets everything it created.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Old state versions are kept for 90 days, then cleaned up.
resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
  depends_on = [aws_s3_bucket_versioning.state]
}

# Only encrypted (HTTPS) connections may read or write state.
data "aws_iam_policy_document" "state" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.state.arn,
      "${aws_s3_bucket.state.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket     = aws_s3_bucket.state.id
  policy     = data.aws_iam_policy_document.state.json
  depends_on = [aws_s3_bucket_public_access_block.state]
}

output "state_bucket" {
  description = "Use this in the backend block of every other Terraform folder."
  value       = aws_s3_bucket.state.bucket
}

output "account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "region" {
  value = var.region
}
