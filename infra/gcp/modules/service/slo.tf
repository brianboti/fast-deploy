# Production's one objective (docs/ROADMAP.md D39, roadmap item 16.4): of the requests the service answered over the last 30
# days, at least slo_goal without a server error. Counted from Cloud Run's own request metric, the uptime check's calls to
# the health path included, so a quiet application still has a measure. Two alerts watch how fast the error budget burns,
# with Google's starting points: ten times the sustainable rate over an hour (an outage), twice over a day (a slow leak).
locals {
  slo_enabled = var.slo_goal > 0 && var.runtime_alerts
  burn_alerts = local.slo_enabled ? {
    fast = { lookback = "60m", threshold = 10, words = "fast: ten times the sustainable rate over the last hour" }
    slow = { lookback = "1440m", threshold = 2, words = "slowly: twice the sustainable rate over the last day" }
  } : {}
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
  display_name        = "${var.service}: ${var.slo_goal * 100} percent of requests without a server error over 30 days"
  goal                = var.slo_goal
  rolling_period_days = 30
  user_labels         = local.labels
  request_based_sli {
    good_total_ratio {
      bad_service_filter   = "metric.type=\"run.googleapis.com/request_count\" AND ${local.service_filter} AND metric.label.response_code_class=\"5xx\""
      total_service_filter = "metric.type=\"run.googleapis.com/request_count\" AND ${local.service_filter}"
    }
  }
}
resource "google_monitoring_alert_policy" "error_budget" {
  for_each     = local.burn_alerts
  display_name = "${var.service}: error budget burning ${each.key}"
  combiner     = "OR"
  conditions {
    display_name = "the error budget burns ${each.value.words}"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${google_monitoring_slo.availability[0].name}\", \"${each.value.lookback}\")"
      comparison      = "COMPARISON_GT"
      threshold_value = each.value.threshold
      duration        = "0s"
    }
  }
  notification_channels = var.alert_channels
  user_labels           = local.labels
  documentation {
    content   = "The ${local.environment_name} objective of ${var.service} (${var.slo_goal * 100} percent of requests without a server error over 30 days) is burning its error budget ${each.value.words}. Look at get_service_health, the recent logs and the newest release; roll back if a release caused it (docs/RUNBOOK.md, Production)."
    mime_type = "text/markdown"
  }
  alert_strategy {
    auto_close = "1800s"
  }
}
