# The controller's own machine (docs/ROADMAP.md D36, Phase 15): one Always Free e2-micro in us-central1 with a 30 GB
# standard disk (both inside Google's free tier), Caddy with Let's Encrypt, the controller and Litestream as systemd
# services. Claude reaches it at its own reserved address (a Let's Encrypt certificate for the address itself, six days
# long and renewed by Caddy every few days), or at a DNS name when `domain` is set. What it costs: the external IPv4 address, $0.005 an hour (about $3.65 a month), plus a
# few cents of Secret Manager and Cloud Storage operations. Nothing here exists until `controller` is set in tfvars.
#
# The machine carries its own identity, not the observer's: the observer's viewer roles, plus the two things only the
# machine needs (its own folder in the backup bucket and the GitHub App's key), so impersonating the observer from a
# laptop never reaches the key. It has no key file; Google gives it short-lived tokens through the metadata server.
locals {
  controller_enabled    = var.controller != null
  controller_tag        = "fast-deploy-controller"
  controller_repository = local.controller_enabled ? (var.controller.repository != "" ? var.controller.repository : (var.pipeline != null ? var.pipeline.platform_repository : "")) : ""
  # Where Claude reaches it: the DNS name when one is given, else the machine's own reserved address.
  controller_host = local.controller_enabled ? (var.controller.domain != "" ? var.controller.domain : google_compute_address.controller[0].address) : ""
  # Starts with an uppercase letter: application runtime identities read secrets by the lowercase prefix "<service>-"
  # (modules/service, secret_accessor), so no application's grant can ever reach this one.
  github_app_key_secret = "FAST_DEPLOY_GITHUB_APP_KEY"
}

resource "google_project_service" "controller_dns" {
  count              = local.controller_enabled && try(var.controller.dns_zone, "") != "" && try(var.controller.domain, "") != "" ? 1 : 0
  service            = "dns.googleapis.com"
  disable_on_destroy = false
}

# A network of its own: the applications' network (network.tf) has no route out for code nobody has reviewed, and
# nothing running there should be able to reach the controller other than over the internet like anyone else.
resource "google_compute_network" "controller" {
  count                   = local.controller_enabled ? 1 : 0
  name                    = "fast-deploy-controller"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.network_apis]
}
resource "google_compute_subnetwork" "controller" {
  count         = local.controller_enabled ? 1 : 0
  name          = "fast-deploy-controller"
  region        = var.region
  network       = google_compute_network.controller[0].id
  ip_cidr_range = "10.9.0.0/28"
}
# HTTPS for Claude and the sign-in page (Claude's connectors call from Anthropic's addresses over IPv4; the browser
# sign-in comes from wherever the operator is), and HTTP only so Caddy can answer Let's Encrypt and redirect to HTTPS.
resource "google_compute_firewall" "controller_web" {
  count         = local.controller_enabled ? 1 : 0
  name          = "fast-deploy-controller-web"
  network       = google_compute_network.controller[0].id
  direction     = "INGRESS"
  source_ranges = ["0.0.0.0/0"]
  target_tags   = [local.controller_tag]
  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
}
# SSH only through Identity-Aware Proxy's TCP forwarding (Google's fixed range), with OS Login: no open port 22, no keys
# in metadata. `gcloud compute ssh fast-deploy --tunnel-through-iap` (scripts/machine.sh) is the way in.
resource "google_compute_firewall" "controller_ssh" {
  count         = local.controller_enabled ? 1 : 0
  name          = "fast-deploy-controller-iap-ssh"
  network       = google_compute_network.controller[0].id
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = [local.controller_tag]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}
# A reserved address, so the DNS record survives a replaced machine. In use it costs what an ephemeral one does; left
# unattached (a deleted machine) it costs twice that, so retire it with the machine.
resource "google_compute_address" "controller" {
  count        = local.controller_enabled ? 1 : 0
  name         = "fast-deploy-controller"
  region       = var.region
  address_type = "EXTERNAL"
  network_tier = "PREMIUM"
  depends_on   = [google_project_service.network_apis]
}

resource "google_service_account" "controller" {
  count        = local.controller_enabled ? 1 : 0
  account_id   = "lab-deploy-controller"
  display_name = "Fast Deploy controller machine"
  depends_on   = [google_project_service.apis]
}
resource "google_project_iam_member" "controller_roles" {
  for_each = local.controller_enabled ? toset(local.observer_roles) : toset([])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.controller[0].email}"
}
# The machine's own folder of the backup bucket: its nightly copies, its configuration, the Litestream replica and the
# commit it runs. Listing is limited to names in that folder too (gcloud lists a folder by its name without the slash,
# `machine`, so the prefix is matched without it), and the laptop's copies (controller/) stay out of its reach.
resource "google_storage_bucket_iam_member" "controller_backups" {
  count  = local.controller_enabled ? 1 : 0
  bucket = google_storage_bucket.backups.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.controller[0].email}"
  condition {
    title       = "The machine's own folder"
    description = "Objects under machine/, and listing them"
    expression  = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.backups.name}/objects/machine/\") || (resource.type == \"storage.googleapis.com/Bucket\" && api.getAttribute(\"storage.googleapis.com/objectListPrefix\", \"\").startsWith(\"machine\"))"
  }
}

# Google's guest agent and the startup script write their own lines to Cloud Logging (scripts/machine.sh logs startup
# reads the same on the machine); without this they retry and fill the serial console with refusals. Inside Logging's
# free allotment. The controller's own log stays on the machine.
resource "google_project_iam_member" "controller_log_writer" {
  count   = local.controller_enabled ? 1 : 0
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.controller[0].email}"
}

# The GitHub App's private key (docs/SETUP.md section 4). Terraform makes the empty secret; the operator adds the key as
# its version with gcloud, so the value never passes through Terraform, its state or this repository.
resource "google_secret_manager_secret" "github_app_key" {
  count     = local.controller_enabled ? 1 : 0
  secret_id = local.github_app_key_secret
  labels    = { lab-deploy-managed = "true" }
  replication {
    auto {}
  }
  depends_on = [google_project_service.secrets]
}
resource "google_secret_manager_secret_iam_member" "controller_github_app_key" {
  count     = local.controller_enabled ? 1 : 0
  secret_id = google_secret_manager_secret.github_app_key[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.controller[0].email}"
}

resource "google_compute_instance" "controller" {
  count        = local.controller_enabled ? 1 : 0
  name         = "fast-deploy"
  zone         = var.controller.zone
  machine_type = "e2-micro" # the Always Free shape: one per billing account, in us-west1, us-central1 or us-east1
  tags         = [local.controller_tag]
  labels       = { lab-deploy-managed = "true", role = "controller" }
  # A metadata change (the startup script, the commit for a fresh disk) applies in place at the next boot.
  allow_stopping_for_update = true
  boot_disk {
    auto_delete = true
    initialize_params {
      image  = "debian-cloud/debian-13"
      size   = 30
      type   = "pd-standard" # the free tier covers 30 GB-months of standard disk, not balanced or SSD
      labels = { lab-deploy-managed = "true" }
    }
  }
  network_interface {
    subnetwork = google_compute_subnetwork.controller[0].id
    access_config {
      nat_ip       = google_compute_address.controller[0].address
      network_tier = "PREMIUM"
    }
  }
  service_account {
    email  = google_service_account.controller[0].email
    scopes = ["cloud-platform"] # what it may do is decided by its roles above, not by scopes
  }
  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }
  scheduling {
    automatic_restart   = true # back after a host failure
    on_host_maintenance = "MIGRATE"
    preemptible         = false
    provisioning_model  = "STANDARD"
  }
  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
    serial-port-enable     = "FALSE"
    startup-script         = file("${path.module}/controller-startup.sh")
    fast-deploy-host       = local.controller_host
    fast-deploy-repository = local.controller_repository
    fast-deploy-commit     = var.controller.install_commit
    fast-deploy-bucket     = google_storage_bucket.backups.name
    fast-deploy-acme-email = var.controller.acme_email
  }
  lifecycle {
    # The image family moves to a new image every few weeks; the machine updates itself (unattended upgrades) instead of
    # being replaced on the next plan.
    ignore_changes = [boot_disk[0].initialize_params[0].image]
    precondition {
      condition     = local.controller_repository != ""
      error_message = "Set controller.repository (owner/name of the platform repository) when the pipeline is not configured."
    }
  }
  depends_on = [google_project_iam_member.controller_roles, google_storage_bucket_iam_member.controller_backups, google_secret_manager_secret_iam_member.controller_github_app_key]
}

# The A record, when a DNS name is used and it lives in a Cloud DNS zone of this project; otherwise set it at the
# registrar to the address in the `controller` output. Without a name there is nothing to set.
resource "google_dns_record_set" "controller" {
  count        = local.controller_enabled && try(var.controller.dns_zone, "") != "" && try(var.controller.domain, "") != "" ? 1 : 0
  managed_zone = var.controller.dns_zone
  name         = "${var.controller.domain}."
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_address.controller[0].address]
  depends_on   = [google_project_service.controller_dns]
}

# Reachability (docs/ROADMAP.md 15.4): /healthz every minute from three US locations; an alert when more than one fails
# for five minutes, beside the GitHub ping (.github/workflows/controller-watch.yml). Inside Monitoring's free allotment.
resource "google_monitoring_uptime_check_config" "controller" {
  count            = local.controller_enabled ? 1 : 0
  display_name     = "Fast Deploy controller"
  timeout          = "10s"
  period           = "60s"
  selected_regions = ["USA_OREGON", "USA_IOWA", "USA_VIRGINIA"]
  http_check {
    path           = "/healthz"
    port           = 443
    use_ssl        = true
    validate_ssl   = true # an expired or wrong certificate counts as down (a six-day one that failed to renew): Claude would refuse it too
    request_method = "GET"
  }
  content_matchers {
    content = "\"status\":\"ok\""
    matcher = "CONTAINS_STRING"
  }
  monitored_resource {
    type   = "uptime_url"
    labels = { project_id = var.project_id, host = local.controller_host }
  }
  depends_on = [google_project_service.apis]
}
resource "google_monitoring_alert_policy" "controller" {
  count        = local.controller_enabled ? 1 : 0
  display_name = "Fast Deploy controller: not answering"
  combiner     = "OR"
  conditions {
    display_name = "/healthz failed from more than one location"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.controller[0].uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "300s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.*"]
      }
      trigger {
        count = 1
      }
    }
  }
  severity    = "CRITICAL"
  user_labels = { lab-deploy-managed = "true", lab-deploy-environment = var.environment }
  documentation {
    subject   = "Fast Deploy controller: not answering"
    content   = "https://${local.controller_host}/healthz has not answered for five minutes, so Claude's tools are down. Releases already running finish on GitHub. docs/RUNBOOK.md, \"The controller's machine\": `scripts/machine.sh status`."
    mime_type = "text/markdown"
  }
  notification_channels = concat(google_monitoring_notification_channel.alert_email[*].id, google_monitoring_notification_channel.alert_webhook[*].id)
  alert_strategy {
    auto_close = "1800s"
  }
}
