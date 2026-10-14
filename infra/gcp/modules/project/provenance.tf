# Build provenance for repositories GitHub cannot attest (private ones on a personal account or below Enterprise Cloud):
# each application's release signs a SLSA statement with its own key in this ring, which the service module creates and
# lets only that application's deployer use. The ring is free and permanent (Cloud KMS never deletes a key ring); each key
# costs about USD 0.06 a month.
resource "google_project_service" "kms" {
  service            = "cloudkms.googleapis.com"
  disable_on_destroy = false
}
resource "google_kms_key_ring" "provenance" {
  name       = "lab-deploy"
  location   = var.region
  depends_on = [google_project_service.kms]
}
# The pipeline creates and grants the per-application keys in this ring and nowhere else; neither identity can sign.
resource "google_kms_key_ring_iam_member" "infra_apply" {
  count       = local.pipeline_enabled ? 1 : 0
  key_ring_id = google_kms_key_ring.provenance.id
  role        = "roles/cloudkms.admin"
  member      = "serviceAccount:${google_service_account.infra_apply[0].email}"
}
resource "google_kms_key_ring_iam_member" "infra_plan" {
  count       = local.pipeline_enabled ? 1 : 0
  key_ring_id = google_kms_key_ring.provenance.id
  role        = "roles/cloudkms.viewer"
  member      = "serviceAccount:${google_service_account.infra_plan[0].email}"
}
