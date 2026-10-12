# What the application declared under `needs` in platform.yaml: buckets, topics (each with one pull subscription) and a
# Firestore database. Names follow fixed conventions so the release runner can set the variables without reading any
# output: BUCKET_<NAME> = <project>-<service>-<name>, TOPIC_<NAME> = projects/<project>/topics/<service>-<name>,
# SUBSCRIPTION_<NAME> = projects/<project>/subscriptions/<service>-<name>, FIRESTORE_DATABASE = <service>.
# The runtime identity gets the narrowest role that lets the app use each one; the deployer may only look, for the
# pre-check that stops a release whose needs do not exist yet. Data-holding resources cannot be destroyed by a plan.
locals {
  runtime  = "serviceAccount:${google_service_account.runtime.email}"
  deployer = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_storage_bucket" "need" {
  for_each                    = toset(var.needs.buckets)
  name                        = "${var.project_id}-${var.service}-${each.key}"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = local.labels
  lifecycle {
    prevent_destroy = true
  }
}
resource "google_storage_bucket_iam_member" "need_runtime" {
  for_each = toset(var.needs.buckets)
  bucket   = google_storage_bucket.need[each.key].name
  role     = "roles/storage.objectUser"
  member   = local.runtime
}
resource "google_storage_bucket_iam_member" "need_deployer" {
  for_each = toset(var.needs.buckets)
  bucket   = google_storage_bucket.need[each.key].name
  role     = "roles/storage.bucketViewer"
  member   = local.deployer
}

resource "google_pubsub_topic" "need" {
  for_each = toset(var.needs.topics)
  name     = "${var.service}-${each.key}"
  labels   = local.labels
}
resource "google_pubsub_subscription" "need" {
  for_each             = toset(var.needs.topics)
  name                 = "${var.service}-${each.key}"
  topic                = google_pubsub_topic.need[each.key].id
  ack_deadline_seconds = 20
  labels               = local.labels
  expiration_policy {
    ttl = "" # never expires for lack of use; a lab app may be idle for months
  }
}
resource "google_pubsub_topic_iam_member" "need_publisher" {
  for_each = toset(var.needs.topics)
  topic    = google_pubsub_topic.need[each.key].name
  role     = "roles/pubsub.publisher"
  member   = local.runtime
}
resource "google_pubsub_subscription_iam_member" "need_subscriber" {
  for_each     = toset(var.needs.topics)
  subscription = google_pubsub_subscription.need[each.key].name
  role         = "roles/pubsub.subscriber"
  member       = local.runtime
}
resource "google_pubsub_topic_iam_member" "need_topic_viewer" {
  for_each = toset(var.needs.topics)
  topic    = google_pubsub_topic.need[each.key].name
  role     = "roles/pubsub.viewer"
  member   = local.deployer
}
resource "google_pubsub_subscription_iam_member" "need_subscription_viewer" {
  for_each     = toset(var.needs.topics)
  subscription = google_pubsub_subscription.need[each.key].name
  role         = "roles/pubsub.viewer"
  member       = local.deployer
}

# One named Firestore database per application (Native mode, regional, no point-in-time recovery: that costs storage).
resource "google_firestore_database" "need" {
  count                             = var.needs.database == "firestore" ? 1 : 0
  name                              = var.service
  location_id                       = var.region
  type                              = "FIRESTORE_NATIVE"
  delete_protection_state           = "DELETE_PROTECTION_ENABLED"
  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_DISABLED"
  lifecycle {
    prevent_destroy = true
  }
}
resource "google_project_iam_member" "need_firestore_runtime" {
  count   = var.needs.database == "firestore" ? 1 : 0
  project = var.project_id
  role    = "roles/datastore.user"
  member  = local.runtime
  condition {
    title       = "${var.service} database only"
    description = "The app's runtime reads and writes its own Firestore database"
    expression  = "resource.name == \"projects/${var.project_id}/databases/${var.service}\""
  }
}
resource "google_project_iam_member" "need_firestore_deployer" {
  count   = var.needs.database == "firestore" ? 1 : 0
  project = var.project_id
  role    = "roles/datastore.viewer"
  member  = local.deployer
  condition {
    title       = "${var.service} database only (deployer pre-check)"
    description = "Lets the release runner confirm the database exists before deploying"
    expression  = "resource.name == \"projects/${var.project_id}/databases/${var.service}\""
  }
}
