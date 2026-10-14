# Build provenance when GitHub cannot attest the repository: the release signs a SLSA statement for the image digest with
# this key and verifies it before deploying. Only this application's deployer may use it, and the deployer is reachable
# only from the deploy workflows on main in the staging environment (the federation condition in main.tf). Created once
# the foundation has its key ring; the pipeline never destroys it, since every stored signature needs it to verify.
resource "google_kms_crypto_key" "provenance" {
  count    = var.kms_key_ring != "" ? 1 : 0
  name     = "${var.service}-provenance"
  key_ring = var.kms_key_ring
  purpose  = "ASYMMETRIC_SIGN"
  labels   = local.labels
  version_template {
    algorithm        = "EC_SIGN_P256_SHA256"
    protection_level = "SOFTWARE"
  }
}
# Cosign reads the key's metadata (viewer) before it signs or fetches the public key (signerVerifier).
resource "google_kms_crypto_key_iam_member" "provenance" {
  for_each      = var.kms_key_ring != "" ? toset(["roles/cloudkms.signerVerifier", "roles/cloudkms.viewer"]) : toset([])
  crypto_key_id = google_kms_crypto_key.provenance[0].id
  role          = each.value
  member        = "serviceAccount:${google_service_account.deployer.email}"
}
