# Instantiate in a dedicated worker project, never in the controller or application project.
variable "project_id" { type = string }
variable "controller_project_id" { type = string }
variable "controller_member" { type = string }
variable "region" { type = string }
variable "proxy_boot_image" {
  type = string
  validation {
    condition     = can(regex("^projects/debian-cloud/global/images/debian-13-[a-z0-9-]+$", var.proxy_boot_image))
    error_message = "Review and pin an exact Debian 13 image, not an image family."
  }
}
variable "images" {
  type = map(string)
  validation {
    condition     = alltrue([for mode, image in var.images : can(regex("^(python-3\\.[0-9]+-(compile|export|export-dev)|node-[0-9]+-lock)$", mode)) && can(regex("@sha256:[a-f0-9]{64}$", image))])
    error_message = "Provide reviewed resolver modes and immutable images."
  }
}
resource "google_project_service" "api" {
  for_each           = toset(["run.googleapis.com", "compute.googleapis.com", "iamcredentials.googleapis.com", "storage.googleapis.com"])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
  lifecycle {
    precondition {
      condition     = var.project_id != var.controller_project_id
      error_message = "Dependency workers must have their own project."
    }
  }
}
resource "google_compute_network" "worker" {
  project                 = var.project_id
  name                    = "dependency-workers"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.api]
}
resource "google_compute_subnetwork" "worker" {
  project       = var.project_id
  region        = var.region
  name          = "dependency-workers"
  network       = google_compute_network.worker.id
  ip_cidr_range = "10.89.0.0/24"
}
resource "google_service_account" "worker" {
  project    = var.project_id
  account_id = "dependency-worker"
  # Deliberately no project/resource IAM grants. Do not trust this identity at privileged endpoints.
}
resource "google_service_account" "proxy" {
  project    = var.project_id
  account_id = "dependency-proxy"
}
resource "google_service_account" "signer" {
  project    = var.project_id
  account_id = "dependency-transfers"
}
resource "google_storage_bucket" "transfers" {
  project                     = var.project_id
  name                        = "${var.project_id}-dependency-transfers"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  lifecycle_rule {
    action { type = "Delete" }
    condition { age = 1 }
  }
}
resource "google_storage_bucket_iam_member" "controller" {
  bucket = google_storage_bucket.transfers.name
  role   = "roles/storage.objectUser"
  member = var.controller_member
}
resource "google_storage_bucket_iam_member" "signer_read" {
  bucket = google_storage_bucket.transfers.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.signer.email}"
  condition {
    title      = "Input objects only"
    expression = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.transfers.name}/objects/inputs/')"
  }
}
resource "google_storage_bucket_iam_member" "signer_write" {
  bucket = google_storage_bucket.transfers.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.signer.email}"
  condition {
    title      = "New output objects only"
    expression = "resource.name.startsWith('projects/_/buckets/${google_storage_bucket.transfers.name}/objects/outputs/')"
  }
}
resource "google_project_iam_custom_role" "sign" {
  project     = var.project_id
  role_id     = "dependencyTransferSign"
  title       = "Sign dependency transfer URLs"
  permissions = ["iam.serviceAccounts.signBlob"]
}
resource "google_service_account_iam_member" "sign" {
  service_account_id = google_service_account.signer.name
  role               = google_project_iam_custom_role.sign.name
  member             = var.controller_member
}
resource "google_compute_instance" "proxy" {
  project             = var.project_id
  zone                = "${var.region}-a"
  name                = "dependency-proxy"
  machine_type        = "e2-micro"
  deletion_protection = true
  tags                = ["resolver-proxy"]
  boot_disk {
    initialize_params {
      image = var.proxy_boot_image
      size  = 10
    }
  }
  network_interface {
    subnetwork = google_compute_subnetwork.worker.id
    network_ip = "10.89.0.10"
    access_config {}
  }
  service_account {
    email  = google_service_account.proxy.email
    scopes = []
  }
  metadata_startup_script = file("${path.module}/proxy-startup.sh")
  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }
}
resource "google_compute_firewall" "proxy_ingress" {
  project       = var.project_id
  name          = "resolver-to-proxy"
  network       = google_compute_network.worker.name
  source_ranges = [google_compute_subnetwork.worker.ip_cidr_range]
  target_tags   = ["resolver-proxy"]
  allow {
    protocol = "tcp"
    ports    = ["3128"]
  }
}
resource "google_compute_firewall" "worker_proxy" {
  project            = var.project_id
  name               = "resolver-only-proxy"
  network            = google_compute_network.worker.name
  direction          = "EGRESS"
  priority           = 900
  destination_ranges = ["10.89.0.10/32"]
  target_tags        = ["resolver-worker"]
  allow {
    protocol = "tcp"
    ports    = ["3128"]
  }
}
# A firewall rule takes ranges of one address family only, so IPv4 and IPv6 are denied by two rules.
resource "google_compute_firewall" "worker_deny" {
  project            = var.project_id
  name               = "resolver-deny-direct-egress"
  network            = google_compute_network.worker.name
  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = ["0.0.0.0/0"]
  target_tags        = ["resolver-worker"]
  deny { protocol = "all" }
  log_config { metadata = "EXCLUDE_ALL_METADATA" }
}
resource "google_compute_firewall" "worker_deny_ipv6" {
  project            = var.project_id
  name               = "resolver-deny-direct-egress-ipv6"
  network            = google_compute_network.worker.name
  direction          = "EGRESS"
  priority           = 1000
  destination_ranges = ["::/0"]
  target_tags        = ["resolver-worker"]
  deny { protocol = "all" }
  log_config { metadata = "EXCLUDE_ALL_METADATA" }
}
resource "google_cloud_run_v2_job" "worker" {
  for_each            = var.images
  project             = var.project_id
  location            = var.region
  name                = "resolve-${replace(each.key, ".", "-")}"
  deletion_protection = true
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = google_service_account.worker.email
      max_retries     = 0
      timeout         = "900s"
      vpc_access {
        egress = "ALL_TRAFFIC"
        network_interfaces {
          network    = google_compute_network.worker.name
          subnetwork = google_compute_subnetwork.worker.name
          tags       = ["resolver-worker"]
        }
      }
      containers {
        image = each.value
        resources { limits = { cpu = "1", memory = "1Gi" } }
        env {
          name  = "HTTPS_PROXY"
          value = "http://10.89.0.10:3128"
        }
        env {
          name  = "HTTP_PROXY"
          value = "http://10.89.0.10:3128"
        }
      }
    }
  }
  depends_on = [google_compute_firewall.worker_deny, google_compute_firewall.worker_proxy]
}
resource "google_cloud_run_v2_job_iam_member" "launch" {
  for_each = var.images
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.worker[each.key].name
  role     = "roles/run.jobsExecutorWithOverrides"
  member   = var.controller_member
}
resource "google_project_iam_custom_role" "read" {
  project     = var.project_id
  role_id     = "dependencyExecutionRead"
  title       = "Observe dependency executions"
  permissions = ["run.jobs.get", "run.operations.get", "run.executions.get", "run.executions.list"]
}
resource "google_project_iam_member" "read" {
  project = var.project_id
  role    = google_project_iam_custom_role.read.name
  member  = var.controller_member
}
output "resolver_configuration" {
  value = {
    bucket = google_storage_bucket.transfers.name
    signer = google_service_account.signer.email
    workers = { for mode, image in var.images : mode => {
      job             = google_cloud_run_v2_job.worker[mode].id, image = image,
      service_account = google_service_account.worker.email,
      network         = google_compute_network.worker.name, subnet = google_compute_subnetwork.worker.name,
      proxy           = "http://10.89.0.10:3128"
    } }
  }
}
