# Ledgerline staging environment.
#
# Built up step by step (guide Chapter 2):
#   Step 2  container registry + documents bucket   <- this file
#   Step 3  Postgres (RDS)                          <- database.tf, network.tf
#   Step 4  secrets                                 <- secrets.tf
#   Step 5  GitHub Actions pushes images           <- github_ci.tf
#   Step 6  the app on ECS Fargate, deployed by CI  <- app.tf
#
#   cd ledgerline/infra/staging
#   copy backend.hcl.example backend.hcl      (then fill in your bucket name)
#   terraform init -backend-config=backend.hcl
#   terraform plan
#   terraform apply

terraform {
  required_version = ">= 1.15"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.23"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7" # 3.7+ has ephemeral random_password (secrets.tf)
    }
  }

  # State lives in the bucket created by ../bootstrap. The settings are in
  # backend.hcl (not committed: it names your account's bucket), because a
  # backend block can't use variables.
  backend "s3" {}
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      project     = "ledgerline"
      environment = var.environment
      managed_by  = "terraform"
      stack       = "staging"
    }
  }
}

variable "region" {
  type    = string
  default = "ap-southeast-2"
}

variable "aws_profile" {
  description = "AWS CLI profile from `aws login --profile ...`. Set to null in CI."
  type        = string
  default     = "ledgerline"
}

variable "environment" {
  type    = string
  default = "staging"
}

data "aws_caller_identity" "current" {}

locals {
  name       = "ledgerline-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
}

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

# ---------------------------------------------------------------- documents

# Uploaded invoices. Container disks vanish on every deploy, so files live
# here (STORAGE_BACKEND=s3). Versioning means an overwritten or deleted
# invoice can be recovered for 30 days.
module "documents" {
  source                  = "../modules/private_bucket"
  name                    = "${local.name}-documents-${local.account_id}"
  noncurrent_version_days = 30
}

# ---------------------------------------------------------------- outputs

output "ecr_repository_url" {
  description = "Push images here, tagged with the git commit SHA."
  value       = aws_ecr_repository.app.repository_url
}

output "documents_bucket" {
  description = "Set as S3_BUCKET for the app."
  value       = module.documents.name
}
