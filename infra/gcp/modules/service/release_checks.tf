# What the release checks besides provenance (provenance.tf), with the deployer's own identity: the Binary Authorization
# attestation it records (docs/ROADMAP.md 14.8) and the data files it inspects before the image is pushed (14.10).
# Binary Authorization (docs/ROADMAP.md 14.8): the deployer records the release's attestation with the project's attestor
# key, once it has verified the image's provenance (provenance.tf). The key is the foundation's; this grant is on that
# one key only.
resource "google_kms_crypto_key_iam_member" "release_attestor" {
  count         = var.binary_authorization != null ? 1 : 0
  crypto_key_id = var.binary_authorization.key
  role          = "roles/cloudkms.signerVerifier"
  member        = "serviceAccount:${google_service_account.deployer.email}"
}
resource "google_project_iam_member" "release_attester" {
  count   = var.binary_authorization != null ? 1 : 0
  project = var.project_id
  role    = var.binary_authorization.attester_role
  member  = "serviceAccount:${google_service_account.deployer.email}"
}
# Data files the image carries are inspected for personal data before it is pushed (docs/ROADMAP.md 14.10,
# labdeploy/datafiles.py): Sensitive Data Protection's content methods only, which bill per GiB inspected after the free
# GiB a month; the release sends at most 2 MiB.
resource "google_project_iam_member" "data_inspector" {
  project = var.project_id
  role    = "roles/dlp.user"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}
