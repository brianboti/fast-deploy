# Alerts for what the application declared under needs (docs/ROADMAP.md 26.2): only where a topic or a database exists,
# under the same runtime_alerts switch as the service's own. One policy per dependency, its conditions together, because
# Google's alert pricing from 2027 counts policies and conditions (docs/RESEARCH-2026-10-21-scaling.md, section 7).
locals {
  # Cloud SQL's default max_connections: 25 on the shared-core db-f1-micro, 100 on db-custom-1-3840 (the sla tier).
  database_connection_limit = local.database_tier == "sla" ? 100 : 25
  database_id               = "${var.project_id}:${var.service}"
}

# A topic's messages that nobody takes: the oldest unacknowledged message on the platform's subscription is over an hour
# old. Either the code that reads the subscription stopped, or nothing reads it by design (then the alert says so once
# and messages expire after the subscription's retention).
resource "google_monitoring_alert_policy" "need_topic" {
  for_each     = var.runtime_alerts ? toset(var.needs.topics) : toset([])
  display_name = "${var.service}: messages waiting on ${each.key}"
  combiner     = "OR"
  conditions {
    display_name = "the oldest unacknowledged message on ${var.service}-${each.key} is over an hour old"
    condition_threshold {
      filter          = "metric.type=\"pubsub.googleapis.com/subscription/oldest_unacked_message_age\" AND resource.type=\"pubsub_subscription\" AND resource.label.subscription_id=\"${google_pubsub_subscription.need[each.key].name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 3600
      duration        = "600s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MAX"
      }
      trigger {
        count = 1
      }
    }
  }
  notification_channels = var.alert_channels
  severity              = "WARNING"
  user_labels           = merge(local.labels, { lab-deploy-environment = local.environment_name })
  documentation {
    subject   = "[${local.environment_name}] ${var.service}: messages waiting on ${each.key}"
    content   = "Messages published to ${var.service}'s topic ${each.key} have waited over an hour on the subscription ${var.service}-${each.key}: the code that reads it stopped, fails before it acknowledges, or nothing reads it. Ask Claude for \"${var.service}'s health\"${local.where} and its error lines (docs/RUNBOOK.md, \"A dependency's alert\")."
    mime_type = "text/markdown"
  }
  alert_strategy {
    auto_close = "3600s"
  }
}

# The database's disk nearly full (it grows by itself only up to disk_autoresize_limit) or its connections near the limit.
resource "google_monitoring_alert_policy" "need_database" {
  count        = var.runtime_alerts && var.needs.database == "postgres" ? 1 : 0
  display_name = "${var.service}: database near a limit"
  combiner     = "OR"
  conditions {
    display_name = "the database's disk is over 90 percent full for ten minutes"
    condition_threshold {
      filter          = "metric.type=\"cloudsql.googleapis.com/database/disk/utilization\" AND resource.type=\"cloudsql_database\" AND resource.label.database_id=\"${local.database_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.9
      duration        = "600s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MAX"
      }
      trigger {
        count = 1
      }
    }
  }
  conditions {
    display_name = "over 80 percent of the database's ${local.database_connection_limit} connections in use for ten minutes"
    condition_threshold {
      filter          = "metric.type=\"cloudsql.googleapis.com/database/postgresql/num_backends\" AND resource.type=\"cloudsql_database\" AND resource.label.database_id=\"${local.database_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = floor(local.database_connection_limit * 0.8)
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.database_id"]
      }
      trigger {
        count = 1
      }
    }
  }
  notification_channels = var.alert_channels
  severity              = "ERROR"
  user_labels           = merge(local.labels, { lab-deploy-environment = local.environment_name })
  documentation {
    subject   = "[${local.environment_name}] ${var.service}: database near a limit"
    content   = "${var.service}'s Cloud SQL database is close to its disk or its connection limit. A full disk stops writes; at the connection limit new connections are refused. Ask Claude for \"${var.service}'s health\"${local.where} (docs/RUNBOOK.md, \"A dependency's alert\")."
    mime_type = "text/markdown"
  }
  alert_strategy {
    auto_close = "3600s"
  }
}

# A failed automated backup: Cloud SQL writes no metric for it, only a system-event audit log entry
# (cloudsql.instances.automatedBackup with a failed window status, Google's backup audit-log page, checked 2026-10-21).
resource "google_monitoring_alert_policy" "need_database_backup" {
  count        = var.runtime_alerts && var.needs.database == "postgres" ? 1 : 0
  display_name = "${var.service}: database backup failed"
  combiner     = "OR"
  conditions {
    display_name = "an automated backup of the database failed"
    condition_matched_log {
      filter = "logName=\"projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Fsystem_event\" AND resource.type=\"cloudsql_database\" AND resource.labels.database_id=\"${local.database_id}\" AND protoPayload.methodName=\"cloudsql.instances.automatedBackup\" AND protoPayload.metadata.windowStatus=(\"STATUS_FAILED\" OR \"STATUS_ATTEMPT_FAILED\")"
    }
  }
  notification_channels = var.alert_channels
  severity              = "ERROR"
  user_labels           = merge(local.labels, { lab-deploy-environment = local.environment_name })
  documentation {
    subject   = "[${local.environment_name}] ${var.service}: database backup failed"
    content   = "An automated backup of ${var.service}'s Cloud SQL database failed. Until one succeeds, a restore goes back to the last good backup. A stopped instance (the spending cap, or by hand) takes no backup and raises nothing. See the instance's Backups tab, then docs/RUNBOOK.md, \"A dependency's alert\"."
    mime_type = "text/markdown"
  }
  alert_strategy {
    notification_rate_limit {
      period = "86400s"
    }
    auto_close = "86400s"
  }
}
