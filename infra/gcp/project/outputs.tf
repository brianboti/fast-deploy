# Application roots read these through terraform_remote_state; keep the names stable.
output "project_number" { value = data.google_project.current.number }
output "region" { value = var.region }
output "state_bucket" { value = local.state_bucket }
output "alert_channels" { value = module.project.alert_channels }
output "tag_manager_role" { value = module.project.tag_manager_role }
output "cap_service_account" { value = module.project.cap_service_account }
output "billing_dataset" { value = module.project.billing_dataset }
output "image_vulnerability_scanning" { value = var.image_vulnerability_scanning }
output "observer_service_account" { value = module.project.observer_service_account }
output "pipeline" { value = module.project.pipeline }
output "network" { value = module.project.network }
output "subnet" { value = module.project.subnet }
output "cost_controls" {
  value = {
    image_vulnerability_scanning = var.image_vulnerability_scanning
    budget                       = module.project.budget
    spending_cap                 = module.project.cap_workflow
    billing_dataset              = module.project.billing_dataset
  }
}
