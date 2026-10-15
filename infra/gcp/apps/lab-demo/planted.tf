# Planted violations for the 1.9.0 live exercise (docs/VALIDATION.md): the infrastructure plan must refuse this pull
# request. It is never merged; the branch is deleted after the trial.
resource "google_storage_bucket" "planted_public" {
  name                        = "lab-deploy-staging-f29a-lab-demo-planted"
  location                    = "us-central1"
  labels                      = { lab-deploy-app = "lab-demo", lab-deploy-managed = "true" }
  uniform_bucket_level_access = true
  public_access_prevention    = "inherited"
  force_destroy               = true
}
resource "google_secret_manager_secret" "planted" {
  secret_id = "lab-demo-PLANTED"
  replication {
    auto {}
  }
}
resource "google_secret_manager_secret_version" "planted" {
  secret      = google_secret_manager_secret.planted.id
  secret_data = "placeholder-not-a-secret"
}
resource "google_sql_database_instance" "planted" {
  name                = "lab-demo-planted"
  region              = "us-central1"
  database_version    = "POSTGRES_17"
  deletion_protection = false
  settings {
    tier        = "db-f1-micro"
    user_labels = { lab-deploy-app = "lab-demo" }
    backup_configuration {
      enabled = false
    }
    ip_configuration {
      ipv4_enabled    = false
      private_network = "projects/lab-deploy-staging-f29a/global/networks/lab-deploy"
      ssl_mode        = "ALLOW_UNENCRYPTED_AND_ENCRYPTED"
    }
  }
}
