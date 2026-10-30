# Step 4: secrets.
#
# Stored in SSM Parameter Store as SecureString (encrypted with KMS; standard
# parameters are free). ECS injects them into the containers as environment
# variables at start-up (Step 5), so the app reads them like any other config.
#
# Generated secrets never touch Terraform's state: they are produced by an
# *ephemeral* resource (exists only during the run) and sent with a
# *write-only* argument (value_wo), which Terraform sends to AWS but never
# stores. Needs Terraform 1.11+.
#
# The database owner's password isn't here: RDS manages it in Secrets Manager
# (database.tf).

locals {
  ssm_prefix = "/ledgerline/${var.environment}"
}

variable "secret_versions" {
  description = "Increase a number to send a new value for that secret (rotation). Write-only values are only re-sent when their version changes."
  type        = map(number)
  default = {
    app_db_password = 1
    site_password   = 1
    gemini_api_key  = 1
  }
}

# Password for ledgerline_app, the restricted database user (guide 2.3). The
# migration task sets it on the database (python -m app.db_roles); the app
# uses it to connect. After rotating, run a deploy so both pick it up.
ephemeral "random_password" "app_db" {
  length           = 32
  special          = true
  override_special = "-_=+.:!#%*" # avoids quotes and backslashes that trip up shells and tools
}

resource "aws_ssm_parameter" "app_db_password" {
  name             = "${local.ssm_prefix}/app_db_password"
  description      = "Password for ledgerline_app (read/write rows only)"
  type             = "SecureString"
  value_wo         = ephemeral.random_password.app_db.result
  value_wo_version = var.secret_versions.app_db_password
}

# Shared password in front of staging (HTTP Basic auth, guide 2.2). Read it with:
#   aws ssm get-parameter --name /ledgerline/staging/site_password --with-decryption --query Parameter.Value --output text
ephemeral "random_password" "site" {
  length  = 24
  special = false # easy to type into a browser's login box
}

resource "aws_ssm_parameter" "site_password" {
  name             = "${local.ssm_prefix}/site_password"
  description      = "HTTP Basic auth password for the staging site"
  type             = "SecureString"
  value_wo         = ephemeral.random_password.site.result
  value_wo_version = var.secret_versions.site_password
}

# Gemini API key. Terraform only creates the parameter with a placeholder;
# you set the real value from the CLI, so the key never appears in code,
# state or CI logs:
#   aws ssm put-parameter --name /ledgerline/staging/gemini_api_key --type SecureString --overwrite --value <key>
# Staging starts with LLM_PROVIDER=fake, so this is optional until you switch.
resource "aws_ssm_parameter" "gemini_api_key" {
  name             = "${local.ssm_prefix}/gemini_api_key"
  description      = "Gemini API key (set manually; placeholder until then)"
  type             = "SecureString"
  value_wo         = "not-set"
  value_wo_version = var.secret_versions.gemini_api_key
}

# Sentry DSN (Chapter 3). Same pattern as the Gemini key: Terraform creates a
# placeholder, you set the real value from the CLI. The app treats anything
# that isn't an https:// URL as "Sentry off", so the placeholder is harmless.
#   aws ssm put-parameter --name /ledgerline/staging/sentry_dsn --type SecureString --overwrite --value <dsn>
resource "aws_ssm_parameter" "sentry_dsn" {
  name             = "${local.ssm_prefix}/sentry_dsn"
  description      = "Sentry DSN for error tracking (set manually; placeholder until then)"
  type             = "SecureString"
  value_wo         = "not-set"
  value_wo_version = 1
}

output "secret_parameters" {
  description = "Parameter Store names. Values are never shown by Terraform."
  value = {
    app_db_password = aws_ssm_parameter.app_db_password.name
    site_password   = aws_ssm_parameter.site_password.name
    gemini_api_key  = aws_ssm_parameter.gemini_api_key.name
    sentry_dsn      = aws_ssm_parameter.sentry_dsn.name
  }
}
