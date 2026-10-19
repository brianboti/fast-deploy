# A custom domain (docs/ROADMAP.md D39, roadmap item 16.6), production only: a global external Application Load Balancer
# answers at the contract's `domain` with a Google-managed certificate, behind a Cloud Armor policy that slows down any
# one address. About $25 a month: the forwarding rules $18.25 (a project's first five global rules bill one minimum
# charge, so the HTTP redirect adds nothing), the policy $5 and its two rules $2; then $0.75 per million requests and
# $0.008 per GiB each way. The certificate is free. The operator points the domain's A record at the root's output
# domain_address, and Google issues the certificate once the name resolves there, usually within an hour.
#
# Who may call is unchanged: the service checks the same Google identity token (it also accepts the domain as the
# token's audience), or IAP for lab-members, which Cloud Run enforces on every way in. The run.app address keeps answering
# behind the same sign-in, because the release probes it; ingress is not narrowed to the balancer. The spending cap's
# closed ingress shuts the domain too, while the balancer's own charges go on until the domain is removed.
locals {
  domain_enabled = var.domain != "" && local.environment_name == "production"
  domain_name    = "${var.service}-domain"
  # Above this many requests a minute from one address, Cloud Armor answers 429 until the minute is over.
  domain_requests_per_minute = 600
}
resource "google_compute_global_address" "domain" {
  count  = local.domain_enabled ? 1 : 0
  name   = local.domain_name
  labels = local.labels
}
resource "google_compute_region_network_endpoint_group" "domain" {
  count                 = local.domain_enabled ? 1 : 0
  name                  = local.domain_name
  region                = var.region
  network_endpoint_type = "SERVERLESS"
  cloud_run {
    service = google_cloud_run_v2_service.app.name
  }
}
resource "google_compute_security_policy" "domain" {
  count       = local.domain_enabled ? 1 : 0
  name        = local.domain_name
  description = "Fast Deploy: ${var.service} at ${var.domain}"
  type        = "CLOUD_ARMOR"
  rule {
    action      = "throttle"
    priority    = 1000
    description = "at most ${local.domain_requests_per_minute} requests a minute from one address"
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = local.domain_requests_per_minute
        interval_sec = 60
      }
    }
  }
  rule {
    action      = "allow"
    priority    = 2147483647
    description = "everything else reaches the service, which checks who is calling"
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}
resource "google_compute_backend_service" "domain" {
  count                 = local.domain_enabled ? 1 : 0
  name                  = local.domain_name
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.domain[0].id
  backend {
    group = google_compute_region_network_endpoint_group.domain[0].id
  }
  # One request in ten, enough to see a pattern after an incident without a flood filling the log bucket.
  log_config {
    enable      = true
    sample_rate = 0.1
  }
}
resource "google_compute_url_map" "domain" {
  count           = local.domain_enabled ? 1 : 0
  name            = local.domain_name
  default_service = google_compute_backend_service.domain[0].id
}
# Named after the domain, so a new name gets its certificate before the old one goes.
resource "google_compute_managed_ssl_certificate" "domain" {
  count = local.domain_enabled ? 1 : 0
  name  = "${local.domain_name}-${substr(sha256(var.domain), 0, 8)}"
  managed {
    domains = [var.domain]
  }
  lifecycle {
    create_before_destroy = true
  }
}
resource "google_compute_ssl_policy" "domain" {
  count           = local.domain_enabled ? 1 : 0
  name            = local.domain_name
  profile         = "MODERN"
  min_tls_version = "TLS_1_2"
}
resource "google_compute_target_https_proxy" "domain" {
  count            = local.domain_enabled ? 1 : 0
  name             = local.domain_name
  url_map          = google_compute_url_map.domain[0].id
  ssl_certificates = [google_compute_managed_ssl_certificate.domain[0].id]
  ssl_policy       = google_compute_ssl_policy.domain[0].id
}
resource "google_compute_global_forwarding_rule" "domain_https" {
  count                 = local.domain_enabled ? 1 : 0
  name                  = "${local.domain_name}-https"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.domain[0].id
  port_range            = "443"
  target                = google_compute_target_https_proxy.domain[0].id
  labels                = local.labels
}
# Plain HTTP only redirects to HTTPS.
resource "google_compute_url_map" "domain_redirect" {
  count = local.domain_enabled ? 1 : 0
  name  = "${local.domain_name}-redirect"
  default_url_redirect {
    https_redirect = true
    strip_query    = false
  }
}
resource "google_compute_target_http_proxy" "domain_redirect" {
  count   = local.domain_enabled ? 1 : 0
  name    = "${local.domain_name}-redirect"
  url_map = google_compute_url_map.domain_redirect[0].id
}
resource "google_compute_global_forwarding_rule" "domain_http" {
  count                 = local.domain_enabled ? 1 : 0
  name                  = "${local.domain_name}-http"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.domain[0].id
  port_range            = "80"
  target                = google_compute_target_http_proxy.domain_redirect[0].id
  labels                = local.labels
}
