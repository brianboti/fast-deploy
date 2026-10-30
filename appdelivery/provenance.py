"""Verifying that an image was built by Cloud Build as the build this service recorded: Cloud Build's SLSA v1 provenance,
read from Container Analysis and checked with Google's own signature, never with the occurrence's unsigned copy.

What counts: an in-toto statement of predicate type SLSA provenance v1, in a DSSE envelope signed by a key Google holds
for Cloud Build's hosted workers (pinned below); its subject the release's digest in the release's repository; its builder
Cloud Build's hosted worker; its invocation the Cloud Build build this service ordered; its source the commit that build
was asked for. Found on Google Cloud on 2026-10-30 (docs/DELIVERY-SPIKE.md section 14); the real answer is the fixture
tests/appdelivery/fixtures/cloudbuild-provenance.json.

The occurrence is read as the environment's service account, which needs roles/containeranalysis.occurrences.viewer
on the project that holds the image."""
import base64
import binascii
import json
import re

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .errors import DeliveryError, ErrorCode

CONTAINER_ANALYSIS = "https://containeranalysis.googleapis.com/v1"
PREDICATE_V1 = "https://slsa.dev/provenance/v1"
BUILDER = "https://cloudbuild.googleapis.com/GoogleHostedWorker"
PAYLOAD_TYPE = "application/vnd.in-toto+json"
# Google's key for Cloud Build's SLSA v1 provenance, read on 2026-10-30 with
#   gcloud kms keys versions get-public-key 1 --location global --keyring attestor --key google-hosted-worker --project verified-builder
# A new key version from Google needs a line here; until then its provenance does not verify, and a release in an
# environment that requires provenance is refused with the key named.
GOOGLE_KEYS = {
    "projects/verified-builder/locations/global/keyRings/attestor/cryptoKeys/google-hosted-worker/cryptoKeyVersions/1":
        "-----BEGIN PUBLIC KEY-----\n"
        "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEg9KII7kzr/30HBluf00y9WwtMFkE\n"
        "qc3oCcFVH3QJ37IBLUv/MUApbnNHFfD75ayJ/a0F45xa+MLv5zoep+GxsA==\n"
        "-----END PUBLIC KEY-----\n",
}
PROJECT = re.compile(r"^[a-z0-9-]+-docker\.pkg\.dev/(?P<project>[a-z][a-z0-9-]{4,28}[a-z0-9])/")


def _decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return base64.urlsafe_b64decode(padded)


def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE's pre-authentication encoding: what the signature is over."""
    kind = payload_type.encode()
    return b"DSSEv1 %d %s %d %s" % (len(kind), kind, len(payload), payload)


def signed_statement(envelope: dict, keys: dict) -> tuple[dict, str] | None:
    """(the statement, the key id that signed it) when a signature by one of the keys verifies; else None."""
    if envelope.get("payloadType") != PAYLOAD_TYPE or not envelope.get("payload"):
        return None
    try:
        payload = _decode(envelope["payload"])
    except (binascii.Error, ValueError):
        return None
    message = pae(envelope["payloadType"], payload)
    for signature in envelope.get("signatures", []):
        pem = keys.get(signature.get("keyid", ""))
        if pem is None:
            continue
        try:
            serialization.load_pem_public_key(pem.encode()).verify(_decode(signature.get("sig", "")), message, ec.ECDSA(hashes.SHA256()))
        except (InvalidSignature, binascii.Error, ValueError):
            continue
        try:
            return json.loads(payload), signature["keyid"]
        except ValueError:
            return None
    return None


def problems(statement: dict, image: str, build: dict) -> list[str]:
    """What in a signed statement does not match this release and the build this service recorded for it."""
    repository, digest = image.split("@sha256:")
    found = []
    if statement.get("predicateType") != PREDICATE_V1:
        found.append(f"predicate type {statement.get('predicateType')}, not {PREDICATE_V1}")
    subjects = statement.get("subject") or []
    if not any(item.get("digest", {}).get("sha256") == digest and str(item.get("name", "")).removeprefix("https://").startswith((repository + ":", repository + "@"))
               for item in subjects):
        found.append("its subject is not this image's digest in this repository")
    predicate = statement.get("predicate") or {}
    run = predicate.get("runDetails") or {}
    if (run.get("builder") or {}).get("id") != BUILDER:
        found.append(f"builder {(run.get('builder') or {}).get('id')}, not Cloud Build's hosted worker")
    invocation = (run.get("metadata") or {}).get("invocationId") or ""
    if not invocation.endswith(f"/builds/{build['cloud_build_id']}"):
        found.append(f"made by another Cloud Build build ({invocation.rsplit('/', 1)[-1] or 'none named'}), not {build['cloud_build_id']}")
    commits = [item.get("digest", {}).get("gitCommit") for item in (predicate.get("buildDefinition") or {}).get("resolvedDependencies") or []]
    if build["revision"] not in commits:
        found.append(f"its source is not commit {build['revision']}")
    return found


def occurrences(client: httpx.Client, token: str, image: str) -> list[dict]:
    """The BUILD occurrences of this image@digest, read from the project that holds it."""
    found = PROJECT.match(image)
    if found is None:
        raise DeliveryError(ErrorCode.PROVENANCE_FAILED, f"{image} is not in Artifact Registry, where Cloud Build records provenance", {"image": image})
    params = {"filter": f'kind = "BUILD" AND resourceUrl = "https://{image}"', "pageSize": 50}
    try:
        response = client.get(f"{CONTAINER_ANALYSIS}/projects/{found.group('project')}/occurrences", params=params, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise DeliveryError(ErrorCode.PROVENANCE_FAILED, f"Container Analysis could not be reached ({type(exc).__name__})", {"image": image}) from None
    if response.status_code in (401, 403):
        raise DeliveryError(ErrorCode.PROVENANCE_FAILED, f"Container Analysis refused this environment's service account ({response.status_code}): it needs "
                            "roles/containeranalysis.occurrences.viewer on the image's project", {"image": image, "status": response.status_code})
    if response.status_code != 200:
        raise DeliveryError(ErrorCode.PROVENANCE_FAILED, f"Container Analysis answered {response.status_code}", {"image": image, "said": response.text[:300]})
    return response.json().get("occurrences", [])


def verify(image: str, build: dict, found: list[dict], keys: dict | None = None) -> dict:
    """{'build', 'source', 'commit', 'key'} from the first occurrence whose signed statement matches; else PROVENANCE_FAILED
    naming why each signed one did not."""
    keys = GOOGLE_KEYS if keys is None else keys
    said = []
    for occurrence in found:
        signed = signed_statement(occurrence.get("envelope") or {}, keys)
        if signed is None:
            continue
        statement, key = signed
        wrong = problems(statement, image, build)
        if not wrong:
            sources = [item for item in (statement["predicate"].get("buildDefinition") or {}).get("resolvedDependencies") or []
                       if item.get("digest", {}).get("gitCommit") == build["revision"]]
            return {"build": build["cloud_build_id"], "source": sources[0].get("uri"), "commit": build["revision"], "key": key}
        said.append("; ".join(wrong))
    if not said:
        reason = ("it has no provenance at all: it was not pushed by Cloud Build" if not found else
                  "none of its provenance is signed by a key this service trusts (Google's for Cloud Build's hosted workers)")
        raise DeliveryError(ErrorCode.PROVENANCE_FAILED, f"{image} has no verified Cloud Build provenance: {reason}",
                            {"image": image, "occurrences": len(found), "trusted_keys": sorted(keys)})
    raise DeliveryError(ErrorCode.PROVENANCE_FAILED, f"{image}'s signed provenance does not match the build this service recorded: {said[0]}",
                        {"image": image, "build_id": build["build_id"], "said": said})
