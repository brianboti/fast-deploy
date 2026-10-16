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

# The smallest Cloud SQL for PostgreSQL instance: private address only, IAM sign-in only, zonal, HDD, daily backups kept
# a week, automatic storage growth capped. It bills every hour it exists; the spending cap stops it at the budget line.
resource "google_sql_database_instance" "need" {
  count               = var.needs.database == "postgres" ? 1 : 0
  name                = var.service
  region              = var.region
  database_version    = "POSTGRES_17"
  deletion_protection = true
  settings {
    tier                        = "db-f1-micro"
    edition                     = "ENTERPRISE"
    availability_type           = "ZONAL"
    disk_type                   = "PD_HDD"
    disk_size                   = 10
    disk_autoresize             = true
    disk_autoresize_limit       = 20
    user_labels                 = local.labels
    deletion_protection_enabled = true
    ip_configuration {
      ipv4_enabled    = false
      private_network = var.network
      ssl_mode        = "ENCRYPTED_ONLY"
    }
    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = false
      backup_retention_settings {
        retained_backups = 7
      }
    }
  }
  lifecycle {
    prevent_destroy = true
    # Stopped stays stopped: the spending cap, or the operator, stops an instance by setting its activation policy to NEVER,
    # and an apply for any other reason must not start it again (found 2026-10-16: a module change planned mono-admin's
    # stopped instance back to ALWAYS, about $8 a month). Starting it is a deliberate `gcloud sql instances patch`.
    ignore_changes = [settings[0].activation_policy]
  }
}
resource "google_sql_database" "app" {
  count    = var.needs.database == "postgres" ? 1 : 0
  name     = "app"
  instance = google_sql_database_instance.need[0].name
}
# The runtime identity signs in as itself: no password exists anywhere. Since PostgreSQL 15 an ordinary user may not
# create tables in the `public` schema, and Cloud SQL gives a new IAM user no privileges beyond connecting, so the
# migration job could not create a single table. The instance belongs to this application alone, so its identity gets
# Cloud SQL's administrative role (`cloudsqlsuperuser`, not a true superuser) and owns what it creates.
resource "google_sql_user" "runtime" {
  count          = var.needs.database == "postgres" ? 1 : 0
  name           = trimsuffix(google_service_account.runtime.email, ".gserviceaccount.com")
  instance       = google_sql_database_instance.need[0].name
  type           = "CLOUD_IAM_SERVICE_ACCOUNT"
  database_roles = ["cloudsqlsuperuser"]
}
# database_auth = "password", for code that connects with a URL: a built-in user `app` whose password the root generates in
# every run and these write-only arguments send once (password_version pins the first value; raise it to rotate both).
# The URL reaches the app as the secret <service>-DATABASE_URL, which the runtime may read through the app's secret grant
# above; neither Terraform's state nor any plan ever holds the password.
locals {
  password_auth    = var.needs.database == "postgres" && var.needs.database_auth == "password"
  password_version = 1
}
resource "google_sql_user" "password" {
  count       = local.password_auth ? 1 : 0
  name        = "app"
  instance    = google_sql_database_instance.need[0].name
  password_wo = var.database_password
  # The number of the URL's secret version: whenever a new version is written (the first run, a rotation, or a version
  # replaced after a failed create and the workflow's retry), the user gets the same run's password in place. A user is
  # never dropped for this: it may own the application's tables.
  password_wo_version = tonumber(regex("[0-9]+$", google_secret_manager_secret_version.database_url[0].name))
}
resource "google_secret_manager_secret" "database_url" {
  count     = local.password_auth ? 1 : 0
  secret_id = "${var.service}-DATABASE_URL"
  labels    = local.labels
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}
resource "google_secret_manager_secret_version" "database_url" {
  count                  = local.password_auth ? 1 : 0
  secret                 = google_secret_manager_secret.database_url[0].id
  secret_data_wo         = "postgresql://app:${var.database_password}@${google_sql_database_instance.need[0].private_ip_address}:5432/app?sslmode=require"
  secret_data_wo_version = local.password_version
  lifecycle {
    # A new instance means a new address and a new user: write the URL again in the same run. (A user deleted by hand is
    # put back with this run's password; raise password_version above to write both again.)
    replace_triggered_by = [google_sql_database_instance.need[0].id]
  }
}
resource "google_project_iam_member" "need_sql_runtime" {
  for_each = var.needs.database == "postgres" ? toset(["roles/cloudsql.client", "roles/cloudsql.instanceUser"]) : toset([])
  project  = var.project_id
  role     = each.value
  member   = local.runtime
}
resource "google_project_iam_member" "need_sql_deployer" {
  count   = var.needs.database == "postgres" ? 1 : 0
  project = var.project_id
  role    = "roles/cloudsql.viewer"
  member  = local.deployer
}

# The migration job: created here with the bootstrap image so the deployer may update and run it; each release points it
# at the release's own image and runs the app's migrate command before the candidate gets traffic.
resource "google_cloud_run_v2_job" "migrate" {
  count               = length(var.needs.migrate) > 0 ? 1 : 0
  name                = "${var.service}-migrate"
  location            = var.region
  deletion_protection = false
  labels              = local.labels
  template {
    template {
      service_account = google_service_account.runtime.email
      max_retries     = 0
      containers {
        image   = var.bootstrap_image
        command = [var.needs.migrate[0]]
        args    = slice(var.needs.migrate, 1, length(var.needs.migrate))
      }
      dynamic "vpc_access" {
        for_each = var.needs.database == "postgres" && var.network != "" ? [1] : []
        content {
          egress = "PRIVATE_RANGES_ONLY"
          network_interfaces {
            network    = var.network
            subnetwork = var.subnet
          }
        }
      }
    }
  }
  lifecycle {
    ignore_changes = [template, client, client_version]
  }
}
resource "google_cloud_run_v2_job_iam_member" "migrate_deployer" {
  count    = length(var.needs.migrate) > 0 ? 1 : 0
  location = var.region
  name     = google_cloud_run_v2_job.migrate[0].name
  role     = "roles/run.developer"
  member   = local.deployer
}
