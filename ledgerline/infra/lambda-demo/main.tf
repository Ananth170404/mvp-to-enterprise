# Ledgerline on AWS Lambda (guide Chapter 4, part 2): the same FastAPI app,
# run per request, scaling to zero, behind an API Gateway HTTP API.
#
#   browser -> API Gateway (HTTP API) -> Lambda (Python 3.12 + Lambda Web Adapter) -> uvicorn -> app
#
# Build the package first (from ledgerline/, see deploy/lambda/build.sh), then:
#   cd ledgerline/infra/lambda-demo
#   terraform init "-backend-config=../staging/backend.hcl" "-backend-config=key=lambda-demo/terraform.tfstate"
#   terraform apply
#   ...
#   terraform destroy

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
  }
  backend "s3" {}
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
  default_tags {
    tags = { project = "ledgerline", environment = "lambda-demo", managed_by = "terraform", stack = "lambda-demo" }
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

variable "fake_llm_delay_seconds" {
  description = "Make the fake model take as long as a real one, to see what happens to background work on Lambda."
  type        = number
  default     = 10
}

data "aws_caller_identity" "current" {}

locals {
  name = "ledgerline-lambda-demo"
  # Lambda Web Adapter, published by AWS as a public layer (x86_64, version 30).
  # Check https://github.com/aws/aws-lambda-web-adapter for newer versions.
  web_adapter_layer = "arn:aws:lambda:${var.region}:753240598075:layer:LambdaAdapterLayerX86:30"
}

# ---------------------------------------------------------------- package

# Built by deploy/lambda/build.sh, inside Linux (file permissions matter).
locals {
  package = "${path.module}/../../build/ledgerline-lambda.zip"
}

# The zip (~50+ MB) is over Lambda's 50 MB direct-upload limit, so it goes
# through S3. The bucket holds only build artifacts.
module "artifacts" {
  source                  = "../modules/private_bucket"
  name                    = "${local.name}-${data.aws_caller_identity.current.account_id}"
  noncurrent_version_days = 1
  expire_after_days       = 7
  force_destroy           = true
}

resource "aws_s3_object" "package" {
  bucket      = module.artifacts.name
  key         = "ledgerline-${filesha256(local.package)}.zip"
  source      = local.package
  source_hash = filesha256(local.package)
}

# ---------------------------------------------------------------- function

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "function" {
  name               = local.name
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.function.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Created before the function, so it has a retention period. Lambda would
# otherwise create it on first run with logs kept forever.
resource "aws_cloudwatch_log_group" "function" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 7
}

# Demo shortcut: this password is in Terraform state and visible in the
# function's configuration. Acceptable for a throwaway demo with synthetic
# data; a real deployment reads it from Parameter Store or Secrets Manager.
resource "random_password" "site" {
  length  = 20
  special = false
}

resource "aws_lambda_function" "app" {
  function_name    = local.name
  role             = aws_iam_role.function.arn
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  handler          = "run.sh" # the Web Adapter runs this script to start the web server
  s3_bucket        = aws_s3_object.package.bucket
  s3_key           = aws_s3_object.package.key
  source_code_hash = filebase64sha256(local.package)
  layers           = [local.web_adapter_layer]
  memory_size      = 1024 # more memory = more CPU = faster cold starts
  timeout          = 30   # API Gateway gives up at 30 s anyway

  environment {
    variables = {
      AWS_LAMBDA_EXEC_WRAPPER      = "/opt/bootstrap" # hand start-up to the Web Adapter
      AWS_LWA_PORT                 = "8000"
      AWS_LWA_READINESS_CHECK_PATH = "/livez"
      APP_ENV                      = "development"                 # local storage; see run.sh
      DATABASE_URL                 = "sqlite:////tmp/ledgerline.db" # per instance, lost when it's recycled
      STORAGE_BACKEND              = "local"
      STORAGE_DIR                  = "/tmp/storage" # /tmp is the only writable path on Lambda
      LLM_PROVIDER                 = "fake"
      FAKE_LLM_DELAY_SECONDS       = tostring(var.fake_llm_delay_seconds)
      DEV_LOGIN                    = "true"
      SITE_PASSWORD                = random_password.site.result
      LOG_FORMAT                   = "json"
    }
  }

  depends_on = [aws_cloudwatch_log_group.function, aws_iam_role_policy_attachment.logs]
}

# ---------------------------------------------------------------- HTTP API

resource "aws_apigatewayv2_api" "http" {
  name          = local.name
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "app" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.app.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "all" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "$default" # every method and path goes to the app
  target    = "integrations/${aws_apigatewayv2_integration.app.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true

  # A public endpoint that bills per request: cap it, so a script hammering
  # the URL can't run up a bill.
  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }
}

# Let API Gateway (this API only) invoke the function.
resource "aws_lambda_permission" "api" {
  statement_id  = "AllowHttpApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.app.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}

# ---------------------------------------------------------------- outputs

output "url" {
  value = aws_apigatewayv2_stage.default.invoke_url
}

output "site_password" {
  value     = random_password.site.result
  sensitive = true # shown only with: terraform output -raw site_password
}
