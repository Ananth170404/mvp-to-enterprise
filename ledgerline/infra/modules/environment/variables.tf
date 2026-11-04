# ---------------------------------------------------------------- identity

variable "environment" {
  description = "staging, production, ... Used in every name."
  type        = string
}

variable "region" {
  type = string
}

# ---------------------------------------------------------------- network

variable "vpc_id" {
  type = string
}

variable "alb_subnet_ids" {
  description = "Public subnets for the load balancer (at least two AZs)."
  type        = list(string)
}

variable "app_subnet_ids" {
  description = "Where the app tasks run: public subnets with assign_public_ip, or private subnets with NAT."
  type        = list(string)
}

variable "db_subnet_ids" {
  description = "Subnets for the database (at least two AZs, even for single-AZ)."
  type        = list(string)
}

variable "assign_public_ip" {
  description = "true when app_subnet_ids are public subnets without NAT (staging)."
  type        = bool
}

# ---------------------------------------------------------------- app

variable "ecr_repository_url" {
  type = string
}

variable "llm_provider" {
  description = "fake (no API calls) or gemini (set the gemini_api_key parameter first)."
  type        = string
  default     = "fake"
}

variable "cloudwatch_agent_image" {
  description = "CloudWatch agent image (needs 1.300070.0+ for OTLP). Pin a version or digest once you've seen it work."
  type        = string
  default     = "public.ecr.aws/cloudwatch-agent/cloudwatch-agent:latest"
}

# ---------------------------------------------------------------- database

variable "free_plan" {
  description = "true while the AWS account is on the Free plan; set false after upgrading."
  type        = bool
  default     = true
}

variable "multi_az" {
  description = "A synchronous standby in a second AZ, with automatic failover. Roughly doubles the database cost."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Refuse to delete the database. true for production with real data."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------- secrets and observability

variable "secret_versions" {
  description = "Increase a number to send a new value for that secret (rotation). Write-only values are only re-sent when their version changes."
  type        = map(number)
  default = {
    app_db_password = 1
    site_password   = 1
    gemini_api_key  = 1
  }
}

variable "alert_email" {
  description = "Where alarm notifications go. Put it in terraform.tfvars (git-ignored), not in code."
  type        = string
}

variable "alarms_enabled" {
  description = "false silences notifications (e.g. while staging is paused). Alarms still change state."
  type        = bool
  default     = true
}

variable "llm_cost_alarm_usd_per_hour" {
  description = "Alert when estimated LLM spend in one hour exceeds this."
  type        = number
  default     = 1
}
