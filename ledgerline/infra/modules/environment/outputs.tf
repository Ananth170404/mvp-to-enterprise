output "url" {
  description = "Open this. Username: anything; password: the site_password parameter."
  value       = "https://${aws_cloudfront_distribution.app.domain_name}"
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "db_endpoint" {
  description = "Hostname of the database (reachable only from inside the VPC)."
  value       = aws_db_instance.main.address
}

output "db_master_secret_arn" {
  description = "Secrets Manager secret holding the master (schema owner) credentials."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "app_security_group_id" {
  value = aws_security_group.app.id
}

output "documents_bucket" {
  value = module.documents.name
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

output "dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.main.dashboard_name}"
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "canary_name" {
  value = aws_synthetics_canary.uptime.name
}
