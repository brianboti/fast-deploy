# Promotion (docs/ROADMAP.md D37, roadmap item 16.2): the application's production deployer copies the exact image a staging
# release deployed into production's own registry, after checking that image's provenance. In staging it may read this
# application's registry and see its provenance key's public half, nothing else; once the copy is made, production
# depends on nothing here, so staging's cleanup policies never reach a production release.
resource "google_artifact_registry_repository_iam_member" "promotion_reader" {
  for_each   = toset(var.promotion_readers)
  location   = var.region
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${each.value}"
}
# Cosign reads the key's metadata before it fetches the public key it verifies with (provenance.tf).
resource "google_kms_crypto_key_iam_member" "promotion_verifier" {
  for_each      = var.kms_key_ring != "" ? { for pair in setproduct(var.promotion_readers, ["roles/cloudkms.viewer", "roles/cloudkms.publicKeyViewer"]) : "${pair[0]} ${pair[1]}" => pair } : {}
  crypto_key_id = google_kms_crypto_key.provenance[0].id
  role          = each.value[1]
  member        = "serviceAccount:${each.value[0]}"
}
