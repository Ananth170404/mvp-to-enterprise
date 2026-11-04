# Chapter 4 moved staging's resources into ../modules/environment. These
# blocks tell Terraform "same resource, new address", so the first plan after
# the move shows "has moved to" instead of destroy-and-recreate. Without them,
# Terraform would delete the database and build a new, empty one.
#
# Keep them until every copy of this state has been applied once; then they
# can be deleted.

moved {
  from = aws_security_group.app
  to   = module.env.aws_security_group.app
}

moved {
  from = aws_vpc_security_group_egress_rule.app_all
  to   = module.env.aws_vpc_security_group_egress_rule.app_all
}

moved {
  from = aws_security_group.db
  to   = module.env.aws_security_group.db
}

moved {
  from = aws_vpc_security_group_ingress_rule.db_from_app
  to   = module.env.aws_vpc_security_group_ingress_rule.db_from_app
}

moved {
  from = aws_db_subnet_group.main
  to   = module.env.aws_db_subnet_group.main
}

moved {
  from = aws_db_instance.main
  to   = module.env.aws_db_instance.main
}

moved {
  from = aws_ssm_parameter.app_db_password
  to   = module.env.aws_ssm_parameter.app_db_password
}

moved {
  from = aws_ssm_parameter.site_password
  to   = module.env.aws_ssm_parameter.site_password
}

moved {
  from = aws_ssm_parameter.gemini_api_key
  to   = module.env.aws_ssm_parameter.gemini_api_key
}

moved {
  from = aws_ssm_parameter.sentry_dsn
  to   = module.env.aws_ssm_parameter.sentry_dsn
}

moved {
  from = aws_cloudwatch_log_group.app
  to   = module.env.aws_cloudwatch_log_group.app
}

moved {
  from = aws_iam_role.task_execution
  to   = module.env.aws_iam_role.task_execution
}

moved {
  from = aws_iam_role_policy_attachment.task_execution
  to   = module.env.aws_iam_role_policy_attachment.task_execution
}

moved {
  from = aws_iam_role_policy.task_execution_secrets
  to   = module.env.aws_iam_role_policy.task_execution_secrets
}

moved {
  from = aws_iam_role.task
  to   = module.env.aws_iam_role.task
}

moved {
  from = aws_iam_role_policy.task_documents
  to   = module.env.aws_iam_role_policy.task_documents
}

moved {
  from = aws_ecs_task_definition.app
  to   = module.env.aws_ecs_task_definition.app
}

moved {
  from = aws_iam_role_policy_attachment.task_cloudwatch_agent
  to   = module.env.aws_iam_role_policy_attachment.task_cloudwatch_agent
}

moved {
  from = aws_ecs_task_definition.migrate
  to   = module.env.aws_ecs_task_definition.migrate
}

moved {
  from = aws_security_group.alb
  to   = module.env.aws_security_group.alb
}

moved {
  from = aws_vpc_security_group_ingress_rule.alb_from_cloudfront
  to   = module.env.aws_vpc_security_group_ingress_rule.alb_from_cloudfront
}

moved {
  from = aws_vpc_security_group_egress_rule.alb_to_app
  to   = module.env.aws_vpc_security_group_egress_rule.alb_to_app
}

moved {
  from = aws_vpc_security_group_ingress_rule.app_from_alb
  to   = module.env.aws_vpc_security_group_ingress_rule.app_from_alb
}

moved {
  from = aws_lb.app
  to   = module.env.aws_lb.app
}

moved {
  from = aws_lb_target_group.app
  to   = module.env.aws_lb_target_group.app
}

moved {
  from = aws_lb_listener.http
  to   = module.env.aws_lb_listener.http
}

moved {
  from = aws_ecs_cluster.main
  to   = module.env.aws_ecs_cluster.main
}

moved {
  from = aws_ecs_service.app
  to   = module.env.aws_ecs_service.app
}

moved {
  from = aws_cloudfront_distribution.app
  to   = module.env.aws_cloudfront_distribution.app
}

moved {
  from = aws_sns_topic.alerts
  to   = module.env.aws_sns_topic.alerts
}

moved {
  from = aws_sns_topic_subscription.alert_email
  to   = module.env.aws_sns_topic_subscription.alert_email
}

moved {
  from = aws_cloudwatch_log_metric_filter.extractions_total
  to   = module.env.aws_cloudwatch_log_metric_filter.extractions_total
}

moved {
  from = aws_cloudwatch_log_metric_filter.extractions_failed
  to   = module.env.aws_cloudwatch_log_metric_filter.extractions_failed
}

moved {
  from = aws_cloudwatch_log_metric_filter.extraction_duration
  to   = module.env.aws_cloudwatch_log_metric_filter.extraction_duration
}

moved {
  from = aws_cloudwatch_log_metric_filter.llm_cost
  to   = module.env.aws_cloudwatch_log_metric_filter.llm_cost
}

moved {
  from = aws_cloudwatch_log_metric_filter.app_errors
  to   = module.env.aws_cloudwatch_log_metric_filter.app_errors
}

moved {
  from = aws_cloudwatch_metric_alarm.availability_burn
  to   = module.env.aws_cloudwatch_metric_alarm.availability_burn
}

moved {
  from = aws_cloudwatch_metric_alarm.latency_p95
  to   = module.env.aws_cloudwatch_metric_alarm.latency_p95
}

moved {
  from = aws_cloudwatch_metric_alarm.no_healthy_targets
  to   = module.env.aws_cloudwatch_metric_alarm.no_healthy_targets
}

moved {
  from = aws_cloudwatch_metric_alarm.extraction_slow
  to   = module.env.aws_cloudwatch_metric_alarm.extraction_slow
}

moved {
  from = aws_cloudwatch_metric_alarm.extraction_failures
  to   = module.env.aws_cloudwatch_metric_alarm.extraction_failures
}

moved {
  from = aws_cloudwatch_metric_alarm.llm_cost
  to   = module.env.aws_cloudwatch_metric_alarm.llm_cost
}

moved {
  from = aws_cloudwatch_metric_alarm.error_logs
  to   = module.env.aws_cloudwatch_metric_alarm.error_logs
}

moved {
  from = aws_iam_role.canary
  to   = module.env.aws_iam_role.canary
}

moved {
  from = aws_iam_role_policy.canary
  to   = module.env.aws_iam_role_policy.canary
}

moved {
  from = aws_synthetics_canary.uptime
  to   = module.env.aws_synthetics_canary.uptime
}

moved {
  from = aws_cloudwatch_metric_alarm.uptime
  to   = module.env.aws_cloudwatch_metric_alarm.uptime
}

moved {
  from = aws_cloudwatch_query_definition.errors
  to   = module.env.aws_cloudwatch_query_definition.errors
}

moved {
  from = aws_cloudwatch_query_definition.request
  to   = module.env.aws_cloudwatch_query_definition.request
}

moved {
  from = aws_cloudwatch_query_definition.slow_routes
  to   = module.env.aws_cloudwatch_query_definition.slow_routes
}

moved {
  from = aws_cloudwatch_query_definition.extractions_by_tenant
  to   = module.env.aws_cloudwatch_query_definition.extractions_by_tenant
}

moved {
  from = aws_cloudwatch_query_definition.llm_spend
  to   = module.env.aws_cloudwatch_query_definition.llm_spend
}

moved {
  from = aws_cloudwatch_dashboard.main
  to   = module.env.aws_cloudwatch_dashboard.main
}

moved {
  from = module.canary_artifacts
  to   = module.env.module.canary_artifacts
}

moved {
  from = module.documents
  to   = module.env.module.documents
}
