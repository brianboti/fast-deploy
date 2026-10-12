variable "project_id" { type = string }
variable "project_number" { type = string }
variable "region" { type = string }
variable "service" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.service))
    error_message = "service must be a short lowercase name."
  }
}
variable "github_repository" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must be owner/name."
  }
}
variable "github_repository_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]+$", var.github_repository_id))
    error_message = "github_repository_id must be the numeric repository id."
  }
}
variable "github_owner_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]+$", var.github_owner_id))
    error_message = "github_owner_id must be the numeric owner id."
  }
}
variable "bootstrap_image" {
  type        = string
  description = "Reviewed working HTTP container listening on 8080, pinned to digest."
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.bootstrap_image))
    error_message = "bootstrap_image must be pinned to a digest."
  }
}
variable "image_versions_to_keep" { type = number }
variable "image_retention_days" { type = number }
variable "registry_cleanup_dry_run" { type = bool }
variable "image_vulnerability_scanning" { type = bool }
variable "tag_manager_role" {
  type        = string
  description = "Project-level custom role that lets the deployer delete stale serving tags."
}
variable "runtime_alerts" { type = bool }
variable "health_path" {
  type = string
  validation {
    condition     = can(regex("^/[A-Za-z0-9/_-]*$", var.health_path))
    error_message = "health_path must start with / and contain only letters, digits, /, _ and -."
  }
}
variable "alert_channels" {
  type        = list(string)
  description = "Notification channel ids shared by every app in the project."
}
variable "error_alert_count" { type = number }
variable "latency_alert_ms" { type = number }
variable "instance_alert_count" { type = number }
variable "cap_service_account" {
  type        = string
  description = "Service account of the spending-cap workflow, allowed to close this service's ingress; empty disables."
  default     = ""
}
variable "billing_dataset" {
  type        = string
  description = "BigQuery dataset id holding the billing export the deployer may read; empty disables."
  default     = ""
}
