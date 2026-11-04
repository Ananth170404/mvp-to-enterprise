# Ledgerline staging environment.
#
# Built step by step in Chapter 2 (registry, database, secrets, CI, ECS) and
# Chapter 3 (observability). In Chapter 4 the environment itself moved into a
# module shared with production (../modules/environment); this root keeps:
#   main.tf       providers, the default-VPC network, the environment module
#   github_ci.tf  the CI identity (pushes images, deploys staging)
#   shared.tf     account-wide pieces: the image registry, X-Ray Transaction Search
#   moved.tf      tells Terraform the resources moved into the module (no rebuild)
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
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7" # zips the uptime canary's script (observability.tf)
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

# Personal and tunable values: set in terraform.tfvars (git-ignored).
variable "alert_email" {
  type = string
}

variable "alarms_enabled" {
  type    = bool
  default = true
}

variable "llm_cost_alarm_usd_per_hour" {
  type    = number
  default = 1
}

variable "llm_provider" {
  type    = string
  default = "fake"
}

variable "cloudwatch_agent_image" {
  type    = string
  default = "public.ecr.aws/cloudwatch-agent/cloudwatch-agent:latest"
}

variable "free_plan" {
  type    = bool
  default = true
}

variable "secret_versions" {
  type = map(number)
  default = {
    app_db_password = 1
    site_password   = 1
    gemini_api_key  = 1
  }
}

data "aws_caller_identity" "current" {}

locals {
  name       = "ledgerline-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
}

# ---------------------------------------------------------------- network

# Staging uses the account's DEFAULT VPC: it already exists, costs nothing,
# and has subnets in every AZ. Its subnets are public; the database isn't
# publicly accessible and only accepts the app's security group. Production
# gets its own VPC with private subnets (../production).
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

# ---------------------------------------------------------------- the environment

module "env" {
  source = "../modules/environment"

  environment = var.environment
  region      = var.region

  vpc_id           = data.aws_vpc.default.id
  alb_subnet_ids   = slice(sort(data.aws_subnets.default.ids), 0, 2) # two: each costs a public IPv4 address
  app_subnet_ids   = data.aws_subnets.default.ids
  db_subnet_ids    = data.aws_subnets.default.ids
  assign_public_ip = true # no NAT gateway in staging (~$40/month saved)

  ecr_repository_url     = aws_ecr_repository.app.repository_url
  llm_provider           = var.llm_provider
  cloudwatch_agent_image = var.cloudwatch_agent_image

  free_plan           = var.free_plan
  multi_az            = false
  deletion_protection = false

  secret_versions             = var.secret_versions
  alert_email                 = var.alert_email
  alarms_enabled              = var.alarms_enabled
  llm_cost_alarm_usd_per_hour = var.llm_cost_alarm_usd_per_hour
}

# ---------------------------------------------------------------- outputs

output "staging_url" {
  description = "Open this. Username: anything; password: the site_password parameter."
  value       = module.env.url
}

output "ecs_cluster" {
  value = module.env.ecs_cluster
}

output "ecr_repository_url" {
  description = "CI pushes images here, tagged with the git commit SHA."
  value       = aws_ecr_repository.app.repository_url
}

output "documents_bucket" {
  value = module.env.documents_bucket
}

output "db_endpoint" {
  value = module.env.db_endpoint
}

output "db_master_secret_arn" {
  value = module.env.db_master_secret_arn
}

output "app_security_group_id" {
  value = module.env.app_security_group_id
}

output "subnet_ids" {
  value = data.aws_subnets.default.ids
}

output "secret_parameters" {
  value = module.env.secret_parameters
}

output "dashboard_url" {
  value = module.env.dashboard_url
}

output "alerts_topic_arn" {
  value = module.env.alerts_topic_arn
}

output "canary_name" {
  value = module.env.canary_name
}
