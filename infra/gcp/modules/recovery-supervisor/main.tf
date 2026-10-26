variable "project_id" { type = string }
variable "region" { type = string }
variable "image" {
  type = string
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.image))
    error_message = "The supervisor image must be pinned by digest."
  }
}
variable "settings" {
  type = object({
    repository = string, app_id = string, key_secret = string,
    targets    = map(any), observe_only = optional(bool, true), stale_seconds = optional(number, 180)
  })
}
variable "alert_channels" { type = list(string) }

resource "google_project_service" "api" {
  for_each           = toset(["run.googleapis.com", "cloudscheduler.googleapis.com", "secretmanager.googleapis.com", "monitoring.googleapis.com"])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
resource "google_service_account" "supervisor" {
  project    = var.project_id
  account_id = "recovery-supervisor"
}
resource "google_service_account" "scheduler" {
  project    = var.project_id
  account_id = "recovery-clock"
}
resource "google_secret_manager_secret_iam_member" "key" {
  secret_id = var.settings.key_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.supervisor.email}"
}
resource "google_storage_bucket_iam_member" "read" {
  for_each = var.settings.targets
  bucket   = each.value.recovery_bucket
  role     = "roles/storage.objectViewer"
  member   = "serviceAccount:${google_service_account.supervisor.email}"
  condition {
    title      = "Checkpoint and request marker"
    expression = "resource.name == 'projects/_/buckets/${each.value.recovery_bucket}/objects/releases/${each.value.service}.json' || resource.name == 'projects/_/buckets/${each.value.recovery_bucket}/objects/recovery-requests/${each.value.service}.json'"
  }
}
resource "google_storage_bucket_iam_member" "request" {
  for_each = var.settings.targets
  bucket   = each.value.recovery_bucket
  role     = "roles/storage.objectUser"
  member   = "serviceAccount:${google_service_account.supervisor.email}"
  condition {
    title      = "Recovery request marker only"
    expression = "resource.name == 'projects/_/buckets/${each.value.recovery_bucket}/objects/recovery-requests/${each.value.service}.json'"
  }
}
resource "google_cloud_run_v2_service" "supervisor" {
  depends_on          = [google_project_service.api]
  project             = var.project_id
  location            = var.region
  name                = "recovery-supervisor"
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_ALL"
  template {
    service_account                  = google_service_account.supervisor.email
    timeout                          = "300s"
    max_instance_request_concurrency = 1
    scaling { max_instance_count = 1 }
    containers {
      image   = var.image
      command = ["uvicorn"]
      args    = ["labdeploy.recovery_supervisor:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
      ports { container_port = 8000 }
      resources { limits = { cpu = "1", memory = "512Mi" } }
      env {
        name  = "RECOVERY_SETTINGS"
        value = jsonencode(var.settings)
      }
    }
  }
}
resource "google_cloud_run_v2_service_iam_member" "invoke" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.supervisor.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}
resource "google_cloud_scheduler_job" "tick" {
  depends_on       = [google_project_service.api]
  project          = var.project_id
  region           = var.region
  name             = "production-recovery"
  schedule         = "* * * * *"
  attempt_deadline = "300s"
  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.supervisor.uri}/tick"
    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = google_cloud_run_v2_service.supervisor.uri
    }
  }
}
resource "google_monitoring_alert_policy" "failure" {
  project               = var.project_id
  display_name          = "Production recovery requires attention"
  combiner              = "OR"
  notification_channels = var.alert_channels
  conditions {
    display_name = "Recovery supervisor reported a manual hold or provider failure"
    condition_matched_log {
      filter = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"recovery-supervisor\" AND (httpRequest.status>=500 OR textPayload=~\"manual_review|unavailable\")"
    }
  }
  alert_strategy {
    notification_rate_limit { period = "300s" }
    auto_close = "1800s"
  }
}
# A log-match policy takes one condition, and silence produces no log line: the scheduler or the service stopping is a
# separate alert on the absence of requests, independent of the controller.
resource "google_monitoring_alert_policy" "silent" {
  project               = var.project_id
  display_name          = "Production recovery supervisor is not running"
  combiner              = "OR"
  notification_channels = var.alert_channels
  conditions {
    display_name = "No supervisor invocation for 10 minutes"
    condition_absent {
      filter   = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"recovery-supervisor\" AND metric.type = \"run.googleapis.com/request_count\""
      duration = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
  alert_strategy { auto_close = "1800s" }
}
output "url" { value = google_cloud_run_v2_service.supervisor.uri }
