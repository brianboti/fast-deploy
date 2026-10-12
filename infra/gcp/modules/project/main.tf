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
}

resource "google_project_service" "apis" {
  for_each           = toset(["run.googleapis.com", "artifactregistry.googleapis.com", "iam.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com", "storage.googleapis.com", "monitoring.googleapis.com", "recommender.googleapis.com", "pubsub.googleapis.com", "firestore.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}
resource "google_project_service" "cap_apis" {
  for_each           = local.cap_enabled ? toset(["pubsub.googleapis.com", "workflows.googleapis.com", "workflowexecutions.googleapis.com", "eventarc.googleapis.com", "cloudbilling.googleapis.com"]) : toset([])
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

# Budget: alerts at 50%, 90%, 100% and 100% forecast; with the cap enabled, every notification also reaches a topic.
resource "google_project_service" "billing_budgets" {
  count              = var.monthly_budget_usd > 0 ? 1 : 0
  service            = "billingbudgets.googleapis.com"
  disable_on_destroy = false
}
resource "google_monitoring_notification_channel" "budget_email" {
  count        = var.monthly_budget_usd > 0 && var.budget_alert_email != "" ? 1 : 0
  display_name = "lab-deploy staging budget alerts"
  type         = "email"
  labels       = { email_address = var.budget_alert_email }
  depends_on   = [google_project_service.apis]
}
resource "google_billing_budget" "staging" {
  count           = var.monthly_budget_usd > 0 ? 1 : 0
  billing_account = var.billing_account
  display_name    = "${var.project_id} staging monthly budget"
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
  for_each = local.cap_enabled ? toset(concat(["roles/workflows.invoker", "roles/eventarc.eventReceiver", "roles/logging.logWriter", "roles/run.viewer"], var.spending_cap.detach_billing ? ["roles/billing.projectManager"] : [])) : toset([])
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
  for_each = toset(["roles/run.viewer", "roles/monitoring.viewer", "roles/logging.viewer"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.observer.email}"
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

# Billing export: Google writes the export (enabled once in the Billing console) into this dataset; the weekly
# right-sizing job reads actual spend from it next to its list-price figures.
resource "google_bigquery_dataset" "billing" {
  count                      = var.billing_export_dataset != "" ? 1 : 0
  dataset_id                 = var.billing_export_dataset
  location                   = "US"
  description                = "Cloud Billing export read by Lab Deploy's weekly cost report"
  delete_contents_on_destroy = false
  depends_on                 = [google_project_service.bigquery]
}
