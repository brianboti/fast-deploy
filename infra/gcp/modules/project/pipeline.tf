# The infrastructure pipeline's identities. GitHub Actions in the platform repository exchanges its OIDC token for one of
# two service accounts: plan (read-only, any workflow of the repository) and apply (write, only jobs that run in the
# protected GitHub environment, which the operator restricts to the main branch with required reviewers).
locals {
  pipeline_enabled = var.pipeline != null
}
resource "google_iam_workload_identity_pool" "platform" {
  count                     = local.pipeline_enabled ? 1 : 0
  workload_identity_pool_id = "lab-deploy-platform"
  display_name              = "Lab Deploy platform repository"
  depends_on                = [google_project_service.apis]
}
resource "google_iam_workload_identity_pool_provider" "platform" {
  count                              = local.pipeline_enabled ? 1 : 0
  workload_identity_pool_id          = google_iam_workload_identity_pool.platform[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.environment"         = "assertion.environment"
    "attribute.ref"                 = "assertion.ref"
  }
  attribute_condition = "assertion.repository_id == '${var.pipeline.platform_repository_id}' && assertion.repository_owner_id == '${var.pipeline.platform_owner_id}'"
  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }
}
resource "google_service_account" "infra_plan" {
  count        = local.pipeline_enabled ? 1 : 0
  account_id   = "lab-deploy-infra-plan"
  display_name = "Lab Deploy infrastructure pipeline: plan (read-only)"
  depends_on   = [google_project_service.apis]
}
resource "google_service_account" "infra_apply" {
  count        = local.pipeline_enabled ? 1 : 0
  account_id   = "lab-deploy-infra-apply"
  display_name = "Lab Deploy infrastructure pipeline: apply"
  depends_on   = [google_project_service.apis]
}
resource "google_service_account_iam_member" "infra_plan_federation" {
  count              = local.pipeline_enabled ? 1 : 0
  service_account_id = google_service_account.infra_plan[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.platform[0].name}/attribute.repository_id/${var.pipeline.platform_repository_id}"
}
resource "google_service_account_iam_member" "infra_apply_federation" {
  count              = local.pipeline_enabled ? 1 : 0
  service_account_id = google_service_account.infra_apply[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.platform[0].name}/attribute.environment/${var.pipeline.environment}"
}
# Plan: see everything, change nothing. Reading IAM policies is what makes a plan of IAM resources possible.
resource "google_project_iam_member" "infra_plan_roles" {
  for_each = local.pipeline_enabled ? toset(["roles/viewer", "roles/iam.securityReviewer"]) : toset([])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.infra_plan[0].email}"
}
resource "google_storage_bucket_iam_member" "infra_plan_state" {
  count  = local.pipeline_enabled ? 1 : 0
  bucket = var.state_bucket
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.infra_plan[0].email}"
}
# Apply: what an application root creates (service, registry, bucket, identities, federation, grants, alerts).
# projectIamAdmin is the powerful one: whoever can run the apply job can change this project's IAM, which is why the
# apply job only runs from the protected environment. Keep the staging project dedicated to the platform.
resource "google_project_iam_member" "infra_apply_roles" {
  for_each = local.pipeline_enabled ? toset([
    "roles/editor", "roles/run.admin", "roles/artifactregistry.admin", "roles/storage.admin", "roles/monitoring.admin",
    "roles/iam.serviceAccountAdmin", "roles/iam.serviceAccountUser", "roles/iam.workloadIdentityPoolAdmin",
    "roles/resourcemanager.projectIamAdmin",
  ]) : toset([])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.infra_apply[0].email}"
}
resource "google_bigquery_dataset_iam_member" "infra_apply_billing" {
  count      = local.pipeline_enabled && var.billing_export_dataset != "" ? 1 : 0
  dataset_id = google_bigquery_dataset.billing[0].dataset_id
  role       = "roles/bigquery.dataOwner"
  member     = "serviceAccount:${google_service_account.infra_apply[0].email}"
}
