# The one private network every application shares. Cloud SQL instances get a private address through Private Service
# Access, and Cloud Run services and jobs that need them send their egress through the subnet below (Direct VPC egress,
# no connector instances). The network, the subnet and the peering cost nothing while idle.
resource "google_project_service" "network_apis" {
  for_each           = toset(["compute.googleapis.com", "servicenetworking.googleapis.com", "sqladmin.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}
resource "google_compute_network" "lab" {
  name                    = "lab-deploy"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
  depends_on              = [google_project_service.network_apis]
}
resource "google_compute_subnetwork" "run" {
  name                     = "lab-deploy-run"
  region                   = var.region
  network                  = google_compute_network.lab.id
  ip_cidr_range            = "10.8.0.0/24"
  private_ip_google_access = true
}
resource "google_compute_global_address" "private_services" {
  name          = "lab-deploy-private-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.lab.id
}
resource "google_service_networking_connection" "private_services" {
  network                 = google_compute_network.lab.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services.name]
  depends_on              = [google_project_service.network_apis]
}
