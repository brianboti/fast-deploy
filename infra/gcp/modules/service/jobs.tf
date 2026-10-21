# Jobs the service starts itself, and the clock that wakes it (docs/ROADMAP.md D64 to D70). Each declared job is a Cloud
# Run job <service>-<name> with an identity of its own that holds only what the contract gave it: its buckets (read or
# write), the model role, its secrets. Terraform creates the job with the bootstrap image and ignores its template, as for
# the migration job: every release points it at the release's image, size, timeout, variables and egress.
locals {
  job_bucket_grants = { for grant in flatten([for job, spec in var.jobs : [for bucket, access in spec.buckets : {
    key = "${job} ${bucket}", job = job, bucket = bucket, role = access == "write" ? "roles/storage.objectUser" : "roles/storage.objectViewer"
  }]]) : grant.key => grant }

  job_secret_grants = { for grant in flatten([for job, spec in var.jobs : [for secret in spec.secrets : { key = "${job} ${secret}", job = job, secret = secret }]]) : grant.key => grant }
  job_models        = { for job, spec in var.jobs : job => spec if length(spec.models) > 0 }

  # Who acts on the jobs: the service starts, cancels and reads them; the controller's guard cancels a run beyond the
  # contract's limits (D70); the cap cancels every running one at the budget line.
  job_guards = { for pair in setproduct(keys(var.jobs), var.job_guard_role != "" ? var.controller_members : []) : "${pair[0]} ${pair[1]}" => { job = pair[0], member = pair[1] } }
}

resource "google_service_account" "job" {
  for_each     = var.jobs
  account_id   = "${var.service}-${each.key}"
  display_name = "Job ${each.key} of ${var.service}"
}
resource "google_cloud_run_v2_job" "job" {
  for_each            = var.jobs
  name                = "${var.service}-${each.key}"
  location            = var.region
  deletion_protection = false
  labels              = merge(local.labels, { lab-deploy-job = each.key })
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = google_service_account.job[each.key].email
      max_retries     = 0
      timeout         = "${each.value.timeout_minutes * 60}s"
      containers {
        image = var.bootstrap_image
        resources {
          limits = { cpu = tostring(each.value.cpu), memory = "${each.value.memory_mib}Mi" }
        }
      }
    }
  }
  lifecycle {
    ignore_changes = [template, client, client_version]
  }
}

# The service may start its jobs with per-run arguments, cancel them and read their executions; on its own jobs only.
# A run with overrides cannot change a job's image, identity, size or network, so acting as the job's identity gives the
# service nothing it could misuse; Cloud Run's documentation names that permission for creating and updating a job only.
resource "google_cloud_run_v2_job_iam_member" "job_runtime" {
  for_each = var.jobs
  location = var.region
  name     = google_cloud_run_v2_job.job[each.key].name
  role     = "roles/run.jobsExecutorWithOverrides"
  member   = local.runtime
}
resource "google_cloud_run_v2_job_iam_member" "job_runtime_viewer" {
  for_each = var.jobs
  location = var.region
  name     = google_cloud_run_v2_job.job[each.key].name
  role     = "roles/run.viewer"
  member   = local.runtime
}
resource "google_service_account_iam_member" "job_runtime_act_as" {
  for_each           = var.jobs
  service_account_id = google_service_account.job[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = local.runtime
}
# The deployer points each job at its release's image (docs/ROADMAP.md 27.5), so it may update the job and act as its identity.
resource "google_cloud_run_v2_job_iam_member" "job_deployer" {
  for_each = var.jobs
  location = var.region
  name     = google_cloud_run_v2_job.job[each.key].name
  role     = "roles/run.developer"
  member   = local.deployer
}
resource "google_service_account_iam_member" "job_deployer_act_as" {
  for_each           = var.jobs
  service_account_id = google_service_account.job[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = local.deployer
}
resource "google_cloud_run_v2_job_iam_member" "job_guard" {
  for_each = local.job_guards
  location = var.region
  name     = google_cloud_run_v2_job.job[each.value.job].name
  role     = var.job_guard_role
  member   = each.value.member
}
resource "google_cloud_run_v2_job_iam_member" "job_cap" {
  for_each = var.cap_service_account != "" ? var.jobs : {}
  location = var.region
  name     = google_cloud_run_v2_job.job[each.key].name
  role     = var.cap_operator_role
  member   = "serviceAccount:${var.cap_service_account}"
}

# What each job was given, and nothing else.
resource "google_storage_bucket_iam_member" "job_bucket" {
  for_each = local.job_bucket_grants
  bucket   = google_storage_bucket.need[each.value.bucket].name
  role     = each.value.role
  member   = "serviceAccount:${google_service_account.job[each.value.job].email}"
}
resource "google_project_iam_member" "job_models" {
  for_each = local.job_models
  project  = var.project_id
  role     = var.model_user_role
  member   = "serviceAccount:${google_service_account.job[each.key].email}"
  lifecycle {
    precondition {
      condition     = var.model_user_role != ""
      error_message = "A job's models need the foundation's model role (labDeployModelUser): apply the foundation first (scripts/apply-foundation.sh), then plan this root again."
    }
  }
}
resource "google_project_iam_member" "job_secret" {
  for_each = local.job_secret_grants
  project  = var.project_id
  role     = "roles/secretmanager.secretAccessor"
  member   = "serviceAccount:${google_service_account.job[each.value.job].email}"
  condition {
    title       = "${var.service} ${each.value.job}: ${each.value.secret}"
    description = "The one secret the job ${each.value.job} names in platform.yaml"
    expression  = "resource.name.startsWith(\"projects/${var.project_number}/secrets/${var.service}-${each.value.secret}/\") || resource.name == \"projects/${var.project_number}/secrets/${var.service}-${each.value.secret}\""
  }
}

# The platform's clock (D66): the controller mints an identity token for this account and sends POST wake.path to the
# service, through IAP for lab-members as the release probe does. Nothing else may use the account.
resource "google_service_account" "wake" {
  count        = var.wake ? 1 : 0
  account_id   = "${var.service}-wake"
  display_name = "The platform's clock for ${var.service}"
}
resource "google_cloud_run_v2_service_iam_member" "wake" {
  count    = var.wake ? 1 : 0
  location = var.region
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.wake[0].email}"
}
resource "google_iap_web_cloud_run_service_iam_member" "wake" {
  count                  = var.wake && local.pages_for_people ? 1 : 0
  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.app.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = "serviceAccount:${google_service_account.wake[0].email}"
}
resource "google_service_account_iam_member" "wake_token" {
  for_each           = var.wake ? toset(var.controller_members) : toset([])
  service_account_id = google_service_account.wake[0].name
  role               = "roles/iam.serviceAccountOpenIdTokenCreator"
  member             = each.value
}
