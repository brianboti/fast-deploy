# Production's one objective (docs/ROADMAP.md D39, D58): of the requests people and programs sent over the last 30 days,
# at least slo_goal answered without a server error. Since 1.26.0 it counts the users' requests only: two log-based
# counters read Cloud Run's request log with the health path left out, because Cloud Run's request metric counts the
# uptime check's calls too (three regions every minute), which on a quiet application are nearly all of it and made a
# handful of real errors either invisible or, after a drill, a month's spent budget. The uptime check keeps its own alert
# ("health check failing") and its own line in the health view.
#
# Three burn-rate alerts follow Google's multiwindow pattern: each needs its long window and its short window (a twelfth of
# it) both burning, and at least 100 user requests in the long window, so a few errors on a quiet service never page.
locals {
  slo_enabled = var.slo_goal > 0 && var.runtime_alerts
  # The users' requests: Cloud Run's request log for this service, without the health path when there is one.
  user_requests_filter = join(" AND ", compact([
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${var.service}\"",
    "logName=\"projects/${var.project_id}/logs/run.googleapis.com%2Frequests\"",
    var.health_probe ? "NOT httpRequest.requestUrl=~\"^https?://[^/]+${var.health_path}([?].*)?$\"" : "",
  ]))
  burn_alerts = local.slo_enabled ? {
    fast = {
      title = "error budget burning fast", long = "60m", short = "5m", seconds = 3600, threshold = 10, severity = "CRITICAL"
      words = "ten times the sustainable rate over the last hour and the last five minutes: an outage. Act now"
    }
    slow = {
      title = "error budget burning slowly", long = "360m", short = "30m", seconds = 21600, threshold = 2, severity = "ERROR"
      words = "twice the sustainable rate over the last six hours and the last half hour: a leak. Look today"
    }
    steady = {
      title = "error budget used steadily", long = "1440m", short = "120m", seconds = 86400, threshold = 1, severity = "WARNING"
      words = "at the sustainable rate or faster for a whole day: the budget will be gone by the end of the window. Look this week"
    }
  } : {}
}
resource "google_logging_metric" "user_requests" {
  count       = local.slo_enabled ? 1 : 0
  name        = "lab-deploy/${var.service}/requests"
  description = "Requests to ${var.service} besides its health path, from Cloud Run's request log (docs/ROADMAP.md D58)"
  filter      = local.user_requests_filter
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}
resource "google_logging_metric" "user_errors" {
  count       = local.slo_enabled ? 1 : 0
  name        = "lab-deploy/${var.service}/server-errors"
  description = "Requests to ${var.service} besides its health path answered 5xx (docs/ROADMAP.md D58)"
  filter      = "${local.user_requests_filter} AND httpRequest.status>=500"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}
resource "google_monitoring_custom_service" "slo" {
  count        = local.slo_enabled ? 1 : 0
  service_id   = "${var.service}-${local.environment_name}"
  display_name = "${var.service} ${local.environment_name}"
  user_labels  = local.labels
}
resource "google_monitoring_slo" "availability" {
  count               = local.slo_enabled ? 1 : 0
  service             = google_monitoring_custom_service.slo[0].service_id
  slo_id              = "availability"
  display_name        = "${var.service}: ${var.slo_goal * 100} percent of user requests without a server error over 30 days"
  goal                = var.slo_goal
  rolling_period_days = 30
  user_labels         = local.labels
  request_based_sli {
    good_total_ratio {
      bad_service_filter   = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.user_errors[0].name}\" AND resource.type=\"cloud_run_revision\""
      total_service_filter = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.user_requests[0].name}\" AND resource.type=\"cloud_run_revision\""
    }
  }
}
resource "google_monitoring_alert_policy" "error_budget" {
  for_each     = local.burn_alerts
  display_name = "${var.service}: ${each.value.title}"
  combiner     = "AND"
  severity     = each.value.severity
  conditions {
    display_name = "the budget burns at more than ${each.value.threshold} times the sustainable rate over ${each.value.long}"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${google_monitoring_slo.availability[0].name}\", \"${each.value.long}\")"
      comparison      = "COMPARISON_GT"
      threshold_value = each.value.threshold
      duration        = "0s"
    }
  }
  conditions {
    display_name = "and still over the last ${each.value.short}"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${google_monitoring_slo.availability[0].name}\", \"${each.value.short}\")"
      comparison      = "COMPARISON_GT"
      threshold_value = each.value.threshold
      duration        = "0s"
    }
  }
  conditions {
    display_name = "with at least 100 user requests over ${each.value.long}"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.user_requests[0].name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 99
      duration        = "0s"
      aggregations {
        alignment_period     = "${each.value.seconds}s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
  notification_channels = var.alert_channels
  user_labels           = merge(local.labels, { lab-deploy-environment = local.environment_name })
  documentation {
    subject   = "[${local.environment_name}] ${var.service}: ${each.value.title}"
    content   = "The ${local.environment_name} objective of ${var.service} (${var.slo_goal * 100} percent of user requests without a server error over 30 days) is burning its error budget at ${each.value.words}. Ask Claude for \"${var.service}'s health in production\" and its errors; if the newest promotion caused it, roll production back (docs/RUNBOOK.md, \"The objective and its error budget\")."
    mime_type = "text/markdown"
  }
  alert_strategy {
    auto_close = "1800s"
  }
}
