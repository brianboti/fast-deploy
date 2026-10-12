output "github_environment_variables" {
  value = { for name, app in module.service : name => app.github_environment_variables }
}
output "service_urls" { value = { for name, app in module.service : name => app.service_url } }
output "console_urls" { value = { for name, app in module.service : name => app.console_url } }
output "dashboard_urls" { value = { for name, app in module.service : name => app.dashboard_url } }
output "runtime_alerts" { value = { for name, app in module.service : name => app.runtime_alerts } }
output "observer_service_account" { value = module.project.observer_service_account }
output "cost_controls" {
  value = {
    image_vulnerability_scanning = var.image_vulnerability_scanning
    budget                       = module.project.budget
    spending_cap                 = module.project.cap_workflow
    billing_dataset              = module.project.billing_dataset
    registries = { for name, app in var.services : name => {
      cleanup_dry_run = app.registry_cleanup_dry_run
      versions_kept   = app.image_versions_to_keep
      retention_days  = app.image_retention_days
    } }
  }
}
