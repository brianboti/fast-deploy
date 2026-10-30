"""Cloud Build's signed provenance as the release gate reads it (appdelivery/provenance.py), against the real answer from
the run on Google Cloud on 2026-10-30 (fixtures/cloudbuild-provenance.json, docs/DELIVERY-SPIKE.md section 14): Google's
signature over the statement, and the statement naming this image, Cloud Build's hosted worker, the recorded build and
its commit. Forgeries are made here with a key of the test's own."""
import base64
import copy
import json
import logging
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fleet_fakes import SECRET_VALUE, World

from appdelivery import provenance
from appdelivery.errors import DeliveryError, ErrorCode

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "cloudbuild-provenance.json").read_text())
FOUND = FIXTURE["provenance_summary"]["provenance"]
REPOSITORY = "us-central1-docker.pkg.dev/project-3e2167e6-db58-4ab7-a22/spike-images"
IMAGE = f"{REPOSITORY}/appdelivery@sha256:647ea40708608dba7635801a7d04095e38e2f00bc4c7c3d74a73d6148b42f528"
BUILD = {"build_id": "bld-569b0ea7e103", "cloud_build_id": "afbc5e12-05db-4c1a-95ed-de8c0af87045", "revision": "aafd68bc704912b732fffc4a071c7a3db31340ca"}
GOOGLE = next(iter(provenance.GOOGLE_KEYS))
V1 = next(item for item in FOUND if "inTotoSlsaProvenanceV1" in item["build"])
STATEMENT = json.loads(base64.b64decode(V1["envelope"]["payload"]))


def refused(image=IMAGE, build=BUILD, found=FOUND, keys=None):
    with pytest.raises(DeliveryError) as caught:
        provenance.verify(image, build, found, keys)
    assert caught.value.code == ErrorCode.PROVENANCE_FAILED
    return caught.value


# ---- the real answer
def test_cloud_builds_real_provenance_verifies_with_googles_key():
    verified = provenance.verify(IMAGE, BUILD, FOUND)
    assert verified == {"build": BUILD["cloud_build_id"], "source": "git+https://github.com/brianboti/fast-deploy.git", "commit": BUILD["revision"], "key": GOOGLE}


def test_only_the_signed_payload_counts_not_the_occurrences_own_copy():
    found = copy.deepcopy(FOUND)
    for item in found:  # the unsigned copy says anything; the signed payload still names the real build
        if "inTotoSlsaProvenanceV1" in item["build"]:
            item["build"]["inTotoSlsaProvenanceV1"]["predicate"]["runDetails"]["metadata"]["invocationId"] = "forged"
    assert provenance.verify(IMAGE, BUILD, found)["build"] == BUILD["cloud_build_id"]


def test_a_changed_payload_no_longer_verifies():
    found = copy.deepcopy(FOUND)
    for item in found:
        envelope = item["envelope"]
        envelope["payload"] = base64.b64encode(base64.b64decode(envelope["payload"]).replace(BUILD["revision"].encode(), b"0" * 40)).decode()
    assert "signed by a key this service trusts" in refused(found=found).message


def test_the_v0_1_statement_alone_is_not_enough():
    v01 = [item for item in FOUND if "intotoStatement" in item["build"]]
    assert "signed by a key this service trusts" in refused(found=v01).message


def test_an_image_with_no_provenance_was_not_pushed_by_cloud_build():
    error = refused(found=[])
    assert "no provenance at all" in error.message and error.details["occurrences"] == 0


@pytest.mark.parametrize("build,expected", [({**BUILD, "cloud_build_id": "another-build"}, "made by another Cloud Build build"),
                                            ({**BUILD, "revision": "b" * 40}, "its source is not commit")])
def test_signed_provenance_of_another_build_or_commit_is_refused(build, expected):
    assert expected in refused(build=build).message


def test_signed_provenance_of_another_digest_or_repository_is_refused():
    assert "subject" in refused(image=IMAGE.replace("647ea407", "00000000")).message
    assert "subject" in refused(image=IMAGE.replace("/appdelivery@", "/other@")).message


# ---- forgeries
OWN = ec.generate_private_key(ec.SECP256R1())
OWN_PEM = OWN.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def signed(statement, keyid=GOOGLE, key=OWN):
    payload = json.dumps(statement).encode()
    signature = key.sign(provenance.pae(provenance.PAYLOAD_TYPE, payload), ec.ECDSA(hashes.SHA256()))
    return [{"kind": "BUILD", "envelope": {"payloadType": provenance.PAYLOAD_TYPE, "payload": base64.b64encode(payload).decode(),
                                           "signatures": [{"keyid": keyid, "sig": base64.b64encode(signature).decode()}]}}]


def test_a_statement_signed_by_anyone_but_google_is_not_trusted_even_under_googles_key_id():
    assert "signed by a key this service trusts" in refused(found=signed(STATEMENT)).message


@pytest.mark.parametrize("change,expected", [(lambda s: s.update(predicateType="https://slsa.dev/provenance/v0.2"), "predicate type"),
                                             (lambda s: s["predicate"]["runDetails"]["builder"].update(id="https://ci.example.com/runner"), "builder"),
                                             (lambda s: s["predicate"]["buildDefinition"].update(resolvedDependencies=[]), "commit")])
def test_each_field_of_a_trusted_statement_is_held_to_the_record(change, expected):
    statement = copy.deepcopy(STATEMENT)
    change(statement)
    assert expected in refused(found=signed(statement, "own"), keys={"own": OWN_PEM}).message
    assert provenance.verify(IMAGE, BUILD, signed(STATEMENT, "own"), keys={"own": OWN_PEM})["key"] == "own", "the same statement unchanged verifies"


# ---- reading it from Container Analysis
def analysis(status=200, body=None, seen=None):
    def handle(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body if body is not None else {"occurrences": FOUND})
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_occurrences_are_read_from_the_images_project_by_its_digest():
    seen = []
    assert provenance.occurrences(analysis(seen=seen), "env-token", IMAGE) == FOUND
    [request] = seen
    assert request.url.path == "/v1/projects/project-3e2167e6-db58-4ab7-a22/occurrences" and request.headers["Authorization"] == "Bearer env-token"
    assert request.url.params["filter"] == f'kind = "BUILD" AND resourceUrl = "https://{IMAGE}"'


def test_a_refusal_names_the_role_to_grant():
    with pytest.raises(DeliveryError) as caught:
        provenance.occurrences(analysis(403, {}), "t", IMAGE)
    assert caught.value.code == ErrorCode.PROVENANCE_FAILED and "roles/containeranalysis.occurrences.viewer" in caught.value.message


def test_an_image_outside_artifact_registry_has_no_cloud_build_provenance():
    with pytest.raises(DeliveryError) as caught:
        provenance.occurrences(analysis(), "t", "ghcr.io/acme/web@sha256:" + "a" * 64)
    assert caught.value.code == ErrorCode.PROVENANCE_FAILED


# ---- the release gate
def world_requiring_provenance(occurrences):
    world = World()
    environment = world.config.environments["dev"]
    environment.require_provenance, environment.allowed_registries = True, [*environment.allowed_registries, REPOSITORY]

    def handle(request):
        if request.url.host == "containeranalysis.googleapis.com":
            return httpx.Response(200, json={"occurrences": occurrences})
        return httpx.Response(200, json={"name": request.url.path.removeprefix("/v1/").replace("/versions/latest:access", "/versions/7"),
                                          "payload": {"data": base64.b64encode(SECRET_VALUE.encode()).decode()}})
    world.delivery.http = httpx.Client(transport=httpx.MockTransport(handle))
    return world


def test_a_release_whose_provenance_verifies_rolls_out(caplog):
    caplog.set_level(logging.INFO, logger="appdelivery")
    world = world_requiring_provenance(FOUND)
    world.built(IMAGE, cloud_build_id=BUILD["cloud_build_id"], revision=BUILD["revision"])
    assert world.delivery.plan(world.release(IMAGE), world.team).provenance_required is True
    released = world.deploy(IMAGE)
    assert released.status == "RUNNING" and "provenance_verified" in caplog.text


def test_a_recorded_build_without_matching_provenance_never_reaches_a_machine():
    world = world_requiring_provenance(FOUND)
    world.built(IMAGE, cloud_build_id="some-other-build", revision=BUILD["revision"])  # a record that does not match what Google signed
    released = world.deploy(IMAGE)
    assert released.status == "FAILED" and released.error["code"] == "PROVENANCE_FAILED" and world.fleet.calls == []
    assert {target.status for target in released.targets} == {"SKIPPED"}


def test_an_environment_without_the_requirement_does_not_ask():
    world = world_requiring_provenance(FOUND)
    world.config.environments["dev"].require_provenance = False
    asked = []
    secrets = world.delivery.http

    def handle(request):
        if request.url.host == "containeranalysis.googleapis.com":
            asked.append(request)
        return secrets.send(request)
    world.delivery.http = httpx.Client(transport=httpx.MockTransport(handle))
    assert world.deploy().status == "RUNNING" and asked == []
