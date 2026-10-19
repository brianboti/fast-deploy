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
variable "health_probe" {
  type        = bool
  default     = true
  description = "false when the contract names no health route (health_path: null): the uptime check asks / and accepts any HTTP answer, since reaching the service is all it can prove."
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
    # iam: the runtime identity signs in, no password exists. password: a built-in user and the secret DATABASE_URL.
    database_auth = optional(string, "iam")
    # The postgres database in production: standard (one zone), ha (a standby in a second zone) or sla (a standby and a
    # dedicated CPU, the shape Google's SLA covers). Staging always runs the smallest (docs/ROADMAP.md D39).
    database_tier = optional(string, "standard")
    # Model APIs the app calls through Vertex AI (docs/ROADMAP.md D47): claude.
    models = optional(list(string), [])
  })
  default = {}
  validation {
    condition     = contains(["none", "firestore", "postgres"], var.needs.database) && alltrue([for n in concat(var.needs.buckets, var.needs.topics) : can(regex("^[a-z][a-z0-9-]{1,20}$", n))])
    error_message = "needs: database must be none, firestore or postgres; bucket and topic names are lowercase, 2 to 21 characters."
  }
  validation {
    condition     = contains(["iam", "password"], var.needs.database_auth) && (var.needs.database_auth == "iam" || var.needs.database == "postgres")
    error_message = "needs: database_auth is iam or password, and password applies to a postgres database only."
  }
  validation {
    condition     = contains(["standard", "ha", "sla"], var.needs.database_tier) && (var.needs.database_tier == "standard" || var.needs.database == "postgres")
    error_message = "needs: database_tier is standard, ha or sla, and applies to a postgres database only."
  }
  validation {
    condition     = alltrue([for m in var.needs.models : contains(["claude"], m)]) && length(distinct(var.needs.models)) == length(var.needs.models)
    error_message = "needs: models is a list of claude, each once."
  }
}
variable "model_user_role" {
  type        = string
  default     = ""
  description = "The foundation's custom role that may call a model through Vertex AI and nothing else; empty until the foundation has it."
  validation {
    condition     = var.model_user_role == "" || can(regex("^projects/[a-z][a-z0-9-]+/roles/labDeployModelUser$", var.model_user_role))
    error_message = "model_user_role is the foundation's labDeployModelUser role."
  }
}
variable "database_password" {
  type        = string
  default     = null
  ephemeral   = true
  description = "For database_auth = password only: generated by the root in every run and sent once through write-only arguments, never stored."
}
variable "github_environment" {
  type        = string
  default     = "staging"
  description = "The GitHub environment the app's workflows run in: staging, staging-<service> for a further application of the same repository, or production-<service> of the platform repository (its promotions, docs/ROADMAP.md D37)."
  validation {
    condition     = can(regex("^(staging(-[a-z][a-z0-9-]{1,19}[a-z0-9])?|production-[a-z][a-z0-9-]{1,19}[a-z0-9])$", var.github_environment))
    error_message = "github_environment is staging, staging-<service> or production-<service>."
  }
}
variable "workflow_files" {
  type        = list(string)
  default     = ["deploy-gcp.yml", "recover-gcp.yml", "rightsize-gcp.yml"]
  description = "The deploy, recover and rightsize workflow files the deploy identity trusts, in that order."
  validation {
    # The application's own files (deploy-gcp.yml, or suffixed by its service), the platform repository's pipeline
    # (deploy.yml, recover.yml, rightsize.yml) when it keeps the contract (docs/ROADMAP.md D33), or, for production, the
    # platform repository's promote.yml alone (D37).
    condition     = (length(var.workflow_files) == 3 && alltrue([for file in var.workflow_files : can(regex("^(deploy|recover|rightsize)(-gcp(-[a-z][a-z0-9-]{1,19}[a-z0-9])?)?[.]yml$", file))])) || (length(var.workflow_files) == 1 && join("", var.workflow_files) == "promote.yml")
    error_message = "workflow_files are the deploy, recover and rightsize workflow file names, or promote.yml alone for production."
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
variable "kms_key_ring" {
  type        = string
  default     = ""
  description = "The foundation's provenance key ring; empty until the foundation has one, which leaves the app without a signing key."
  validation {
    condition     = var.kms_key_ring == "" || can(regex("^projects/[a-z][a-z0-9-]+/locations/[a-z0-9-]+/keyRings/[A-Za-z0-9_-]+$", var.kms_key_ring))
    error_message = "kms_key_ring must be a key ring id (projects/P/locations/L/keyRings/K) or empty."
  }
}
variable "promotion_readers" {
  type        = list(string)
  default     = []
  description = "The application's production deployer (NAME@PROJECT.iam.gserviceaccount.com), which copies the images staging released into production after checking their provenance (docs/ROADMAP.md D37)."
  validation {
    condition     = alltrue([for email in var.promotion_readers : can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9][.]iam[.]gserviceaccount[.]com$", email))])
    error_message = "promotion_readers are service account emails."
  }
}
variable "slo_goal" {
  type        = number
  default     = 0
  description = "Production's objective (docs/ROADMAP.md D39): the share of requests answered without a server error over 30 days, such as 0.995; 0 sets none, as in staging."
  validation {
    condition     = var.slo_goal == 0 || (var.slo_goal >= 0.9 && var.slo_goal <= 0.999)
    error_message = "slo_goal is 0 (none) or a share from 0.9 to 0.999."
  }
}
variable "uptime_period" {
  type        = string
  default     = "300s"
  description = "How often the uptime check calls the health path: every five minutes in staging, every minute in production (D39)."
  validation {
    condition     = contains(["60s", "300s", "600s", "900s"], var.uptime_period)
    error_message = "uptime_period is 60s, 300s, 600s or 900s."
  }
}
variable "uptime_regions" {
  type        = list(string)
  default     = []
  description = "Where the uptime check calls from: every region when empty (staging); production uses three US regions (D39)."
}
variable "domain" {
  type        = string
  default     = ""
  description = "A name of the lab's own that a global load balancer with Cloud Armor answers at (domain.tf, docs/ROADMAP.md 16.6): production only, about $25 a month; empty for none."
  validation {
    condition     = var.domain == "" || (length(var.domain) <= 253 && can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$", var.domain)) && !endswith(var.domain, ".run.app"))
    error_message = "domain is empty or a lowercase host name of the lab's own, not a run.app address."
  }
}
