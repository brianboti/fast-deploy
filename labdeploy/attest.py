"""A release's Binary Authorization attestation (docs/ROADMAP.md 14.8), written through Google's REST APIs.

Cloud Run checks no Cosign signature or GitHub attestation of its own: a service that opts in runs an image only when an
attestation under the project's attestor's note says so (Binary Authorization on Cloud Run, the default rule). Once the
release has verified the image's provenance, this signs the image's digest with the project's attestor key in Cloud KMS
and stores the attestation in Artifact Analysis, the steps of Google's manual flow (`gcloud container binauthz
create-signature-payload`, `gcloud kms asymmetric-sign`, `gcloud container binauthz attestations create --validate`) in
one place; gcloud's single command for it, `sign-and-create`, exists only in its beta track. The attestation is checked
with the attestor before it is stored, so a key the attestor does not know fails here, not at the deploy.

    python -m labdeploy.attest IMAGE@sha256:DIGEST --attestor projects/P/attestors/A --key-version projects/P/locations/L/keyRings/R/cryptoKeys/K/cryptoKeyVersions/N

The access token comes from the environment (GOOGLE_ACCESS_TOKEN), never from an argument. Safe to repeat: an image the
attestor already attested is left as it is.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sys

import httpx

IMAGE = re.compile(r"^(?P<name>[a-z0-9-]+-docker\.pkg\.dev/[a-z][a-z0-9-]+/[a-z0-9._-]+/[a-z0-9._/-]+)@(?P<digest>sha256:[a-f0-9]{64})$")
ATTESTOR = re.compile(r"^projects/[a-z][a-z0-9-]+/attestors/[a-z0-9-]+$")
KEY_VERSION = re.compile(r"^projects/[a-z][a-z0-9-]+/locations/[a-z0-9-]+/keyRings/[A-Za-z0-9_-]+/cryptoKeys/[A-Za-z0-9_-]+/cryptoKeyVersions/[0-9]+$")
BINAUTHZ = "https://binaryauthorization.googleapis.com/v1"
ANALYSIS = "https://containeranalysis.googleapis.com/v1"
KMS = "https://cloudkms.googleapis.com/v1"


def payload(image):
    """The signed statement Binary Authorization reads (its "atomic container signature"): this image, by digest."""
    found = IMAGE.fullmatch(image)
    if not found:
        raise ValueError("The image is named by digest in Artifact Registry: REGION-docker.pkg.dev/PROJECT/REPOSITORY/IMAGE@sha256:HEX")
    statement = {"critical": {"identity": {"docker-reference": found["name"]}, "image": {"docker-manifest-digest": found["digest"]},
                              "type": "Google cloud binauthz container signature"}}
    return json.dumps(statement, sort_keys=True, separators=(",", ":")).encode()


def public_key_id(key_version):
    """How Terraform names a Cloud KMS key on an attestor (infra/gcp/modules/project/binauthz.tf), and so how the signature does."""
    return "//cloudkms.googleapis.com/v1/" + key_version


class Attester:
    def __init__(self, token, client=None):
        self.token = token
        self.client = client or httpx.Client(timeout=60)

    def call(self, method, url, body=None, params=None):
        response = self.client.request(method, url, json=body, params=params, headers={"Authorization": "Bearer " + self.token})
        if response.status_code >= 400:
            message = (response.json().get("error") or {}).get("message", "") if response.headers.get("content-type", "").startswith("application/json") else ""
            raise RuntimeError(f"{method} {url.split('?')[0]} answered {response.status_code}: {message[:300]}")
        return response.json()

    def note(self, attestor):
        found = self.call("GET", f"{BINAUTHZ}/{attestor}")
        note = (found.get("userOwnedGrafeasNote") or {}).get("noteReference")
        if not note:
            raise RuntimeError(f"{attestor} names no note")
        return note

    def attested(self, note, uri):
        found = self.call("GET", f"{ANALYSIS}/{note}/occurrences", params={"filter": f'resourceUrl="{uri}"', "pageSize": 1})
        return bool(found.get("occurrences"))

    def sign(self, key_version, data):
        digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
        return self.call("POST", f"{KMS}/{key_version}:asymmetricSign", {"digest": {"sha256": digest}})["signature"]

    def attest(self, image, attestor, key_version):
        """'already' or 'attested'; raises when the attestor would not accept what was signed."""
        if not ATTESTOR.fullmatch(attestor) or not KEY_VERSION.fullmatch(key_version):
            raise ValueError("--attestor is projects/P/attestors/A and --key-version a Cloud KMS key version's full name")
        data = payload(image)
        uri = "https://" + image
        note = self.note(attestor)
        if self.attested(note, uri):
            return "already"
        attestation = {"serializedPayload": base64.b64encode(data).decode(),
                       "signatures": [{"publicKeyId": public_key_id(key_version), "signature": self.sign(key_version, data)}]}
        check = self.call("POST", f"{BINAUTHZ}/{attestor}:validateAttestationOccurrence",
                          {"attestation": attestation, "occurrenceNote": note, "occurrenceResourceUri": uri})
        if check.get("result") != "VERIFIED":
            raise RuntimeError(f"the attestor would not accept this attestation: {check.get('denialReason') or check.get('result')}")
        project = attestor.split("/")[1]
        self.call("POST", f"{ANALYSIS}/projects/{project}/occurrences", {"resourceUri": uri, "noteName": note, "attestation": attestation})
        return "attested"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m labdeploy.attest", description=__doc__.split("\n\n")[0])
    parser.add_argument("image")
    parser.add_argument("--attestor", required=True)
    parser.add_argument("--key-version", required=True)
    args = parser.parse_args(argv)
    token = os.environ.get("GOOGLE_ACCESS_TOKEN", "")
    if not token:
        print("::error title=Binary Authorization::no access token")
        return 1
    try:
        outcome = Attester(token).attest(args.image, args.attestor, args.key_version)
    except (RuntimeError, ValueError, httpx.HTTPError) as exc:
        print(f"::error title=Binary Authorization::the image could not be attested: {exc}")
        return 1
    line = f"Binary Authorization: {args.image} " + ("was already attested" if outcome == "already" else "attested")
    print(line)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
