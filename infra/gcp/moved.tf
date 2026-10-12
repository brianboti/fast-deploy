# Migrates a state written by the single-module layout (versions up to 0.6.x) into the project and service modules.
# A move whose source is absent from the state is ignored, so this file is harmless for fresh installs.
# The pilot's only service was lab-demo; add equivalent blocks for other names before upgrading a state that has them.
moved {
  from = google_project_service.apis
  to   = module.project.google_project_service.apis
}
moved {
  from = google_project_service.container_scanning
  to   = module.project.google_project_service.container_scanning
}
moved {
  from = google_project_service.billing_budgets
  to   = module.project.google_project_service.billing_budgets
}
moved {
  from = google_monitoring_notification_channel.budget_email
  to   = module.project.google_monitoring_notification_channel.budget_email
}
moved {
  from = google_billing_budget.staging
  to   = module.project.google_billing_budget.staging
}
moved {
  from = google_project_iam_custom_role.tag_manager
  to   = module.project.google_project_iam_custom_role.tag_manager
}
moved {
  from = google_monitoring_notification_channel.alert_email
  to   = module.project.google_monitoring_notification_channel.alert_email
}
moved {
  from = google_monitoring_notification_channel.alert_webhook
  to   = module.project.google_monitoring_notification_channel.alert_webhook
}
moved {
  from = google_service_account.runtime
  to   = module.service["lab-demo"].google_service_account.runtime
}
moved {
  from = google_service_account.deployer
  to   = module.service["lab-demo"].google_service_account.deployer
}
moved {
  from = google_service_account.probe
  to   = module.service["lab-demo"].google_service_account.probe
}
moved {
  from = google_artifact_registry_repository.images
  to   = module.service["lab-demo"].google_artifact_registry_repository.images
}
moved {
  from = google_artifact_registry_repository_iam_member.writer
  to   = module.service["lab-demo"].google_artifact_registry_repository_iam_member.writer
}
moved {
  from = google_artifact_registry_repository_iam_member.tag_manager
  to   = module.service["lab-demo"].google_artifact_registry_repository_iam_member.tag_manager
}
moved {
  from = google_service_account_iam_member.act_as_runtime
  to   = module.service["lab-demo"].google_service_account_iam_member.act_as_runtime
}
moved {
  from = google_service_account_iam_member.probe_token
  to   = module.service["lab-demo"].google_service_account_iam_member.probe_token
}
moved {
  from = google_project_iam_member.vulnerability_reader
  to   = module.service["lab-demo"].google_project_iam_member.vulnerability_reader
}
moved {
  from = google_project_iam_member.metrics_reader
  to   = module.service["lab-demo"].google_project_iam_member.metrics_reader
}
moved {
  from = google_project_iam_member.recommendation_reader
  to   = module.service["lab-demo"].google_project_iam_member.recommendation_reader
}
moved {
  from = google_project_iam_member.log_reader
  to   = module.service["lab-demo"].google_project_iam_member.log_reader
}
moved {
  from = google_cloud_run_v2_service.app
  to   = module.service["lab-demo"].google_cloud_run_v2_service.app
}
moved {
  from = google_cloud_run_v2_service_iam_member.deployer
  to   = module.service["lab-demo"].google_cloud_run_v2_service_iam_member.deployer
}
moved {
  from = google_cloud_run_v2_service_iam_member.probe
  to   = module.service["lab-demo"].google_cloud_run_v2_service_iam_member.probe
}
moved {
  from = google_cloud_run_v2_service_iam_member.uptime_probe
  to   = module.service["lab-demo"].google_cloud_run_v2_service_iam_member.uptime_probe
}
moved {
  from = google_iam_workload_identity_pool.github
  to   = module.service["lab-demo"].google_iam_workload_identity_pool.github
}
moved {
  from = google_iam_workload_identity_pool_provider.github
  to   = module.service["lab-demo"].google_iam_workload_identity_pool_provider.github
}
moved {
  from = google_service_account_iam_member.federation
  to   = module.service["lab-demo"].google_service_account_iam_member.federation
}
moved {
  from = google_storage_bucket.recovery
  to   = module.service["lab-demo"].google_storage_bucket.recovery
}
moved {
  from = google_storage_bucket_iam_member.recovery
  to   = module.service["lab-demo"].google_storage_bucket_iam_member.recovery
}
moved {
  from = google_monitoring_uptime_check_config.health
  to   = module.service["lab-demo"].google_monitoring_uptime_check_config.health
}
moved {
  from = google_monitoring_alert_policy.health
  to   = module.service["lab-demo"].google_monitoring_alert_policy.health
}
moved {
  from = google_monitoring_alert_policy.errors
  to   = module.service["lab-demo"].google_monitoring_alert_policy.errors
}
moved {
  from = google_monitoring_alert_policy.latency
  to   = module.service["lab-demo"].google_monitoring_alert_policy.latency
}
moved {
  from = google_monitoring_alert_policy.saturation
  to   = module.service["lab-demo"].google_monitoring_alert_policy.saturation
}
moved {
  from = google_monitoring_alert_policy.memory
  to   = module.service["lab-demo"].google_monitoring_alert_policy.memory
}
moved {
  from = google_monitoring_alert_policy.log_errors
  to   = module.service["lab-demo"].google_monitoring_alert_policy.log_errors
}
moved {
  from = google_monitoring_dashboard.service
  to   = module.service["lab-demo"].google_monitoring_dashboard.service
}
