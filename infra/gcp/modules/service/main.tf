# One application: its identities, registry, Cloud Run service, GitHub trust, recovery bucket and runtime alerts.
locals {
  service_filter = "resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${var.service}\""
  # Marks what the platform manages: the spending cap closes services with lab-deploy-managed, and the billing export
  # groups cost by lab-deploy-app.
  labels = { lab-deploy-managed = "true", lab-deploy-app = var.service }
}

resource "google_service_account" "runtime" {
  account_id   = "${var.service}-runtime"
  display_name = "Unprivileged Cloud Run runtime"
}
resource "google_service_account" "deployer" {
  account_id = "${var.service}-deploy"
}
resource "google_service_account" "probe" {
  account_id = "${var.service}-probe"
}
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = var.service
  format        = "DOCKER"
  labels        = local.labels
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
  # The release and right-sizing jobs tag every serving revision's digest serving-<revision>, so an image that still
  # serves traffic, including one a rollback returned to, is never deleted whatever its age.
  cleanup_policies {
    id     = "keep-serving-images"
    action = "KEEP"
    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["serving-"]
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
}
resource "google_artifact_registry_repository_iam_member" "writer" {
  location   = var.region
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.deployer.email}"
}
resource "google_artifact_registry_repository_iam_member" "tag_manager" {
  location   = var.region
  repository = google_artifact_registry_repository.images.name
  role       = var.tag_manager_role
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
# A failed candidate's own last log lines go into the release receipt; reading logs is project-level and read-only.
resource "google_project_iam_member" "log_reader" {
  project = var.project_id
  role    = "roles/logging.viewer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}
# Secrets: platform.yaml references names; the values live in Secret Manager as <service>-NAME. The runtime identity may
# read only those, and the deployer may only check that they exist. Both grants are limited by name through an IAM condition.
resource "google_project_iam_member" "secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.runtime.email}"
  condition {
    title       = "${var.service} secrets only"
    description = "Secrets named ${var.service}-NAME, as platform.yaml references them"
    expression  = "resource.name.startsWith(\"projects/${var.project_number}/secrets/${var.service}-\")"
  }
}
resource "google_project_iam_member" "secret_viewer" {
  project = var.project_id
  role    = "roles/secretmanager.viewer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
  condition {
    title       = "${var.service} secrets only"
    description = "Lets the deployer confirm a referenced secret exists before deploying"
    expression  = "resource.name.startsWith(\"projects/${var.project_number}/secrets/${var.service}-\")"
  }
}
# Actual spend from the billing export, read by the weekly cost report.
resource "google_project_iam_member" "billing_query" {
  count   = var.billing_dataset != "" ? 1 : 0
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}
resource "google_bigquery_dataset_iam_member" "billing_reader" {
  count      = var.billing_dataset != "" ? 1 : 0
  project    = var.project_id
  dataset_id = var.billing_dataset
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_cloud_run_v2_service" "app" {
  name                = var.service
  location            = var.region
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels              = local.labels # releases merge their own labels in (--update-labels), so these survive
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
    # ingress too: the spending cap closes it and the operator reopens it; a later apply must not undo either.
    ignore_changes = [template, traffic, client, client_version, ingress]
  }
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
# The spending-cap workflow closes this service's ingress when the budget line is crossed.
resource "google_cloud_run_v2_service_iam_member" "cap" {
  count    = var.cap_service_account != "" ? 1 : 0
  location = var.region
  name     = google_cloud_run_v2_service.app.name
  role     = var.cap_operator_role
  member   = "serviceAccount:${var.cap_service_account}"
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "${var.service}-gh"
}
# The subject claim's format changed in 2026 to embed numeric IDs (repo:OWNER@ID/NAME@ID:environment:ENV), so the
# condition matches the dedicated environment claim plus immutable IDs, the branch and the exact workflow files.
resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
  }
  attribute_condition = "assertion.repository_id == '${var.github_repository_id}' && assertion.repository_owner_id == '${var.github_owner_id}' && assertion.ref == 'refs/heads/main' && assertion.environment == 'staging' && assertion.workflow_ref in ['${var.github_repository}/.github/workflows/deploy-gcp.yml@refs/heads/main', '${var.github_repository}/.github/workflows/recover-gcp.yml@refs/heads/main', '${var.github_repository}/.github/workflows/rightsize-gcp.yml@refs/heads/main']"
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
  labels                      = local.labels
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
}
resource "google_storage_bucket_iam_member" "recovery" {
  bucket = google_storage_bucket.recovery.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.deployer.email}"
}

# ---------------------------------------------------------------------------------------------------------------------
# Runtime watch: what tells you about trouble after a release has been promoted. Everything here stays within Google's
# free monitoring allotment at lab traffic. Set runtime_alerts = false to create none of it.
# ---------------------------------------------------------------------------------------------------------------------
# The uptime check signs its requests as the Monitoring service agent, so that agent needs invoker on this service.
# If the agent does not exist yet in a new project: gcloud beta services identity create --service=monitoring.googleapis.com
resource "google_cloud_run_v2_service_iam_member" "uptime_probe" {
  count    = var.runtime_alerts ? 1 : 0
  location = var.region
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${var.project_number}@gcp-sa-monitoring-notification.iam.gserviceaccount.com"
}
resource "google_monitoring_uptime_check_config" "health" {
  count        = var.runtime_alerts ? 1 : 0
  display_name = "${var.service} health"
  timeout      = "10s"
  period       = "300s"
  http_check {
    path           = var.health_path
    port           = 443
    use_ssl        = true # certificate validation (validate_ssl) is only offered for plain-URL checks, not Cloud Run targets
    request_method = "GET"
    service_agent_authentication {
      type = "OIDC_TOKEN"
    }
  }
  # Service-agent (OIDC) authentication is only offered for Cloud Run targets, not arbitrary URLs: the check must name
  # the service as a cloud_run_revision resource. Google's own example passes project_id, location and service_name.
  monitored_resource {
    type = "cloud_run_revision"
    # The API stores revision_name and configuration_name as empty labels; naming them keeps every later plan clean.
    labels = { project_id = var.project_id, location = var.region, service_name = google_cloud_run_v2_service.app.name, revision_name = "", configuration_name = "" }
  }
  depends_on = [google_cloud_run_v2_service_iam_member.uptime_probe]
}
resource "google_monitoring_alert_policy" "health" {
  count        = var.runtime_alerts ? 1 : 0
  display_name = "${var.service}: health check failing"
  combiner     = "OR"
  conditions {
    display_name = "uptime check failed from more than one region"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"cloud_run_revision\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.health[0].uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "300s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.*"]
      }
      trigger {
        count = 1
      }
    }
  }
  notification_channels = var.alert_channels
  alert_strategy {
    auto_close = "1800s"
  }
}
resource "google_monitoring_alert_policy" "errors" {
  count        = var.runtime_alerts ? 1 : 0
  display_name = "${var.service}: server errors"
  combiner     = "OR"
  conditions {
    display_name = "more than ${var.error_alert_count} 5xx responses in five minutes"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/request_count\" AND ${local.service_filter} AND metric.label.response_code_class=\"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.error_alert_count
      duration        = "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
      trigger {
        count = 1
      }
    }
  }
  notification_channels = var.alert_channels
  alert_strategy {
    auto_close = "1800s"
  }
}
resource "google_monitoring_alert_policy" "latency" {
  count        = var.runtime_alerts ? 1 : 0
  display_name = "${var.service}: slow responses"
  combiner     = "OR"
  conditions {
    display_name = "p95 latency above ${var.latency_alert_ms} ms for five minutes"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/request_latencies\" AND ${local.service_filter}"
      comparison      = "COMPARISON_GT"
      threshold_value = var.latency_alert_ms
      duration        = "300s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_95"
        cross_series_reducer = "REDUCE_MAX"
        group_by_fields      = ["resource.label.service_name"]
      }
      trigger {
        count = 1
      }
    }
  }
  notification_channels = var.alert_channels
  alert_strategy {
    auto_close = "1800s"
  }
}
resource "google_monitoring_alert_policy" "saturation" {
  count        = var.runtime_alerts ? 1 : 0
  display_name = "${var.service}: at maximum instances"
  combiner     = "OR"
  conditions {
    display_name = "${var.instance_alert_count} or more active instances for five minutes"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/container/instance_count\" AND ${local.service_filter} AND metric.label.state=\"active\""
      comparison      = "COMPARISON_GT" # the API only offers GT and LT; instance counts are whole numbers, so GT n-1 means "n or more"
      threshold_value = var.instance_alert_count - 1
      duration        = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
      trigger {
        count = 1
      }
    }
  }
  notification_channels = var.alert_channels
  alert_strategy {
    auto_close = "1800s"
  }
}
resource "google_monitoring_alert_policy" "memory" {
  count        = var.runtime_alerts ? 1 : 0
  display_name = "${var.service}: memory above 80%"
  combiner     = "OR"
  conditions {
    display_name = "p95 memory utilization above 80% for five minutes"
    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/container/memory/utilizations\" AND ${local.service_filter}"
      comparison      = "COMPARISON_GT"
      threshold_value = 0.8
      duration        = "300s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_95"
        cross_series_reducer = "REDUCE_MAX"
        group_by_fields      = ["resource.label.service_name"]
      }
      trigger {
        count = 1
      }
    }
  }
  notification_channels = var.alert_channels
  alert_strategy {
    auto_close = "1800s"
  }
}
resource "google_monitoring_alert_policy" "log_errors" {
  count        = var.runtime_alerts ? 1 : 0
  display_name = "${var.service}: errors in the application log"
  combiner     = "OR"
  conditions {
    display_name = "a log line of severity ERROR or worse"
    condition_matched_log {
      filter = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${var.service}\" AND severity>=ERROR"
    }
  }
  notification_channels = var.alert_channels
  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close = "1800s"
  }
}
resource "google_monitoring_dashboard" "service" {
  count = var.runtime_alerts ? 1 : 0
  dashboard_json = jsonencode({
    displayName = "${var.service} staging"
    mosaicLayout = {
      columns = 12
      tiles = [
        for index, tile in [
          { title = "Requests per minute by response class", metric = "run.googleapis.com/request_count", aligner = "ALIGN_RATE", reducer = "REDUCE_SUM", groups = ["metric.label.response_code_class"] },
          { title = "Request latency p95 (ms)", metric = "run.googleapis.com/request_latencies", aligner = "ALIGN_PERCENTILE_95", reducer = "REDUCE_MAX", groups = [] },
          { title = "Active instances", metric = "run.googleapis.com/container/instance_count", aligner = "ALIGN_MAX", reducer = "REDUCE_SUM", groups = ["metric.label.state"] },
          { title = "Memory utilization p95", metric = "run.googleapis.com/container/memory/utilizations", aligner = "ALIGN_PERCENTILE_95", reducer = "REDUCE_MAX", groups = [] },
          { title = "CPU utilization p95", metric = "run.googleapis.com/container/cpu/utilizations", aligner = "ALIGN_PERCENTILE_95", reducer = "REDUCE_MAX", groups = [] },
          { title = "Billable instance time (s/min)", metric = "run.googleapis.com/container/billable_instance_time", aligner = "ALIGN_RATE", reducer = "REDUCE_SUM", groups = [] },
          # Written in the form the API stores (no zero positions, no empty group list, explicit axis) so plans stay clean.
          ] : merge(
          index % 2 == 0 ? {} : { xPos = (index % 2) * 6 },
          index < 2 ? {} : { yPos = floor(index / 2) * 4 },
          {
            width  = 6
            height = 4
            widget = {
              title = tile.title
              xyChart = {
                dataSets = [{
                  plotType   = "LINE"
                  targetAxis = "Y1"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter = "metric.type=\"${tile.metric}\" AND ${local.service_filter}"
                      aggregation = merge(
                        { alignmentPeriod = "60s", perSeriesAligner = tile.aligner, crossSeriesReducer = tile.reducer },
                        length(tile.groups) == 0 ? {} : { groupByFields = tile.groups }
                      )
                    }
                  }
                }]
              }
            }
        })
      ]
    }
  })
}
