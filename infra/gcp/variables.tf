variable "project_id" {
  type = string
}
variable "region" {
  type    = string
  default = "us-central1"
  validation {
    condition     = var.region == "us-central1"
    error_message = "MVP policy allows us-central1 only."
  }
}
variable "service" {
  type    = string
  default = "lab-demo"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.service))
    error_message = "Use a lowercase service name between 3 and 21 characters."
  }
}
variable "github_repository" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Expected owner/repository."
  }
}
variable "github_repository_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]+$", var.github_repository_id))
    error_message = "Use the immutable numeric repository ID."
  }
}
variable "github_owner_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]+$", var.github_owner_id))
    error_message = "Use the immutable numeric owner ID."
  }
}
variable "bootstrap_image" {
  type        = string
  description = "Reviewed working HTTP container listening on 8080, pinned to digest."
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.bootstrap_image))
    error_message = "Pin the approved bootstrap image to sha256 digest."
  }
}

# Cost controls (0.2.0)
variable "image_versions_to_keep" {
  type        = number
  default     = 10
  description = "Newest image versions always kept in Artifact Registry as rollback targets."
  validation {
    condition     = var.image_versions_to_keep >= 3 && var.image_versions_to_keep <= 100
    error_message = "Keep between 3 and 100 image versions."
  }
}
variable "image_retention_days" {
  type        = number
  default     = 180
  description = "Tagged images older than this are deleted unless protected by image_versions_to_keep."
  validation {
    condition     = var.image_retention_days >= 30
    error_message = "Retain tagged images for at least 30 days."
  }
}
variable "registry_cleanup_dry_run" {
  type        = bool
  default     = true
  description = "Dry run first: Artifact Registry logs what it would delete. Set to false after reviewing those logs."
}
variable "billing_account" {
  type        = string
  default     = ""
  description = "Billing account ID (XXXXXX-XXXXXX-XXXXXX) that owns the project; required only when monthly_budget_usd is set."
  validation {
    condition     = var.billing_account == "" || can(regex("^[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}$", var.billing_account))
    error_message = "Use the billing account ID in the form XXXXXX-XXXXXX-XXXXXX."
  }
}
variable "monthly_budget_usd" {
  type        = number
  default     = 0
  description = "Monthly budget alert for this project in USD; 0 disables the budget resource. Alerts notify, they do not stop spending."
  validation {
    condition     = var.monthly_budget_usd >= 0 && floor(var.monthly_budget_usd) == var.monthly_budget_usd
    error_message = "Use a whole, non-negative number of US dollars."
  }
}
variable "budget_alert_email" {
  type        = string
  default     = ""
  description = "Optional email address for budget threshold notifications in addition to billing administrators."
}
variable "runtime_alerts" {
  type        = bool
  default     = true
  description = "Create the authenticated uptime check, the alert policies and the dashboard for the service."
}
variable "health_path" {
  type        = string
  default     = "/health"
  description = "Health path probed by the uptime check; keep it equal to health_path in platform.yaml."
  validation {
    condition     = can(regex("^/[A-Za-z0-9/_-]*$", var.health_path))
    error_message = "health_path must start with / and contain only letters, digits, /, _ and -."
  }
}
variable "alert_email" {
  type        = string
  default     = ""
  description = "Email address for runtime alerts (health, errors, latency, saturation); empty disables email."
}
variable "alert_webhook_url" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional Slack or Discord incoming webhook URL for runtime alerts; empty disables the webhook channel."
}
variable "error_alert_count" {
  type        = number
  default     = 5
  description = "5xx responses within five minutes that trigger the error alert."
}
variable "latency_alert_ms" {
  type        = number
  default     = 2000
  description = "p95 request latency in milliseconds, sustained for five minutes, that triggers the latency alert."
}
variable "instance_alert_count" {
  type        = number
  default     = 3
  description = "Active instances that trigger the saturation alert; the staging policy caps revisions at 3."
}
variable "image_vulnerability_scanning" {
  type        = bool
  default     = true
  description = "Scan each newly pushed image digest for known vulnerabilities with Artifact Analysis (about USD 0.26 per new digest)."
}
