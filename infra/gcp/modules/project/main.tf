# Project-wide pieces shared by every application: APIs, scanning, the budget and its spending cap, notification
# channels, the registry tag role, and the billing export dataset. One instance per project.
locals {
  cap_enabled   = var.spending_cap.enabled && var.monthly_budget_usd > 0
  budget_topics = local.cap_enabled ? [google_pubsub_topic.budget[0].id] : []
  # The workflow discovers the services to close by their lab-deploy-managed label, so adding an app needs no re-apply here.
  cap_workflow = replace(replace(replace(replace(replace(file("${path.module}/cap-workflow.yaml"),
    "__THRESHOLD__", tostring(var.spending_cap.threshold)),
    "__PROJECT__", var.project_id),
    "__REGION__", var.region),
    "__DETACH__", var.spending_cap.detach_billing ? "true" : "false"),
  "__DETACH_THRESHOLD__", tostring(var.spending_cap.detach_threshold))
  # Viewer roles only; four let the health view confirm an app's declared needs exist (and that a database runs), and the
  # last two let the Monday digest read what Artifact Analysis holds for each serving image and pull it, so that analysis
  # goes on (docs/ROADMAP.md 26.3). The observer and the controller's machine (controller.tf) both hold exactly these.
  observer_roles = ["roles/run.viewer", "roles/monitoring.viewer", "roles/logging.viewer", "roles/storage.bucketViewer", "roles/pubsub.viewer", "roles/datastore.viewer", "roles/cloudsql.viewer",
  "roles/containeranalysis.occurrences.viewer", "roles/artifactregistry.reader"]
}

resource "google_project_service" "apis" {
  for_each           = toset(["run.googleapis.com", "artifactregistry.googleapis.com", "iam.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com", "storage.googleapis.com", "monitoring.googleapis.com", "recommender.googleapis.com", "pubsub.googleapis.com", "firestore.googleapis.com", "iap.googleapis.com", "aiplatform.googleapis.com", "servicehealth.googleapis.com", "dlp.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}
resource "google_project_service" "cap_apis" {
  for_each           = local.cap_enabled ? toset(["workflows.googleapis.com", "workflowexecutions.googleapis.com", "eventarc.googleapis.com", "cloudbilling.googleapis.com"]) : toset([])
  service            = each.value
  disable_on_destroy = false
}
resource "google_project_service" "secrets" {
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = false
}
resource "google_project_service" "bigquery" {
  count              = var.billing_export_dataset != "" ? 1 : 0
  service            = "bigquery.googleapis.com"
  disable_on_destroy = false
}

# Automatic vulnerability scanning of every pushed image digest (Artifact Analysis), about USD 0.26 per digest.
resource "google_project_service" "container_scanning" {
  count              = var.image_vulnerability_scanning ? 1 : 0
  service            = "containerscanning.googleapis.com"
  disable_on_destroy = false
}

# The writer role can create tags but not delete them; retiring stale serving-<revision> tags needs this one permission.
resource "google_project_iam_custom_role" "tag_manager" {
  role_id     = "labDeployTagManager"
  title       = "Lab Deploy registry tag manager"
  description = "Lets a deployer retire serving-<revision> tags in its own repository"
  permissions = ["artifactregistry.tags.delete"]
}
# Calls to a model through Vertex AI (needs.models, docs/ROADMAP.md D47) and nothing else: the stock Vertex AI User role
# carried 459 permissions on 2026-10-19, most of them to create resources that bill. Granted per app by its own root.
resource "google_project_iam_custom_role" "model_user" {
  role_id     = "labDeployModelUser"
  title       = "Lab Deploy model user"
  description = "Calls models through Vertex AI (predictions only); creates nothing"
  permissions = ["aiplatform.endpoints.predict"]
}

# Budget: alerts at 50%, 90%, 100% and 100% forecast; with the cap enabled, every notification also reaches a topic.
resource "google_project_service" "billing_budgets" {
  count              = var.monthly_budget_usd > 0 ? 1 : 0
  service            = "billingbudgets.googleapis.com"
  disable_on_destroy = false
}
resource "google_monitoring_notification_channel" "budget_email" {
  count        = var.monthly_budget_usd > 0 && var.budget_alert_email != "" ? 1 : 0
  display_name = "lab-deploy ${var.environment} budget alerts"
  type         = "email"
  labels       = { email_address = var.budget_alert_email }
  depends_on   = [google_project_service.apis]
}
resource "google_billing_budget" "staging" {
  count           = var.monthly_budget_usd > 0 ? 1 : 0
  billing_account = var.billing_account
  display_name    = "${var.project_id} ${var.environment} monthly budget"
  budget_filter {
    projects        = ["projects/${var.project_number}"]
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
    for_each = var.budget_alert_email != "" || local.cap_enabled ? [1] : []
    content {
      monitoring_notification_channels = var.budget_alert_email != "" ? [google_monitoring_notification_channel.budget_email[0].name] : []
      pubsub_topic                     = local.cap_enabled ? google_pubsub_topic.budget[0].id : null
      schema_version                   = local.cap_enabled ? "1.0" : null
      disable_default_iam_recipients   = false
    }
  }
  lifecycle {
    precondition {
      condition     = var.billing_account != ""
      error_message = "Set billing_account when monthly_budget_usd is greater than 0."
    }
  }
  depends_on = [google_project_service.billing_budgets, google_pubsub_topic_iam_member.budget_publisher]
}

# Spending cap: budget notification -> Pub/Sub -> Eventarc -> a Cloud Workflow that closes ingress on the platform's
# services (and optionally detaches billing). No code to build; the workflow definition is cap-workflow.yaml.
resource "google_pubsub_topic" "budget" {
  count      = local.cap_enabled ? 1 : 0
  name       = "lab-deploy-budget-alerts"
  depends_on = [google_project_service.cap_apis]
}
resource "google_pubsub_topic_iam_member" "budget_publisher" {
  count  = local.cap_enabled ? 1 : 0
  topic  = google_pubsub_topic.budget[0].name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:billing-budget-alert@system.gserviceaccount.com"
}
resource "google_service_account" "cap" {
  count        = local.cap_enabled ? 1 : 0
  account_id   = "lab-deploy-spending-cap"
  display_name = "Lab Deploy spending cap workflow"
  depends_on   = [google_project_service.apis]
}
resource "google_project_iam_member" "cap_roles" {
  for_each = local.cap_enabled ? toset(concat(["roles/workflows.invoker", "roles/eventarc.eventReceiver", "roles/logging.logWriter", "roles/run.viewer", "roles/cloudsql.editor"], var.spending_cap.detach_billing ? ["roles/billing.projectManager"] : [])) : toset([])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.cap[0].email}"
}
resource "google_workflows_workflow" "cap" {
  count           = local.cap_enabled ? 1 : 0
  name            = "lab-deploy-spending-cap"
  region          = var.region
  description     = "Closes ingress on Lab Deploy services when the budget line is crossed; reopen with gcloud run services update SERVICE --ingress=all"
  service_account = google_service_account.cap[0].id
  source_contents = local.cap_workflow
  depends_on      = [google_project_service.cap_apis]
}
resource "google_eventarc_trigger" "cap" {
  count    = local.cap_enabled ? 1 : 0
  name     = "lab-deploy-spending-cap"
  location = var.region
  matching_criteria {
    attribute = "type"
    value     = "google.cloud.pubsub.topic.v1.messagePublished"
  }
  transport {
    pubsub {
      topic = google_pubsub_topic.budget[0].id
    }
  }
  destination {
    workflow = google_workflows_workflow.cap[0].id
  }
  service_account = google_service_account.cap[0].email
  depends_on      = [google_project_iam_member.cap_roles]
}

# A viewer-only identity for the controller's live health and log views. On a machine it is attached directly; on a
# laptop the operator impersonates it through gcloud, so the controller never holds a key and never gains write access.
resource "google_service_account" "observer" {
  account_id   = "lab-deploy-observer"
  display_name = "Lab Deploy observer (viewer only)"
  depends_on   = [google_project_service.apis]
}
resource "google_project_iam_member" "observer_roles" {
  for_each = toset(local.observer_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.observer.email}"
}
# Identities of another project that may view this one with the observer's roles: in production, the controller's
# machine in staging (controller.tf there), so live health and logs work for production apps too (docs/ROADMAP.md D37).
resource "google_project_iam_member" "outside_viewers" {
  for_each = { for pair in setproduct(var.viewer_members, local.observer_roles) : "${pair[0]} ${pair[1]}" => pair }
  project  = var.project_id
  role     = each.value[1]
  member   = each.value[0]
}
resource "google_service_account_iam_member" "observer_impersonators" {
  for_each           = toset(var.observer_impersonators)
  service_account_id = google_service_account.observer.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = each.value
}

# Runtime alert channels shared by every app in the project.
resource "google_monitoring_notification_channel" "alert_email" {
  count        = var.alert_email != "" ? 1 : 0
  display_name = "lab-deploy alerts (email)"
  type         = "email"
  labels       = { email_address = var.alert_email }
  depends_on   = [google_project_service.apis]
}
resource "google_monitoring_notification_channel" "alert_webhook" {
  count        = var.alert_webhook_url != "" ? 1 : 0
  display_name = "lab-deploy alerts (webhook)"
  type         = "webhook_tokenauth"
  labels       = { url = var.alert_webhook_url }
  depends_on   = [google_project_service.apis]
}

# Google's side and access changes (docs/ROADMAP.md 25.4, 25.6). Personalized Service Health writes Google's incidents
# that concern this project to its log; an alert on a new one in this region (or a global one) tells the operator it is
# Google, not the application, which changes what to do. Free, and recommended by Cloud Run's monitoring page.
resource "google_monitoring_alert_policy" "google_incident" {
  display_name = "Google Cloud: an incident in ${var.region}"
  combiner     = "OR"
  conditions {
    display_name = "Personalized Service Health reports a new incident here"
    condition_matched_log {
      filter = "logName=\"projects/${var.project_id}/logs/servicehealth.googleapis.com%2Factivity\" AND resource.type=\"servicehealth.googleapis.com/Event\" AND labels.\"servicehealth.googleapis.com/new_event\"=\"true\" AND jsonPayload.category=\"INCIDENT\" AND jsonPayload.impactedLocations=~\"${var.region}|global\""
    }
  }
  notification_channels = concat(google_monitoring_notification_channel.alert_email[*].id, google_monitoring_notification_channel.alert_webhook[*].id)
  severity              = "WARNING"
  user_labels           = { lab-deploy-managed = "true", lab-deploy-environment = var.environment }
  documentation {
    subject   = "[${var.environment}] Google reports an incident in ${var.region}"
    content   = "Google's Personalized Service Health reports an incident that may touch this project. Errors or slowness now are likely Google's: rolling back a release will not help. Follow it on the Service Health page of the console and status.cloud.google.com, and ask Claude for today's digest (docs/RUNBOOK.md, \"An incident\")."
    mime_type = "text/markdown"
  }
  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close = "86400s"
  }
  depends_on = [google_project_service.apis]
}
# Access granted or changed by anyone but the pipeline: a role on a service, a bucket, a secret or the project, or a service
# made public. The drift check sees only the members Terraform manages, so a grant made beside them in the console was
# invisible; the Admin Activity audit log, always on and free, records every one. An operator's own foundation apply raises
# it too, once an hour at most: that is expected.
resource "google_monitoring_alert_policy" "access_changed" {
  display_name = "Lab Deploy: access changed outside the pipeline"
  combiner     = "OR"
  conditions {
    display_name = "SetIamPolicy by an identity other than the platform's apply identity"
    condition_matched_log {
      filter = join(" AND ", concat([
        "logName=\"projects/${var.project_id}/logs/cloudaudit.googleapis.com%2Factivity\"",
        "protoPayload.methodName:\"SetIamPolicy\"",
        "NOT protoPayload.authenticationInfo.principalEmail:\"lab-deploy-infra-\"",
        "severity!=ERROR",
      ], [for member in var.access_change_quiet_members : "protoPayload.authenticationInfo.principalEmail!=\"${member}\""]))
    }
  }
  notification_channels = concat(google_monitoring_notification_channel.alert_email[*].id, google_monitoring_notification_channel.alert_webhook[*].id)
  severity              = "ERROR"
  user_labels           = { lab-deploy-managed = "true", lab-deploy-environment = var.environment }
  documentation {
    subject   = "[${var.environment}] access changed outside the pipeline"
    content   = "Someone or something other than the platform's pipeline changed who may use a resource in this project (an IAM policy). If it was you applying the foundation, nothing to do. Otherwise open Logs Explorer at the time of this alert (protoPayload.methodName:SetIamPolicy) to see who and what, and put the code's version back (docs/RUNBOOK.md, \"Drift reported\")."
    mime_type = "text/markdown"
  }
  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close = "1800s"
  }
}

# Billing export: Google writes the export (enabled once in the Billing console) into this dataset; the weekly
# right-sizing job reads actual spend from it next to its list-price figures.
resource "google_bigquery_dataset" "billing" {
  count                      = var.billing_export_dataset != "" ? 1 : 0
  dataset_id                 = var.billing_export_dataset
  location                   = "US"
  description                = "Cloud Billing export read by Lab Deploy's weekly cost report"
  labels                     = { lab-deploy-managed = "true" }
  delete_contents_on_destroy = false
  depends_on                 = [google_project_service.bigquery]
}

# The daily digest's cost line (docs/ROADMAP.md 24.2): the observer and the controller's machine read the billing export
# with BigQuery, the same rows the weekly cost report reads. Running a query needs the job user role in this project; the
# rows need data viewer on the export's dataset only. The keys are fixed words, so a first apply plans before the
# identities exist.
locals {
  billing_readers = var.billing_export_dataset == "" ? {} : merge(
    { observer = "serviceAccount:${google_service_account.observer.email}" },
    local.controller_enabled ? { machine = "serviceAccount:${google_service_account.controller[0].email}" } : {}
  )
}
resource "google_project_iam_member" "billing_query" {
  for_each = local.billing_readers
  project  = var.project_id
  role     = "roles/bigquery.jobUser"
  member   = each.value
}
resource "google_bigquery_dataset_iam_member" "billing_reader" {
  for_each   = local.billing_readers
  project    = var.project_id
  dataset_id = google_bigquery_dataset.billing[0].dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = each.value
}

# The cap acts through this role on each managed service (granted per service by the service module) and on
# instances through roles/cloudsql.editor: the narrowest set that can close ingress and stop an instance.
resource "google_project_iam_custom_role" "cap_operator" {
  count       = local.cap_enabled ? 1 : 0
  role_id     = "labDeployCapOperator"
  title       = "Lab Deploy spending-cap operator"
  description = "Reads and updates a Cloud Run service's ingress setting, and cancels a managed job's running execution; nothing else"
  # run.executions.cancel (docs/ROADMAP.md 27.7): granted on each managed job by the service module; the cap's project-wide
  # run.viewer lists the jobs and their executions.
  permissions = ["run.services.get", "run.services.update", "run.services.list", "run.operations.get", "run.executions.cancel"]
}
# The controller's guard (docs/ROADMAP.md D70): it cancels a job's execution that runs more than one task, past its timeout
# or beyond the day's runs. Granted on each job by the service module; the machine's run.viewer already reads executions.
# How a run of an application's job ended (docs/ROADMAP.md 27.6): its execution and its task's exit code. Cloud Run checks
# these reads on the project, not on the job (the live exercise of 2026-10-21: a job-level viewer grant was refused), and takes
# no IAM condition on a resource's name, so the service module grants this narrow role on the project to a service with jobs.
resource "google_project_iam_custom_role" "job_reader" {
  role_id     = "labDeployJobReader"
  title       = "Lab Deploy job reader"
  description = "Reads Cloud Run job executions and their tasks; nothing else"
  permissions = ["run.executions.get", "run.executions.list", "run.tasks.get", "run.tasks.list"]
}
resource "google_project_iam_custom_role" "job_guard" {
  role_id     = "labDeployJobGuard"
  title       = "Lab Deploy job guard"
  description = "Cancels a Cloud Run job's execution; nothing else"
  permissions = ["run.executions.cancel", "run.operations.get"]
}
# A cap execution that fails is the one failure that must never go unnoticed.
resource "google_monitoring_alert_policy" "cap_failures" {
  count        = local.cap_enabled ? 1 : 0
  display_name = "Lab Deploy: spending-cap workflow failed"
  combiner     = "OR"
  conditions {
    display_name = "a cap execution finished with an error"
    condition_threshold {
      filter          = "metric.type=\"workflows.googleapis.com/finished_execution_count\" AND resource.type=\"workflows.googleapis.com/Workflow\" AND resource.label.workflow_id=\"${google_workflows_workflow.cap[0].name}\" AND metric.label.status!=\"SUCCEEDED\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
      trigger {
        count = 1
      }
    }
  }
  notification_channels = concat(google_monitoring_notification_channel.alert_email[*].id, google_monitoring_notification_channel.alert_webhook[*].id)
  severity              = "CRITICAL"
  user_labels           = { lab-deploy-managed = "true", lab-deploy-environment = var.environment }
  documentation {
    subject   = "[${var.environment}] the spending cap could not act"
    content   = "A run of the spending cap's workflow failed, so at the budget line some service or database may stay open and keep billing. Look at the execution in Cloud Workflows (lab-deploy-spending-cap) and docs/RUNBOOK.md, \"The spending cap fired\"; Google's native spend cap, where it exists, still stops Cloud Run."
    mime_type = "text/markdown"
  }
  alert_strategy {
    auto_close = "1800s"
  }
}

# Controller backups: the controller copies its database and configuration here nightly (config `backup_bucket`): a
# laptop under controller/, the machine under machine/ beside its Litestream replica (docs/RUNBOOK.md, "Restore the
# controller", "The controller's machine"). Private, versioned and pruned.
resource "google_storage_bucket" "backups" {
  name                        = "${var.project_id}-lab-deploy-backups"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = { lab-deploy-managed = "true" }
  versioning { enabled = true }
  lifecycle_rule {
    condition { num_newer_versions = 10 }
    action { type = "Delete" }
  }
  lifecycle_rule {
    condition { age = 120 }
    action { type = "Delete" }
  }
  # Litestream replaces its small files every few minutes; the versions it retires are of no use a day later.
  lifecycle_rule {
    condition {
      matches_prefix             = ["machine/litestream/"]
      days_since_noncurrent_time = 1
    }
    action { type = "Delete" }
  }
}
