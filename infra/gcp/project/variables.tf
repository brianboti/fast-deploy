variable "project_id" {
  type = string
}
variable "environment" {
  type        = string
  default     = "staging"
  description = "staging or production: production is a project of its own with its own foundation and state (docs/ROADMAP.md D37)."
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment is staging or production."
  }
}
variable "viewer_members" {
  type        = list(string)
  default     = []
  description = "Service accounts of another project that view this one with the observer's roles; in production, the controller machine's identity in staging."
  validation {
    condition     = alltrue([for member in var.viewer_members : can(regex("^serviceAccount:[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9][.]iam[.]gserviceaccount[.]com$", member))])
    error_message = "viewer_members are serviceAccount:NAME@PROJECT.iam.gserviceaccount.com."
  }
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
variable "access_change_quiet_members" {
  type        = list(string)
  default     = []
  description = "Email addresses whose own IAM changes do not raise the access-change alert (docs/ROADMAP.md 25.6), such as the operator who applies the foundation."
}
variable "binary_authorization" {
  type        = string
  default     = "dryrun"
  description = "Binary Authorization on Cloud Run (docs/ROADMAP.md 14.8): dryrun logs a deploy of an image no release attested, enforced refuses it, off allows every image. Enforce once a few releases have run clean in dryrun (docs/RUNBOOK.md, \"Binary Authorization\")."
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
    former_repositories    = optional(list(string), []) # owner/name before a rename, accepted while workflows move
  })
  default     = null
  description = "The platform repository (owner/name with its numeric repository and owner ids) whose Actions may plan and apply application roots, and the GitHub environment that applies."
}
variable "iap_client_id" {
  type        = string
  default     = ""
  description = "The project's IAP OAuth client id (not the secret), created once in the console for pages for people; empty until then."
}

# The controller's own machine (docs/ROADMAP.md D36, Phase 15): null keeps the controller on a laptop.
variable "controller" {
  type = object({
    install_commit = string               # the platform commit a fresh disk installs when the bucket names none
    domain         = optional(string, "") # a DNS name for the controller (D6); empty: the machine's own address, no name needed
    repository     = optional(string, "") # owner/name of the platform repository; default: pipeline.platform_repository
    zone           = optional(string, "us-central1-a")
    acme_email     = optional(string, "") # Let's Encrypt's contact for certificate notices; empty sends none
    dns_zone       = optional(string, "") # a Cloud DNS zone of this project for the A record; empty: set it at the registrar
  })
  default     = null
  description = "One Always Free e2-micro running the controller behind Caddy, with Litestream, an uptime check and its alert."
  validation {
    condition = var.controller == null ? true : (
      (var.controller.domain == "" || can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$", var.controller.domain))) &&
      can(regex("^[0-9a-f]{40}$", var.controller.install_commit)) &&
      can(regex("^us-central1-[a-f]$", var.controller.zone)) &&
      (var.controller.repository == "" || can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.controller.repository))) &&
      (var.controller.dns_zone == "" || (var.controller.domain != "" && can(regex("^[a-z][a-z0-9-]{0,62}$", var.controller.dns_zone))))
    )
    error_message = "controller: install_commit a full 40-character commit; domain empty (the machine's own address) or a lowercase DNS name; zone one of us-central1-a to -f (the free tier's region); repository owner/name; dns_zone a Cloud DNS zone name, only with a domain."
  }
}
