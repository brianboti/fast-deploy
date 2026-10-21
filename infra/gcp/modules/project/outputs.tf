output "alert_channels" { value = concat(google_monitoring_notification_channel.alert_email[*].id, google_monitoring_notification_channel.alert_webhook[*].id) }
output "tag_manager_role" { value = google_project_iam_custom_role.tag_manager.name }
output "model_user_role" { value = google_project_iam_custom_role.model_user.name }
output "cap_service_account" { value = local.cap_enabled ? google_service_account.cap[0].email : "" }
output "backup_bucket" { value = google_storage_bucket.backups.name }
output "cap_operator_role" { value = local.cap_enabled ? google_project_iam_custom_role.cap_operator[0].name : "" }
output "cap_workflow" { value = local.cap_enabled ? google_workflows_workflow.cap[0].name : "not configured" }
output "billing_dataset" { value = var.billing_export_dataset != "" ? google_bigquery_dataset.billing[0].dataset_id : "" }
output "observer_service_account" { value = google_service_account.observer.email }
output "budget" { value = var.monthly_budget_usd > 0 ? google_billing_budget.staging[0].name : "not configured" }
output "pipeline" {
  value = local.pipeline_enabled ? {
    plan_provider         = google_iam_workload_identity_pool_provider.plan[0].name
    apply_provider        = google_iam_workload_identity_pool_provider.apply[0].name
    plan_service_account  = google_service_account.infra_plan[0].email
    apply_service_account = google_service_account.infra_apply[0].email
    environment           = var.pipeline.environment
  } : null
}
output "network" { value = google_compute_network.lab.id }
output "subnet" { value = google_compute_subnetwork.run.id }
output "kms_key_ring" { value = google_kms_key_ring.provenance.id }
output "controller" {
  value = local.controller_enabled ? {
    name                  = google_compute_instance.controller[0].name
    zone                  = google_compute_instance.controller[0].zone
    address               = google_compute_address.controller[0].address
    host                  = local.controller_host
    url                   = "https://${local.controller_host}/mcp"
    service_account       = google_service_account.controller[0].email
    github_app_key_secret = google_secret_manager_secret.github_app_key[0].id
  } : null
}
# Binary Authorization (binauthz.tf): what an application's root needs to opt its service in and let its deployer attest.
output "binary_authorization" {
  value = {
    mode          = var.binary_authorization
    attestor      = google_binary_authorization_attestor.release.id
    key           = google_kms_crypto_key.release_attestor.id
    key_version   = trimprefix(data.google_kms_crypto_key_version.release_attestor.id, "//cloudkms.googleapis.com/v1/")
    attester_role = google_project_iam_custom_role.release_attester.name
  }
}
