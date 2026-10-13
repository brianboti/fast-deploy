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
variable "cap_operator_role" {
  type        = string
  description = "The foundation's custom role that only reads and updates a service's ingress, granted to the cap on this service."
  default     = ""
}
variable "billing_dataset" {
  type        = string
  description = "BigQuery dataset id holding the billing export the deployer may read; empty disables."
  default     = ""
}
variable "needs" {
  description = "What the application declared under needs in platform.yaml: buckets, topics and a database the platform provisions."
  type = object({
    buckets  = optional(list(string), [])
    topics   = optional(list(string), [])
    database = optional(string, "none")
    migrate  = optional(list(string), [])
  })
  default = {}
  validation {
    condition     = contains(["none", "firestore", "postgres"], var.needs.database) && alltrue([for n in concat(var.needs.buckets, var.needs.topics) : can(regex("^[a-z][a-z0-9-]{1,20}$", n))])
    error_message = "needs: database must be none, firestore or postgres; bucket and topic names are lowercase, 2 to 21 characters."
  }
}
variable "network" {
  type        = string
  default     = ""
  description = "The foundation's private network id; a Cloud SQL instance gets its address there."
}
variable "subnet" {
  type        = string
  default     = ""
  description = "The foundation's subnet id for Cloud Run egress into that network."
}
variable "exposure" {
  type        = string
  default     = "authenticated"
  description = "authenticated: programs with identity tokens. lab-members: people through Identity-Aware Proxy, the listed viewers only."
  validation {
    condition     = contains(["authenticated", "lab-members"], var.exposure)
    error_message = "exposure must be authenticated or lab-members; public access is never an option."
  }
}
variable "viewers" {
  type        = list(string)
  default     = []
  description = "Google accounts (user:, group: or domain:) that may open the pages when exposure is lab-members."
}
variable "iap_client_id" {
  type        = string
  default     = ""
  description = "The project's IAP OAuth client id, published to the app's environment so the release probes can pass the proxy."
}
