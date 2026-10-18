variable "project_id" { type = string }
variable "environment" {
  type        = string
  default     = "staging"
  description = "staging or production (docs/ROADMAP.md D37): only names change with it; each environment is its own project and foundation."
}
variable "viewer_members" {
  type        = list(string)
  default     = []
  description = "Service accounts of another project (serviceAccount:EMAIL) that view this one with the observer's roles."
}
variable "project_number" { type = string }
variable "region" { type = string }
variable "state_bucket" {
  type        = string
  description = "GCS bucket that holds this project's Terraform states; the pipeline identities get read or write access to it."
}
variable "pipeline" {
  type = object({
    platform_repository    = string
    platform_repository_id = string
    platform_owner_id      = string
    environment            = optional(string, "infra")
    former_repositories    = optional(list(string), []) # owner/name before a rename, accepted while workflows move
  })
  default     = null
  description = "The platform repository allowed to plan and apply application roots through Workload Identity Federation; null creates no pipeline identities."
}
variable "image_vulnerability_scanning" { type = bool }
variable "billing_account" { type = string }
variable "monthly_budget_usd" { type = number }
variable "budget_alert_email" { type = string }
variable "alert_email" { type = string }
variable "alert_webhook_url" {
  type      = string
  sensitive = true
}
variable "spending_cap" {
  type = object({
    enabled          = optional(bool, true)
    threshold        = optional(number, 1.0)
    detach_billing   = optional(bool, false)
    detach_threshold = optional(number, 1.2)
  })
  description = "What happens at the budget line: close ingress on the platform's services at `threshold` of the budget; optionally detach billing from the project at `detach_threshold`."
}
variable "observer_impersonators" {
  type        = list(string)
  description = "Principals (user:… or serviceAccount:…) allowed to mint tokens for the viewer-only observer identity, for the controller on a laptop."
}
variable "controller" {
  type = object({
    install_commit = string
    domain         = optional(string, "")
    repository     = optional(string, "")
    zone           = optional(string, "us-central1-a")
    acme_email     = optional(string, "")
    dns_zone       = optional(string, "")
  })
  default     = null
  description = "The controller's own machine (controller.tf); null creates none."
}
variable "billing_export_dataset" {
  type        = string
  description = "BigQuery dataset id for the billing export (enable the export in the Billing console to this dataset); empty creates nothing."
}
