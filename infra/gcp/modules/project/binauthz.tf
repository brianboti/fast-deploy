# Binary Authorization on Cloud Run (docs/ROADMAP.md 14.8): a service that opts in (every application's, from the root
# that follows this foundation) runs only an image this project's release attestor has signed. Cloud Run reads no Cosign
# signature or GitHub attestation of its own, only attestations stored in Artifact Analysis under an attestor's note, and
# a project has one policy, so there is one attestor per project: a release attests its image with this key once its
# provenance (GitHub's, or the application's own Cosign key) has been verified, and a promotion does the same in
# production after it has checked staging's.
#
# `binary_authorization` is dryrun until the operator has watched a few releases: Cloud Run then deploys an image without
# an attestation and writes the would-be refusal to the audit log; enforced refuses it; off allows every image and takes
# the services out at their roots' next apply. The key, the note and the attestor stay whatever the mode, and releases
# keep attesting, so switching back needs nothing but the apply. Cloud Run checks at each new revision, never a revision already serving, so a rollback is
# never refused. Binary Authorization costs nothing on Cloud Run; the key costs about $0.06 a month.
resource "google_project_service" "binauthz" {
  for_each           = toset(["binaryauthorization.googleapis.com", "containeranalysis.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}
resource "google_kms_crypto_key" "release_attestor" {
  name     = "release-attestor"
  key_ring = google_kms_key_ring.provenance.id
  purpose  = "ASYMMETRIC_SIGN"
  labels   = { lab-deploy-managed = "true" }
  version_template {
    algorithm        = "EC_SIGN_P256_SHA256"
    protection_level = "SOFTWARE"
  }
  lifecycle {
    prevent_destroy = true # every attestation it signed needs it to verify, and Cloud KMS never frees the name
  }
}
data "google_kms_crypto_key_version" "release_attestor" {
  crypto_key = google_kms_crypto_key.release_attestor.id
}
resource "google_container_analysis_note" "release_attestor" {
  name = "lab-deploy-releases"
  attestation_authority {
    hint {
      human_readable_name = "Fast Deploy: images whose provenance a release verified"
    }
  }
  depends_on = [google_project_service.binauthz]
}
# Binary Authorization's own service agent reads the attestations under the note when Cloud Run asks it about a deploy.
# scripts/apply-foundation.sh makes sure the agent exists before this grant.
resource "google_container_analysis_note_iam_member" "binauthz_agent" {
  note   = google_container_analysis_note.release_attestor.name
  role   = "roles/containeranalysis.notes.occurrences.viewer"
  member = "serviceAccount:service-${var.project_number}@gcp-sa-binaryauthorization.iam.gserviceaccount.com"
}
resource "google_binary_authorization_attestor" "release" {
  name        = "lab-deploy-releases"
  description = "Fast Deploy: a release verified this image's provenance"
  attestation_authority_note {
    note_reference = google_container_analysis_note.release_attestor.name
    public_keys {
      id = data.google_kms_crypto_key_version.release_attestor.id
      pkix_public_key {
        public_key_pem      = data.google_kms_crypto_key_version.release_attestor.public_key[0].pem
        signature_algorithm = data.google_kms_crypto_key_version.release_attestor.public_key[0].algorithm
      }
    }
  }
}
# The project's one policy. dryrun and enforced require the attestor; off allows every image. The reference image
# Terraform gives a new service before its first release (infra.py BOOTSTRAP_IMAGE) is always allowed.
resource "google_binary_authorization_policy" "policy" {
  global_policy_evaluation_mode = "ENABLE"
  admission_whitelist_patterns {
    name_pattern = "us-docker.pkg.dev/cloudrun/container/hello*"
  }
  default_admission_rule {
    evaluation_mode         = var.binary_authorization == "off" ? "ALWAYS_ALLOW" : "REQUIRE_ATTESTATION"
    enforcement_mode        = var.binary_authorization == "dryrun" ? "DRYRUN_AUDIT_LOG_ONLY" : "ENFORCED_BLOCK_AND_AUDIT_LOG"
    require_attestations_by = var.binary_authorization == "off" ? [] : [google_binary_authorization_attestor.release.name]
  }
  depends_on = [google_project_service.binauthz]
}
# What a deployer needs to attest its own release (labdeploy/attest.py), granted per application by its root with
# signerVerifier on the key: read the attestor and its note's occurrences, check an attestation with the attestor, store
# it under the note. Nothing here can change the policy, the attestor or the key.
resource "google_project_iam_custom_role" "release_attester" {
  role_id     = "labDeployReleaseAttester"
  title       = "Lab Deploy release attester"
  description = "Lets a deployer record a Binary Authorization attestation for its release"
  permissions = ["binaryauthorization.attestors.get", "binaryauthorization.attestors.verifyImageAttested", "containeranalysis.notes.attachOccurrence",
  "containeranalysis.notes.listOccurrences", "containeranalysis.occurrences.create"]
}
