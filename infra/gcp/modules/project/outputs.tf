output "alert_channels" { value = concat(google_monitoring_notification_channel.alert_email[*].id, google_monitoring_notification_channel.alert_webhook[*].id) }
output "tag_manager_role" { value = google_project_iam_custom_role.tag_manager.name }
output "cap_service_account" { value = local.cap_enabled ? google_service_account.cap[0].email : "" }
output "cap_workflow" { value = local.cap_enabled ? google_workflows_workflow.cap[0].name : "not configured" }
output "billing_dataset" { value = var.billing_export_dataset != "" ? google_bigquery_dataset.billing[0].dataset_id : "" }
output "budget" { value = var.monthly_budget_usd > 0 ? google_billing_budget.staging[0].name : "not configured" }
