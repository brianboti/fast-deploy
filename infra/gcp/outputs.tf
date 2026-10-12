output "github_environment_variables" {
  value = {
    GCP_PROJECT_ID         = var.project_id
    GCP_REGION             = var.region
    CLOUD_RUN_SERVICE      = var.service
    ARTIFACT_REPOSITORY    = google_artifact_registry_repository.images.repository_id
    WIF_PROVIDER           = google_iam_workload_identity_pool_provider.github.name
    DEPLOY_SERVICE_ACCOUNT = google_service_account.deployer.email
    PROBE_SERVICE_ACCOUNT  = google_service_account.probe.email
    RECOVERY_BUCKET        = google_storage_bucket.recovery.name
  }
}
output "service_url" { value = google_cloud_run_v2_service.app.uri }
output "console_url" { value = "https://console.cloud.google.com/run/detail/${var.region}/${var.service}/metrics?project=${var.project_id}" }
output "dashboard_url" {
  value = var.runtime_alerts ? "https://console.cloud.google.com/monitoring/dashboards/builder/${element(split("/", google_monitoring_dashboard.service[0].id), 3)}?project=${var.project_id}" : "not configured"
}
output "runtime_alerts" {
  value = var.runtime_alerts ? {
    uptime_check          = google_monitoring_uptime_check_config.health[0].uptime_check_id
    notification_channels = length(local.alert_channels)
    policies              = ["health", "errors", "latency", "saturation", "memory", "log_errors"]
  } : null
}
output "cost_controls" {
  value = {
    registry_cleanup_dry_run = var.registry_cleanup_dry_run
    image_versions_kept      = var.image_versions_to_keep
    image_retention_days     = var.image_retention_days
    vulnerability_scanning   = var.image_vulnerability_scanning
    budget                   = var.monthly_budget_usd > 0 ? google_billing_budget.staging[0].name : "not configured"
  }
}
