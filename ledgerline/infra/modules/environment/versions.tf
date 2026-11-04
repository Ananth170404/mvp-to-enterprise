# One Ledgerline environment (guide Chapter 4): everything staging and
# production have in common, with the differences as inputs.
#
#   network.tf        security groups (the VPC and subnets are inputs)
#   database.tf       RDS Postgres
#   secrets.tf        Parameter Store values (write-only)
#   documents.tf      the documents bucket
#   app.tf            ECS, load balancer, CloudFront, IAM roles
#   observability.tf  alarms, canary, dashboard, saved queries
#   ci.tf             what CI may do to deploy this environment
#
# Providers come from the calling root (region, profile, default tags).

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
    random = {
      source = "hashicorp/random"
    }
    archive = {
      source = "hashicorp/archive"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  name       = "ledgerline-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
}
