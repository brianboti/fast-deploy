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
variable "state_bucket" {
  type        = string
  default     = ""
  description = "GCS bucket holding every Terraform state of this project; empty means <project_id>-tfstate, the bootstrap convention."
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
  description = "At `threshold` of the budget the cap workflow closes ingress on every managed service; with detach_billing it also detaches billing at detach_threshold. Reopen a service with: gcloud run services update SERVICE --ingress=all"
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

variable "observer_impersonators" {
  type        = list(string)
  default     = []
  description = "Principals allowed to impersonate the viewer-only observer identity, for example [\"user:you@example.edu\"] when the controller runs on a laptop."
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

# The infrastructure pipeline: GitHub Actions in the platform repository plans every application root on a pull request
# with a read-only identity and applies it on merge with a write identity (docs/SETUP.md 7f). null disables it.
variable "pipeline" {
  type = object({
    platform_repository    = string
    platform_repository_id = string
    platform_owner_id      = string
    environment            = optional(string, "infra")
  })
  default     = null
  description = "The platform repository (owner/name with its numeric repository and owner ids) whose Actions may plan and apply application roots, and the GitHub environment that applies."
}
