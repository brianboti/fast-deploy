variable "project_id" { type = string }
variable "project_number" { type = string }
variable "region" { type = string }
variable "services" {
  type        = list(string)
  description = "Service names the spending cap closes when the budget line is crossed."
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
variable "billing_export_dataset" {
  type        = string
  description = "BigQuery dataset id for the billing export (enable the export in the Billing console to this dataset); empty creates nothing."
}
