# Root module: one project module plus one service module per application. Application releases never run
# Terraform; an administrator applies this once per change (see docs/SETUP.md).
terraform {
  required_version = ">= 1.6, < 2.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "current" {}

module "project" {
  source                       = "./modules/project"
  project_id                   = var.project_id
  project_number               = data.google_project.current.number
  region                       = var.region
  services                     = sort(keys(var.services))
  image_vulnerability_scanning = var.image_vulnerability_scanning
  billing_account              = var.billing_account
  monthly_budget_usd           = var.monthly_budget_usd
  budget_alert_email           = var.budget_alert_email
  alert_email                  = var.alert_email
  alert_webhook_url            = var.alert_webhook_url
  spending_cap                 = var.spending_cap
  billing_export_dataset       = var.billing_export_dataset
  observer_impersonators       = var.observer_impersonators
}

module "service" {
  for_each                     = var.services
  source                       = "./modules/service"
  project_id                   = var.project_id
  project_number               = data.google_project.current.number
  region                       = var.region
  service                      = each.key
  github_repository            = each.value.github_repository
  github_repository_id         = each.value.github_repository_id
  github_owner_id              = each.value.github_owner_id
  bootstrap_image              = each.value.bootstrap_image
  image_versions_to_keep       = each.value.image_versions_to_keep
  image_retention_days         = each.value.image_retention_days
  registry_cleanup_dry_run     = each.value.registry_cleanup_dry_run
  image_vulnerability_scanning = var.image_vulnerability_scanning
  tag_manager_role             = module.project.tag_manager_role
  runtime_alerts               = each.value.runtime_alerts
  health_path                  = each.value.health_path
  alert_channels               = module.project.alert_channels
  error_alert_count            = each.value.error_alert_count
  latency_alert_ms             = each.value.latency_alert_ms
  instance_alert_count         = each.value.instance_alert_count
  cap_service_account          = module.project.cap_service_account
  billing_dataset              = module.project.billing_dataset
  depends_on                   = [module.project]
}
