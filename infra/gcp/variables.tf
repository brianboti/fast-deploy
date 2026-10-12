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
variable "services" {
  description = "One entry per application, keyed by its Cloud Run service name (3 to 21 lowercase characters)."
  type = map(object({
    github_repository        = string
    github_repository_id     = string
    github_owner_id          = string
    bootstrap_image          = string
    health_path              = optional(string, "/health")
    image_versions_to_keep   = optional(number, 10)
    image_retention_days     = optional(number, 180)
    registry_cleanup_dry_run = optional(bool, true)
    runtime_alerts           = optional(bool, true)
    error_alert_count        = optional(number, 5)
    latency_alert_ms         = optional(number, 2000)
    instance_alert_count     = optional(number, 3)
  }))
  validation {
    condition     = alltrue([for name in keys(var.services) : can(regex("^[a-z][a-z0-9-]{2,20}$", name))])
    error_message = "Service names must be lowercase, 3 to 21 characters, letters, digits and dashes."
  }
}

# Project-wide cost controls
variable "image_vulnerability_scanning" {
  type        = bool
  default     = true
  description = "Scan each newly pushed image digest with Artifact Analysis (about USD 0.26 per new digest). A cost choice for staging."
}
variable "billing_account" {
  type        = string
  default     = ""
  description = "Billing account ID (XXXXXX-XXXXXX-XXXXXX) that owns the project; required only when monthly_budget_usd is set."
}
variable "monthly_budget_usd" {
  type        = number
  default     = 0
  description = "Monthly budget for this project in USD; 0 disables the budget and the spending cap. Alerts notify; the cap acts."
}
variable "budget_alert_email" {
  type        = string
  default     = ""
  description = "Optional email address for budget threshold notifications in addition to billing administrators."
}
variable "spending_cap" {
  description = "At `threshold` of the budget the cap workflow closes ingress on every service here; with detach_billing it also detaches billing at detach_threshold. Reopen a service with: gcloud run services update SERVICE --ingress=all"
  type = object({
    enabled          = optional(bool, true)
    threshold        = optional(number, 1.0)
    detach_billing   = optional(bool, false)
    detach_threshold = optional(number, 1.2)
  })
  default = {}
}
variable "billing_export_dataset" {
  type        = string
  default     = ""
  description = "BigQuery dataset id for the Cloud Billing export; create it here, then point the export at it in the Billing console. Empty creates nothing."
}

# Runtime alert channels shared by every application
variable "alert_email" {
  type        = string
  default     = ""
  description = "Email address for runtime alerts (health, errors, latency, saturation, memory, log errors); empty disables email."
}
variable "alert_webhook_url" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional Slack or Discord incoming webhook URL for runtime alerts; empty disables the webhook channel."
}
