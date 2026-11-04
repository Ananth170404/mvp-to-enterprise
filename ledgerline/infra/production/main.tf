# Ledgerline production (guide Chapter 4, part 2).
#
# The same environment module as staging, with production values:
#
#                           staging                production
#   network                 default VPC, public    own VPC, private subnets + NAT
#   app tasks               1, public IP           2 (one per AZ), no public IP
#   database                single-AZ              Multi-AZ (standby + failover)
#   CI identity             pushes + deploys       deploys only, its own key, used
#                                                  only after a human approves
#
# In a real company production lives in its own AWS account (section 4.8);
# here it shares the account with staging to keep the demo affordable.
#
# COST: roughly $5/day while it exists (NAT gateway, Multi-AZ database, load
# balancer, two tasks). Create it, try promotion, destroy it.
#
#   cd ledgerline/infra/production
#   copy ..\staging\terraform.tfvars terraform.tfvars        (alert_email)
#   terraform init "-backend-config=../staging/backend.hcl" "-backend-config=key=production/terraform.tfstate"
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
      version = "~> 3.7"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }
  backend "s3" {}
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
  default_tags {
    tags = { project = "ledgerline", environment = "production", managed_by = "terraform", stack = "production" }
  }
}

variable "region" {
  type    = string
  default = "ap-southeast-2"
}

variable "aws_profile" {
  type    = string
  default = "ledgerline"
}

variable "alert_email" {
  type = string
}

variable "multi_az" {
  description = "Set false if the account's plan refuses Multi-AZ (FreeTierRestrictionError)."
  type        = bool
  default     = true
}

variable "free_plan" {
  type    = bool
  default = true
}

# The image registry is shared: production deploys images that staging built
# and tested. It's created by the staging root.
data "aws_ecr_repository" "app" {
  name = "ledgerline"
}

# ---------------------------------------------------------------- network

module "network" {
  source     = "../modules/network"
  name       = "ledgerline-production"
  cidr       = "10.20.0.0/16"
  az_count   = 2
  nat_per_az = false # one NAT for the demo; a real production setup uses one per AZ
}

# ---------------------------------------------------------------- the environment

module "env" {
  source = "../modules/environment"

  environment = "production"
  region      = var.region

  vpc_id           = module.network.vpc_id
  alb_subnet_ids   = module.network.public_subnet_ids
  app_subnet_ids   = module.network.private_subnet_ids
  db_subnet_ids    = module.network.private_subnet_ids
  assign_public_ip = false # private subnets; outbound through NAT

  ecr_repository_url = data.aws_ecr_repository.app.repository_url
  llm_provider       = "fake" # until a production Gemini key is set

  free_plan           = var.free_plan
  multi_az            = var.multi_az
  deletion_protection = false # true with real data; false so the demo can be destroyed

  alert_email = var.alert_email
}

# ---------------------------------------------------------------- CI identity

# Production gets its OWN CI identity, separate from staging's:
#   - it can deploy production and nothing else (no image pushes: production
#     only ever runs images staging already built and tested)
#   - its key is stored as a secret of the GitHub "production" environment,
#     which only jobs that a reviewer approved can read
resource "aws_iam_user" "github_ci" {
  name = "ledgerline-production-github-ci"
  path = "/ci/"
}

resource "aws_iam_user_policy" "deploy" {
  name   = "deploy-production"
  user   = aws_iam_user.github_ci.name
  policy = module.env.ci_deploy_policy_json
}

# ---------------------------------------------------------------- outputs

output "production_url" {
  value = module.env.url
}

output "ecs_cluster" {
  value = module.env.ecs_cluster
}

output "github_ci_user" {
  value = aws_iam_user.github_ci.name
}

output "dashboard_url" {
  value = module.env.dashboard_url
}
