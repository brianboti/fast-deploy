# The infrastructure pipeline's identities. GitHub Actions in the platform repository exchanges its OIDC token for one
# of two service accounts through two providers that pin, in the token's claims, what may run as each: the plan
# identity only from the plan workflow on a pull request, the apply identity only from the apply workflow on the main
# branch in the protected environment. Google's guide for deployment pipelines shows this ref and workflow pinning.
locals {
  pipeline_enabled = var.pipeline != null
  pipeline_repo    = local.pipeline_enabled ? var.pipeline.platform_repository : ""
  pipeline_ids     = local.pipeline_enabled ? "assertion.repository_id == '${var.pipeline.platform_repository_id}' && assertion.repository_owner_id == '${var.pipeline.platform_owner_id}'" : ""
}
resource "google_iam_workload_identity_pool" "platform" {
  count                     = local.pipeline_enabled ? 1 : 0
  workload_identity_pool_id = "lab-deploy-platform"
  display_name              = "Lab Deploy platform repository"
  depends_on                = [google_project_service.apis]
}
resource "google_iam_workload_identity_pool_provider" "plan" {
  count                              = local.pipeline_enabled ? 1 : 0
  workload_identity_pool_id          = google_iam_workload_identity_pool.platform[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github-plan"
  display_name                       = "Plan on pull requests"
  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.event_name"          = "assertion.event_name"
  }
  # A pull request's plan runs the plan workflow from the pull request's own ref, which is why only the file path is pinned.
  attribute_condition = "${local.pipeline_ids} && assertion.event_name == 'pull_request' && assertion.workflow_ref.startsWith('${local.pipeline_repo}/.github/workflows/infra-plan.yml@')"
  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }
}
resource "google_iam_workload_identity_pool_provider" "apply" {
  count                              = local.pipeline_enabled ? 1 : 0
  workload_identity_pool_id          = google_iam_workload_identity_pool.platform[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github-apply"
  display_name                       = "Apply from main in the protected environment"
  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.environment"         = "assertion.environment"
  }
  attribute_condition = "${local.pipeline_ids} && assertion.ref == 'refs/heads/main' && assertion.environment == '${var.pipeline.environment}' && assertion.workflow_ref == '${local.pipeline_repo}/.github/workflows/infra-apply.yml@refs/heads/main'"
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
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.platform[0].name}/attribute.event_name/pull_request"
  # Each provider maps an attribute the other does not (event_name here, environment below), so a token from one
  # provider can never become the other identity.
  depends_on = [google_iam_workload_identity_pool_provider.plan]
}
resource "google_service_account_iam_member" "infra_apply_federation" {
  count              = local.pipeline_enabled ? 1 : 0
  service_account_id = google_service_account.infra_apply[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.platform[0].name}/attribute.environment/${var.pipeline.environment}"
  depends_on         = [google_iam_workload_identity_pool_provider.apply]
}
# Plan: see what an application root reads, change nothing. No basic roles; state reads only under the apps/ prefix.
resource "google_project_iam_member" "infra_plan_roles" {
  for_each = local.pipeline_enabled ? toset([
    "roles/iam.securityReviewer", "roles/run.viewer", "roles/artifactregistry.reader", "roles/monitoring.viewer", "roles/pubsub.viewer",
    "roles/cloudsql.viewer", "roles/datastore.viewer", "roles/iam.workloadIdentityPoolViewer", "roles/serviceusage.serviceUsageViewer",
    "roles/compute.networkViewer", "roles/bigquery.metadataViewer", "roles/logging.viewer", "roles/storage.bucketViewer",
  ]) : toset([])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.infra_plan[0].email}"
}
resource "google_storage_bucket_iam_member" "infra_plan_state" {
  count  = local.pipeline_enabled ? 1 : 0
  bucket = var.state_bucket
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.infra_plan[0].email}"
  condition {
    title       = "application states and the foundation's outputs"
    description = "Plans read their own root's state and the foundation's outputs; nothing else in the bucket"
    # Listing is a bucket-level operation (names only), so the bucket itself is allowed; object reads stay under the two prefixes.
    expression = "resource.type == \"storage.googleapis.com/Bucket\" || resource.name.startsWith(\"projects/_/buckets/${var.state_bucket}/objects/apps/\") || resource.name.startsWith(\"projects/_/buckets/${var.state_bucket}/objects/project/\")"
  }
}
# Apply: exactly what an application root creates. No editor; buckets through a custom role instead of storage.admin.
# projectIamAdmin remains the powerful one, which is why the identity is reachable only from the apply workflow on main.
resource "google_project_iam_custom_role" "bucket_manager" {
  count       = local.pipeline_enabled ? 1 : 0
  role_id     = "labDeployBucketManager"
  title       = "Lab Deploy bucket manager"
  description = "Creates and configures application buckets and their access without reaching their contents"
  permissions = ["storage.buckets.create", "storage.buckets.get", "storage.buckets.list", "storage.buckets.update", "storage.buckets.getIamPolicy", "storage.buckets.setIamPolicy"]
}
resource "google_project_iam_member" "infra_apply_roles" {
  for_each = local.pipeline_enabled ? toset([
    "roles/run.admin", "roles/artifactregistry.admin", "roles/monitoring.admin", "roles/iam.serviceAccountAdmin", "roles/iam.serviceAccountUser",
    "roles/iam.workloadIdentityPoolAdmin", "roles/resourcemanager.projectIamAdmin", "roles/pubsub.admin", "roles/datastore.owner", "roles/cloudsql.admin",
    "roles/compute.networkViewer", "roles/serviceusage.serviceUsageConsumer", "roles/logging.viewer",
  ]) : toset([])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.infra_apply[0].email}"
}
resource "google_project_iam_member" "infra_apply_buckets" {
  count   = local.pipeline_enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.bucket_manager[0].name
  member  = "serviceAccount:${google_service_account.infra_apply[0].email}"
}
resource "google_storage_bucket_iam_member" "infra_apply_state" {
  count  = local.pipeline_enabled ? 1 : 0
  bucket = var.state_bucket
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.infra_apply[0].email}"
  condition {
    title       = "application states only"
    description = "The apply identity writes application states and reads the foundation's outputs; it never touches the foundation's state"
    expression  = "resource.type == \"storage.googleapis.com/Bucket\" || resource.name.startsWith(\"projects/_/buckets/${var.state_bucket}/objects/apps/\")"
  }
}
resource "google_storage_bucket_iam_member" "infra_apply_foundation_outputs" {
  count  = local.pipeline_enabled ? 1 : 0
  bucket = var.state_bucket
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.infra_apply[0].email}"
  condition {
    title       = "the foundation's outputs"
    description = "Read-only: application roots read the foundation's outputs through terraform_remote_state"
    expression  = "resource.name.startsWith(\"projects/_/buckets/${var.state_bucket}/objects/project/\")"
  }
}
resource "google_bigquery_dataset_iam_member" "infra_apply_billing" {
  count      = local.pipeline_enabled && var.billing_export_dataset != "" ? 1 : 0
  dataset_id = google_bigquery_dataset.billing[0].dataset_id
  role       = "roles/bigquery.dataOwner"
  member     = "serviceAccount:${google_service_account.infra_apply[0].email}"
}
