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
    IAP_CLIENT_ID          = var.iap_client_id
  }
}
output "service_url" { value = google_cloud_run_v2_service.app.uri }
output "deployer_email" { value = google_service_account.deployer.email }
output "console_url" { value = "https://console.cloud.google.com/run/detail/${var.region}/${var.service}/metrics?project=${var.project_id}" }
output "dashboard_url" {
  value = var.runtime_alerts ? "https://console.cloud.google.com/monitoring/dashboards/builder/${element(split("/", google_monitoring_dashboard.service[0].id), 3)}?project=${var.project_id}" : "not configured"
}
output "runtime_alerts" {
  value = var.runtime_alerts ? {
    uptime_check = google_monitoring_uptime_check_config.health[0].uptime_check_id
    policies     = ["health", "errors", "latency", "saturation", "memory", "log_errors"]
  } : null
}
