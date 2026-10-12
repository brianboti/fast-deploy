terraform {
  required_version = ">= 1.6, < 2.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "current" {}

resource "google_project_service" "apis" {
  for_each           = toset(["run.googleapis.com", "artifactregistry.googleapis.com", "iam.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com", "storage.googleapis.com", "monitoring.googleapis.com", "recommender.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "runtime" {
  account_id   = "${var.service}-runtime"
  display_name = "Unprivileged Cloud Run runtime"
  depends_on   = [google_project_service.apis]
}
resource "google_service_account" "deployer" {
  account_id = "${var.service}-deploy"
  depends_on = [google_project_service.apis]
}
resource "google_service_account" "probe" {
  account_id = "${var.service}-probe"
  depends_on = [google_project_service.apis]
}
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = var.service
  format        = "DOCKER"
  # Cost control: old images are pure storage cost because releases pin digests and never redeploy old tags.
  # KEEP wins over DELETE, so the newest versions always survive as rollback targets. Dry run logs instead of deleting.
  cleanup_policy_dry_run = var.registry_cleanup_dry_run
  cleanup_policies {
    id     = "keep-recent-releases"
    action = "KEEP"
    most_recent_versions {
      keep_count = var.image_versions_to_keep
    }
  }
  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s"
    }
  }
  cleanup_policies {
    id     = "delete-stale-releases"
    action = "DELETE"
    condition {
      tag_state  = "TAGGED"
      older_than = "${var.image_retention_days * 86400}s"
    }
  }
  depends_on = [google_project_service.apis]
}
resource "google_artifact_registry_repository_iam_member" "writer" {
  location   = var.region
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.deployer.email}"
}
resource "google_service_account_iam_member" "act_as_runtime" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}
resource "google_service_account_iam_member" "probe_token" {
  service_account_id = google_service_account.probe.name
  role               = "roles/iam.serviceAccountOpenIdTokenCreator"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}
# Automatic vulnerability scanning of every pushed image digest (Artifact Analysis). The deployer reads the results.
resource "google_project_service" "container_scanning" {
  count              = var.image_vulnerability_scanning ? 1 : 0
  service            = "containerscanning.googleapis.com"
  disable_on_destroy = false
}
resource "google_project_iam_member" "vulnerability_reader" {
  count   = var.image_vulnerability_scanning ? 1 : 0
  project = var.project_id
  role    = "roles/containeranalysis.occurrences.viewer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}
# Right-sizing reads usage metrics and Google's Cloud Run cost recommendations. Both are project-level,
# read-only grants: Cloud Monitoring has no per-service IAM, and recommendations live under the project.
resource "google_project_iam_member" "metrics_reader" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}
resource "google_project_iam_member" "recommendation_reader" {
  project = var.project_id
  role    = "roles/run.viewer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_cloud_run_v2_service" "app" {
  name                = var.service
  location            = var.region
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_ALL"
  template {
    service_account = google_service_account.runtime.email
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    containers {
      image = var.bootstrap_image
      ports { container_port = 8080 }
      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = true
      }
    }
  }
  # CI owns subsequent workload revisions and traffic. Terraform owns IAM/foundation.
  lifecycle {
    ignore_changes = [template, traffic, client, client_version]
  }
  depends_on = [google_project_service.apis]
}
resource "google_cloud_run_v2_service_iam_member" "deployer" {
  location = var.region
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.deployer.email}"
}
resource "google_cloud_run_v2_service_iam_member" "probe" {
  location = var.region
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.probe.email}"
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "${var.service}-gh"
  depends_on                = [google_project_service.apis]
}
resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
  }
  attribute_condition = "assertion.repository_id == '${var.github_repository_id}' && assertion.repository_owner_id == '${var.github_owner_id}' && assertion.ref == 'refs/heads/main' && assertion.sub == 'repo:${var.github_repository}:environment:staging' && assertion.workflow_ref in ['${var.github_repository}/.github/workflows/deploy-gcp.yml@refs/heads/main', '${var.github_repository}/.github/workflows/recover-gcp.yml@refs/heads/main', '${var.github_repository}/.github/workflows/rightsize-gcp.yml@refs/heads/main']"
  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }
}
resource "google_service_account_iam_member" "federation" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${var.github_repository_id}"
}

resource "google_storage_bucket" "recovery" {
  name                        = "${var.project_id}-${var.service}-recovery"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  # Cost control: only the newest checkpoint versions matter for recovery; older noncurrent versions are deleted.
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      with_state         = "ARCHIVED"
      num_newer_versions = 10
    }
  }
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      with_state                 = "ARCHIVED"
      days_since_noncurrent_time = 30
    }
  }
  lifecycle {
    prevent_destroy = true
  }
  depends_on = [google_project_service.apis]
}

# Optional budget alert for the whole staging project. Alerts notify; they do not stop spending.
resource "google_project_service" "billing_budgets" {
  count              = var.monthly_budget_usd > 0 ? 1 : 0
  service            = "billingbudgets.googleapis.com"
  disable_on_destroy = false
}
resource "google_monitoring_notification_channel" "budget_email" {
  count        = var.monthly_budget_usd > 0 && var.budget_alert_email != "" ? 1 : 0
  display_name = "${var.service} staging budget alerts"
  type         = "email"
  labels       = { email_address = var.budget_alert_email }
  depends_on   = [google_project_service.apis]
}
resource "google_billing_budget" "staging" {
  count           = var.monthly_budget_usd > 0 ? 1 : 0
  billing_account = var.billing_account
  display_name    = "${var.project_id} staging monthly budget"
  budget_filter {
    projects        = ["projects/${data.google_project.current.number}"]
    calendar_period = "MONTH"
  }
  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.monthly_budget_usd)
    }
  }
  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }
  dynamic "all_updates_rule" {
    for_each = var.budget_alert_email != "" ? [1] : []
    content {
      monitoring_notification_channels = [google_monitoring_notification_channel.budget_email[0].name]
      disable_default_iam_recipients   = false
    }
  }
  lifecycle {
    precondition {
      condition     = var.billing_account != ""
      error_message = "Set billing_account when monthly_budget_usd is greater than 0."
    }
  }
  depends_on = [google_project_service.billing_budgets]
}
resource "google_storage_bucket_iam_member" "recovery" {
  bucket = google_storage_bucket.recovery.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.deployer.email}"
}

