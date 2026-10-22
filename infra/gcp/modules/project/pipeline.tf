# The infrastructure pipeline's identities. GitHub Actions in the platform repository exchanges its OIDC token for one
# of two service accounts through two providers that pin, in the token's claims, what may run as each: the plan
# identity only from the plan workflow on a pull request or from the weekly drift workflow on main (scheduled or run by
# hand), the apply identity only from the apply workflow on the main branch in the protected environment. Google's guide
# for deployment pipelines shows this ref and workflow pinning.
locals {
  pipeline_enabled = var.pipeline != null
  pipeline_ids     = local.pipeline_enabled ? "assertion.repository_id == '${var.pipeline.platform_repository_id}' && assertion.repository_owner_id == '${var.pipeline.platform_owner_id}'" : ""
  # The workflow paths carry the repository's name: its name now, and any it had before a rename (GitHub keeps the ids and
  # redirects the old name), so the pipeline keeps signing in while everything moves to the new name. The ids are the anchor.
  pipeline_names  = local.pipeline_enabled ? concat([var.pipeline.platform_repository], var.pipeline.former_repositories) : []
  plan_workflows  = join(" || ", [for name in local.pipeline_names : "assertion.workflow_ref.startsWith('${name}/.github/workflows/infra-plan.yml@')"])
  drift_workflows = join(", ", [for name in local.pipeline_names : "'${name}/.github/workflows/infra-drift.yml@refs/heads/main'"])
  apply_workflows = join(", ", [for name in local.pipeline_names : "'${name}/.github/workflows/infra-apply.yml@refs/heads/main'"])
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
  display_name                       = "Plan: pull requests and drift" # at most 32 characters
  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.event_name"          = "assertion.event_name"
  }
  # A pull request's plan runs the plan workflow from the pull request's own ref, which is why only the file path is pinned.
  # The drift check runs from main only, so its workflow is pinned with the ref.
  attribute_condition = "${local.pipeline_ids} && ((assertion.event_name == 'pull_request' && (${local.plan_workflows})) || (assertion.event_name in ['schedule', 'workflow_dispatch'] && assertion.ref == 'refs/heads/main' && assertion.workflow_ref in [${local.drift_workflows}]))"
  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }
}
resource "google_iam_workload_identity_pool_provider" "apply" {
  count                              = local.pipeline_enabled ? 1 : 0
  workload_identity_pool_id          = google_iam_workload_identity_pool.platform[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github-apply"
  display_name                       = "Apply from main in infra" # at most 32 characters
  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.environment"         = "assertion.environment"
  }
  attribute_condition = "${local.pipeline_ids} && assertion.ref == 'refs/heads/main' && assertion.environment == '${var.pipeline.environment}' && assertion.workflow_ref in [${local.apply_workflows}]"
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
# "Check this project" in the controller's Connections screen (docs/CONNECTIONS.md) reads these two accounts as the
# controller itself, to see that this project has a foundation. The machine holds only the observer's viewer roles, and
# none of them reads a service account, so every check was refused until 1.31.8. Only this one permission (no keys, no
# policies), only on these two accounts, for the machine of this project and for viewer_members (the machine of another
# project that may choose this one; the observer's bucketViewer there already covers the state bucket's metadata).
resource "google_project_iam_custom_role" "foundation_reader" {
  count       = local.pipeline_enabled ? 1 : 0
  role_id     = "labDeployFoundationReader"
  title       = "Lab Deploy foundation reader"
  description = "Reads the infrastructure pipeline's two service accounts, so the controller can see that the foundation exists; nothing else"
  permissions = ["iam.serviceAccounts.get"]
}
locals {
  # Keyed by a name known at plan time: the machine's own email is not known before its first apply.
  foundation_readers = merge({ for member in var.viewer_members : member => member },
  local.controller_enabled ? { controller = "serviceAccount:${google_service_account.controller[0].email}" } : {})
}
resource "google_service_account_iam_member" "foundation_readers" {
  for_each           = local.pipeline_enabled ? { for pair in setproduct(keys(local.foundation_readers), ["plan", "apply"]) : "${pair[0]} ${pair[1]}" => pair } : {}
  service_account_id = each.value[1] == "plan" ? google_service_account.infra_plan[0].name : google_service_account.infra_apply[0].name
  role               = google_project_iam_custom_role.foundation_reader[0].name
  member             = local.foundation_readers[each.value[0]]
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
# The weekly drift check (and its manual run) plans as the same read-only identity; the provider above admits those events
# only for the drift workflow on main.
resource "google_service_account_iam_member" "infra_plan_drift_federation" {
  for_each           = local.pipeline_enabled ? toset(["schedule", "workflow_dispatch"]) : toset([])
  service_account_id = google_service_account.infra_plan[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.platform[0].name}/attribute.event_name/${each.value}"
  depends_on         = [google_iam_workload_identity_pool_provider.plan]
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
    # Secret metadata only (never a value): an application root with database_auth = password plans its DATABASE_URL secret.
    "roles/secretmanager.viewer",
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
# A log-based alert policy (the service module's "requests refused") is stored as a Cloud Logging notification rule, and
# production's objective counts users' requests with two log-based metrics (slo.tf, docs/ROADMAP.md D58); monitoring.admin
# covers neither. Only these ten permissions: no sinks, views or exclusions (roles/logging.configWriter). The metric
# permissions were missing until 1.29.1: production's first apply of 1.26.0's objective was refused for them.
resource "google_project_iam_custom_role" "log_alert_writer" {
  count       = local.pipeline_enabled ? 1 : 0
  role_id     = "labDeployLogAlertWriter"
  title       = "Lab Deploy log alert writer"
  description = "Creates and maintains the notification rules behind log-based alert policies and the log-based metrics an objective counts"
  permissions = ["logging.notificationRules.create", "logging.notificationRules.delete", "logging.notificationRules.get", "logging.notificationRules.list", "logging.notificationRules.update",
  "logging.logMetrics.create", "logging.logMetrics.delete", "logging.logMetrics.get", "logging.logMetrics.list", "logging.logMetrics.update"]
}
resource "google_project_iam_member" "infra_apply_log_alerts" {
  count   = local.pipeline_enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.log_alert_writer[0].name
  member  = "serviceAccount:${google_service_account.infra_apply[0].email}"
}
# Pages for people (exposure: lab-members): an application root lets its viewers, its release probe and its wake identity
# through Identity-Aware Proxy on its own service, which reads and writes the proxy's policy on that service. run.admin and
# projectIamAdmin cover neither, so the first lab-members apply in staging was refused (2026-10-21). Only these two: no
# proxy settings (the OAuth client stays the operator's, docs/SETUP.md 7h), unlike roles/iap.admin.
resource "google_project_iam_custom_role" "iap_access_manager" {
  count       = local.pipeline_enabled ? 1 : 0
  role_id     = "labDeployIapAccessManager"
  title       = "Lab Deploy IAP access manager"
  description = "Lets an application root say who may pass Identity-Aware Proxy on its own Cloud Run service"
  permissions = ["iap.webServices.getIamPolicy", "iap.webServices.setIamPolicy"]
}
resource "google_project_iam_member" "infra_apply_iap_access" {
  count   = local.pipeline_enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.iap_access_manager[0].name
  member  = "serviceAccount:${google_service_account.infra_apply[0].email}"
}
# A custom domain in production (docs/ROADMAP.md 16.6, the service module's domain.tf): the apply identity builds the load
# balancer (loadBalancerAdmin reads instances and networks but changes neither) and its Cloud Armor policy (only the
# policy permissions, not securityAdmin's firewall rules); the plan identity reads the two kinds of resource that
# networkViewer leaves out. Staging has no domains, so its foundation is unchanged.
locals {
  domains_enabled = local.pipeline_enabled && var.environment == "production"
}
resource "google_project_iam_custom_role" "armor_manager" {
  count       = local.domains_enabled ? 1 : 0
  role_id     = "labDeployArmorManager"
  title       = "Lab Deploy Cloud Armor manager"
  description = "Creates and changes the Cloud Armor policies in front of application domains"
  permissions = ["compute.securityPolicies.create", "compute.securityPolicies.delete", "compute.securityPolicies.get", "compute.securityPolicies.list",
  "compute.securityPolicies.setLabels", "compute.securityPolicies.update", "compute.securityPolicies.use"]
}
resource "google_project_iam_member" "infra_apply_balancers" {
  count   = local.domains_enabled ? 1 : 0
  project = var.project_id
  role    = "roles/compute.loadBalancerAdmin"
  member  = "serviceAccount:${google_service_account.infra_apply[0].email}"
}
resource "google_project_iam_member" "infra_apply_armor" {
  count   = local.domains_enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.armor_manager[0].name
  member  = "serviceAccount:${google_service_account.infra_apply[0].email}"
}
resource "google_project_iam_custom_role" "balancer_viewer" {
  count       = local.domains_enabled ? 1 : 0
  role_id     = "labDeployBalancerViewer"
  title       = "Lab Deploy load balancer viewer"
  description = "Reads the serverless endpoint groups and Cloud Armor policies of application domains"
  permissions = ["compute.regionNetworkEndpointGroups.get", "compute.regionNetworkEndpointGroups.list", "compute.securityPolicies.get", "compute.securityPolicies.list"]
}
resource "google_project_iam_member" "infra_plan_balancers" {
  count   = local.domains_enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.balancer_viewer[0].name
  member  = "serviceAccount:${google_service_account.infra_plan[0].email}"
}
# An application root with database_auth = "password" creates its DATABASE_URL secret and adds the first version through a
# write-only argument. Creating and describing secrets and versions only: secretmanager.versions.access is not here, so the
# pipeline can never read a value back (the provider does not read a write-only version's payload either).
resource "google_project_iam_custom_role" "secret_writer" {
  count       = local.pipeline_enabled ? 1 : 0
  role_id     = "labDeploySecretWriter"
  title       = "Lab Deploy secret writer"
  description = "Creates application secrets and adds versions without reading any secret value"
  # The provider enables a version right after adding it, and replaces one (a rotation, or a retry after a failed create)
  # by destroying the old one: enable, disable and destroy change a version's state, never reveal its value.
  permissions = ["secretmanager.secrets.create", "secretmanager.secrets.get", "secretmanager.secrets.list", "secretmanager.secrets.update",
    "secretmanager.versions.add", "secretmanager.versions.get", "secretmanager.versions.list", "secretmanager.versions.enable",
  "secretmanager.versions.disable", "secretmanager.versions.destroy"]
}
resource "google_project_iam_member" "infra_apply_secrets" {
  count   = local.pipeline_enabled ? 1 : 0
  project = var.project_id
  role    = google_project_iam_custom_role.secret_writer[0].name
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
