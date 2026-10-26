# Recovery is opt-in after the supervised runner-loss drill. Its workflow builds nothing and runs no job, but this identity
# can update the service and its jobs (Cloud Run has no traffic-only permission): the workflow code and its WIF binding are
# the limit, not IAM.
variable "automatic_recovery_enabled" {
  type    = bool
  default = false
}

resource "google_service_account" "recovery" {
  count        = var.automatic_recovery_enabled ? 1 : 0
  account_id   = "${var.service}-recover"
  display_name = "Recovery only for ${var.service}"
  lifecycle {
    precondition {
      condition     = var.github_environment == "production-${var.service}"
      error_message = "Independent recovery must use a production service root."
    }
  }
}

resource "google_iam_workload_identity_pool" "recovery" {
  count                     = var.automatic_recovery_enabled ? 1 : 0
  workload_identity_pool_id = "${var.service}-recovery"
}

resource "google_iam_workload_identity_pool_provider" "recovery" {
  count                              = var.automatic_recovery_enabled ? 1 : 0
  workload_identity_pool_id          = google_iam_workload_identity_pool.recovery[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  attribute_mapping = {
    "google.subject"          = "assertion.sub"
    "attribute.repository_id" = "assertion.repository_id"
  }
  attribute_condition = "assertion.repository_id == '${var.github_repository_id}' && assertion.repository_owner_id == '${var.github_owner_id}' && assertion.ref == 'refs/heads/main' && assertion.environment == 'production-recovery-${var.service}' && assertion.workflow_ref == '${var.github_repository}/.github/workflows/recover-production.yml@refs/heads/main'"
  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }
}

resource "google_service_account_iam_member" "recovery_federation" {
  count              = var.automatic_recovery_enabled ? 1 : 0
  service_account_id = google_service_account.recovery[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.recovery[0].name}/attribute.repository_id/${var.github_repository_id}"
}

resource "google_project_iam_custom_role" "recovery" {
  count       = var.automatic_recovery_enabled ? 1 : 0
  role_id     = "recovery_${replace(var.service, "-", "_")}"
  title       = "Restore ${var.service}"
  permissions = ["run.services.get", "run.services.getIamPolicy", "run.services.update", "run.revisions.get", "run.revisions.list", "run.jobs.get", "run.jobs.update"]
}

resource "google_cloud_run_v2_service_iam_member" "recovery" {
  count    = var.automatic_recovery_enabled ? 1 : 0
  location = var.region
  name     = google_cloud_run_v2_service.app.name
  role     = google_project_iam_custom_role.recovery[0].name
  member   = "serviceAccount:${google_service_account.recovery[0].email}"
}

resource "google_cloud_run_v2_job_iam_member" "recovery" {
  for_each = var.automatic_recovery_enabled ? var.jobs : {}
  location = var.region
  name     = google_cloud_run_v2_job.job[each.key].name
  role     = google_project_iam_custom_role.recovery[0].name
  member   = "serviceAccount:${google_service_account.recovery[0].email}"
}

resource "google_service_account_iam_member" "recovery_runtime" {
  count              = var.automatic_recovery_enabled ? 1 : 0
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.recovery[0].email}"
}

resource "google_service_account_iam_member" "recovery_job" {
  for_each           = var.automatic_recovery_enabled ? var.jobs : {}
  service_account_id = google_service_account.job[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.recovery[0].email}"
}

resource "google_project_iam_custom_role" "recovery_operations" {
  count       = var.automatic_recovery_enabled ? 1 : 0
  role_id     = "recovery_ops_${replace(var.service, "-", "_")}"
  title       = "Read recovery operations for ${var.service}"
  permissions = ["run.operations.get"]
}

resource "google_project_iam_member" "recovery_operations" {
  count   = var.automatic_recovery_enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.recovery_operations[0].name
  member  = "serviceAccount:${google_service_account.recovery[0].email}"
}

resource "google_storage_bucket_iam_member" "recovery_worker" {
  count  = var.automatic_recovery_enabled ? 1 : 0
  bucket = google_storage_bucket.recovery.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.recovery[0].email}"
  condition {
    title      = "Release checkpoint only"
    expression = "resource.name == 'projects/_/buckets/${google_storage_bucket.recovery.name}/objects/releases/${var.service}.json'"
  }
}

output "recovery_environment_variables" {
  value = var.automatic_recovery_enabled ? {
    RECOVERY_WIF_PROVIDER      = google_iam_workload_identity_pool_provider.recovery[0].name
    RECOVERY_SERVICE_ACCOUNT   = google_service_account.recovery[0].email
    AUTOMATIC_RECOVERY_ENABLED = "true"
  } : {}
}
