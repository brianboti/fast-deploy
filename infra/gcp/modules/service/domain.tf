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

# The domain's own uptime check (docs/ROADMAP.md 26.4): the service's check calls the run.app address, so a certificate
# that failed to renew or a DNS record that moved would go unseen while run.app still answers. This one calls the domain
# through the balancer and validates its certificate. Any HTTP answer passes, a sign-in redirect or a 403 included: the
# service checks who calls behind the balancer, and what is proven here is the name, the certificate and the balancer.
# Right after the domain is added it fails until the A record points at domain_address and Google has issued the
# certificate, usually within an hour. Its executions count against the free allotment of a million a month.
resource "google_monitoring_uptime_check_config" "domain" {
  count            = local.domain_enabled && var.runtime_alerts ? 1 : 0
  display_name     = "${var.service} domain ${var.domain}"
  timeout          = "10s"
  period           = var.uptime_period
  selected_regions = length(var.uptime_regions) > 0 ? var.uptime_regions : null
  http_check {
    path           = var.health_path
    port           = 443
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"
    accepted_response_status_codes {
      status_class = "STATUS_CLASS_ANY"
    }
  }
  monitored_resource {
    type   = "uptime_url"
    labels = { project_id = var.project_id, host = var.domain }
  }
}
resource "google_monitoring_alert_policy" "domain" {
  count        = local.domain_enabled && var.runtime_alerts ? 1 : 0
  display_name = "${var.service}: ${var.domain} unreachable"
  combiner     = "OR"
  conditions {
    display_name = "the domain failed its check from more than one region for ten minutes"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.domain[0].uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "600s"
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
  conditions {
    # Google renews a managed certificate about 30 days before it expires; under 14 days left, renewal has failed.
    display_name = "the domain's certificate expires in less than 14 days"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/time_until_ssl_cert_expires\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.domain[0].uptime_check_id}\""
      comparison      = "COMPARISON_LT"
      threshold_value = 14
      duration        = "600s"
      aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_MIN"
        group_by_fields      = ["resource.label.*"]
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
    subject   = "[${local.environment_name}] ${var.service}: ${var.domain} unreachable"
    content   = "${var.domain} did not answer through its load balancer, or its certificate is close to expiring, while ${var.service}'s run.app address may still answer. Check that the domain's A record still points at the root's output domain_address and the managed certificate's status (docs/RUNBOOK.md, \"A custom domain\"). Right after the domain is added this fires until the record points there and the certificate is issued."
    mime_type = "text/markdown"
  }
  alert_strategy {
    auto_close = "1800s"
  }
}
