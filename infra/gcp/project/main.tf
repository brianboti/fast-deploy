provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "current" {}

locals {
  state_bucket = var.state_bucket != "" ? var.state_bucket : "${var.project_id}-tfstate"
}

module "project" {
  source                       = "../modules/project"
  project_id                   = var.project_id
  environment                  = var.environment
  viewer_members               = var.viewer_members
  project_number               = data.google_project.current.number
  region                       = var.region
  image_vulnerability_scanning = var.image_vulnerability_scanning
  billing_account              = var.billing_account
  monthly_budget_usd           = var.monthly_budget_usd
  budget_alert_email           = var.budget_alert_email
  alert_email                  = var.alert_email
  alert_webhook_url            = var.alert_webhook_url
  access_change_quiet_members  = var.access_change_quiet_members
  spending_cap                 = var.spending_cap
  billing_export_dataset       = var.billing_export_dataset
  observer_impersonators       = var.observer_impersonators
  pipeline                     = var.pipeline
  controller                   = var.controller
  state_bucket                 = local.state_bucket
}
