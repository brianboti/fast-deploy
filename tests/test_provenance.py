"""Build provenance for private repositories: Cosign with the application's Cloud KMS key, and the operator's exception."""

import base64
import json
import re
from pathlib import Path

import httpx
import pytest
import yaml
from conftest import TARGET
from test_phase2_release import make, release
from test_release import IMAGE, PLAN_ID

from labdeploy import provenance
from labdeploy.config import Config
from labdeploy.github import GitHub
from labdeploy.infra import ALLOWED_ROLES, app_root, check_plan

COMMIT = "c" * 40
REPOSITORY = "brianboti/sophia"
IMAGE_NAME = "us-central1-docker.pkg.dev/lab-deploy-staging-f29a/sophia/sophia"
DIGEST = "sha256:" + "d" * 64
RUN = {"repository": REPOSITORY, "commit": COMMIT, "plan_id": PLAN_ID, "spec_hash": "e" * 64, "run_id": "123", "run_attempt": "1",
       "repository_id": "1418277247", "owner_id": "207480197", "workflow_ref": f"{REPOSITORY}/.github/workflows/deploy-gcp.yml@refs/heads/main"}


def envelope(statement):
    """One line of `cosign verify-attestation` output: the DSSE envelope its signature check passed."""
    payload = base64.b64encode(json.dumps(statement).encode()).decode()
    return json.dumps({"payloadType": "application/vnd.in-toto+json", "payload": payload, "signatures": [{"sig": "MEUC"}]})


def statement(name=IMAGE_NAME, digest=DIGEST, **run):
    return {"_type": "https://in-toto.io/Statement/v0.1", "predicateType": "https://slsa.dev/provenance/v1",
            "subject": [{"name": name, "digest": {"sha256": digest.removeprefix("sha256:")}}], "predicate": provenance.predicate(**{**RUN, **run})}


def check(output, **expected):
    return provenance.verify(output, **{"image_name": IMAGE_NAME, "digest": DIGEST, "repository": REPOSITORY, "commit": COMMIT, **expected})


def test_the_signing_key_is_the_application_s_own_in_the_foundation_ring():
    uri = provenance.key_uri("lab-deploy-staging-f29a", "us-central1", "sophia")
    assert uri == "gcpkms://projects/lab-deploy-staging-f29a/locations/us-central1/keyRings/lab-deploy/cryptoKeys/sophia-provenance/cryptoKeyVersions/1"
    with pytest.raises(provenance.ProvenanceError):
        provenance.key_uri("lab-deploy-staging-f29a", "us-central1", "sophia/../other")
    # The names the template signs with are the ones Terraform creates.
    ring = Path("infra/gcp/modules/project/provenance.tf").read_text()
    key = Path("infra/gcp/modules/service/provenance.tf").read_text()
    assert f'name       = "{provenance.KEY_RING}"' in ring and 'name     = "${var.service}-provenance"' in key
    assert 'purpose  = "ASYMMETRIC_SIGN"' in key and 'algorithm        = "EC_SIGN_P256_SHA256"' in key and 'protection_level = "SOFTWARE"' in key
    assert 'member        = "serviceAccount:${google_service_account.deployer.email}"' in key and key.count("member ") == 1


def test_the_statement_records_repository_commit_workflow_and_run():
    body = provenance.predicate(**RUN)
    assert body["buildDefinition"]["resolvedDependencies"] == [{"uri": f"git+https://github.com/{REPOSITORY}@refs/heads/main", "digest": {"gitCommit": COMMIT}}]
    assert body["runDetails"]["builder"]["id"] == f"https://github.com/{REPOSITORY}/.github/workflows/deploy-gcp.yml@refs/heads/main"
    assert body["runDetails"]["metadata"]["invocationId"] == f"https://github.com/{REPOSITORY}/actions/runs/123/attempts/1"
    assert body["buildDefinition"]["internalParameters"]["lab_deploy"] == {"plan_id": PLAN_ID, "spec_hash": "e" * 64}
    # It never claims a builder that is not the one running.
    with pytest.raises(provenance.ProvenanceError, match="only by .github/workflows/deploy-gcp.yml on main"):
        provenance.predicate(**{**RUN, "workflow_ref": f"{REPOSITORY}/.github/workflows/other.yml@refs/heads/main"})
    with pytest.raises(provenance.ProvenanceError, match="only by"):
        provenance.predicate(**{**RUN, "workflow_ref": f"{REPOSITORY}/.github/workflows/deploy-gcp.yml@refs/heads/feature"})
    with pytest.raises(provenance.ProvenanceError, match="Invalid commit"):
        provenance.predicate(**{**RUN, "commit": "abc"})


def test_only_a_statement_about_this_image_commit_and_workflow_passes():
    assert check(envelope(statement()))["predicate"]["runDetails"]["metadata"]["invocationId"].endswith("/runs/123/attempts/1")
    refused = {
        "built from a different commit": envelope(statement(commit="f" * 40)),
        "names a different image": envelope(statement(digest="sha256:" + "0" * 64)),
        "built by a different repository, workflow or branch": envelope(statement(repository="someone/else",
                                                                                  workflow_ref="someone/else/.github/workflows/deploy-gcp.yml@refs/heads/main")),
        "not SLSA v1 provenance": envelope({**statement(), "predicateType": "https://slsa.dev/provenance/v0.2"}),
        "not an in-toto statement": envelope({**statement(), "_type": "something"}),
        "unreadable": '{"payloadType": "application/vnd.in-toto+json", "payload": "%%%"}',
        "Cosign returned no verified attestation": "\n",
    }
    for reason, output in refused.items():
        with pytest.raises(provenance.ProvenanceError, match=re.escape(reason)):
            check(output)
    other_name = statement(name="us-central1-docker.pkg.dev/lab-deploy-staging-f29a/other/other")
    with pytest.raises(provenance.ProvenanceError, match="names a different image"):
        check(envelope(other_name))
    builder = statement()
    builder["predicate"]["runDetails"]["builder"]["id"] = "https://example.com/builder"
    with pytest.raises(provenance.ProvenanceError, match="a different builder"):
        check(envelope(builder))
    # A reused image can carry several statements, all signed by the same key; one about this commit is enough.
    assert check("\n".join([envelope(statement(commit="f" * 40)), envelope(statement())]))


def test_the_verify_command_reads_the_run_s_environment(tmp_path, monkeypatch, capsys):
    output = tmp_path / "verified.jsonl"
    output.write_text(envelope(statement()) + "\n")
    for name, value in {"IMAGE_NAME": IMAGE_NAME, "IMAGE_SHA256": DIGEST, "GITHUB_REPOSITORY": REPOSITORY, "COMMIT_SHA": COMMIT}.items():
        monkeypatch.setenv(name, value)
    assert provenance.main(["verify", str(output)]) == 0
    assert "Provenance verified" in capsys.readouterr().out
    monkeypatch.setenv("COMMIT_SHA", "f" * 40)
    assert provenance.main(["verify", str(output)]) == 1
    assert "::error title=Provenance::" in capsys.readouterr().err
    assert provenance.main(["sign"]) == 2


def test_the_deploy_job_picks_github_cosign_or_the_operator_exception():
    workflow = yaml.safe_load(Path("templates/deploy-gcp.yml").read_text())
    dispatch = workflow[True]["workflow_dispatch"]["inputs"]["provenance"]
    assert dispatch["type"] == "choice" and dispatch["options"] == ["required", "unavailable"] and dispatch["default"] == "required"
    steps = workflow["jobs"]["deploy"]["steps"]
    names = [step.get("name") for step in steps]
    order = ["Choose the provenance path", "Push image and resolve immutable digest", "Attest build provenance", "Verify build provenance",
             "Install Cosign", "Sign build provenance with Cloud KMS", "Verify build provenance with Cloud KMS", "Deploy, probe, promote; restore traffic on failure"]
    assert [names.index(name) for name in order] == sorted(names.index(name) for name in order)
    step = {name: steps[names.index(name)] for name in order}
    choose = step["Choose the provenance path"]["run"]
    assert 'gh api "repos/$GITHUB_REPOSITORY" --jq .visibility)" = "public" ]' in choose and "MODE=cosign-kms" in choose
    # The exception is refused with a reused image: only provenance shows the pipeline built it.
    exception = choose[choose.index('if [ "$PROVENANCE_INPUT" = "unavailable" ]'):choose.index("MODE=unavailable")]
    assert 'if [ -n "$IMAGE_DIGEST_INPUT" ]' in exception and "exit 1" in exception
    assert step["Attest build provenance"]["if"] == "inputs.image_digest == '' && steps.provenance.outputs.mode == 'github'"
    assert step["Verify build provenance"]["if"] == "steps.provenance.outputs.mode == 'github'"
    install = step["Install Cosign"]["run"]
    assert "releases/download/v3.1.3/cosign-linux-amd64" in install and "| sha256sum --check --strict" in install
    assert "4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71" in install
    sign = step["Sign build provenance with Cloud KMS"]
    assert sign["if"] == "inputs.image_digest == '' && steps.provenance.outputs.mode == 'cosign-kms'"
    # Stored as the tag the registry keeps; never uploaded to a public transparency log.
    for flag in ("--new-bundle-format=false", "--use-signing-config=false", "--tlog-upload=false", "--type slsaprovenance1", '--key "$PROVENANCE_KEY"'):
        assert flag in sign["run"], flag
    verify = step["Verify build provenance with Cloud KMS"]
    assert verify["if"] == "steps.provenance.outputs.mode == 'cosign-kms'"  # also on reuse
    assert "verify-attestation" in verify["run"] and "--insecure-ignore-tlog=true" in verify["run"]
    assert "python -m labdeploy.provenance verify" in verify["run"]


def test_the_release_refuses_to_run_without_a_provenance_decision(monkeypatch, tmp_path):
    from labdeploy import release

    monkeypatch.setenv("TARGET_JSON", json.dumps(TARGET, sort_keys=True))
    for name, value in {"GCP_PROJECT_ID": TARGET["project_id"], "GCP_REGION": TARGET["region"], "SERVICE": TARGET["service"],
                        "REGISTRY_NAME": TARGET["registry"], "WIF_PROVIDER": TARGET["wif_provider"], "DEPLOY_SERVICE_ACCOUNT": TARGET["deploy_service_account"],
                        "PROBE_SERVICE_ACCOUNT": TARGET["probe_service_account"], "RECOVERY_BUCKET": TARGET["recovery_bucket"], "IMAGE_DIGEST": IMAGE}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PROVENANCE_MODE", raising=False)
    monkeypatch.delenv("RECOVERY_ONLY", raising=False)
    with pytest.raises(ValueError, match="No provenance decision"):
        release.main()
    monkeypatch.setenv("PROVENANCE_MODE", "skipped")
    with pytest.raises(ValueError, match="No provenance decision"):
        release.main()


def test_the_receipt_records_which_provenance_the_image_carries(tmp_path, monkeypatch):
    from labdeploy.models import AppSpec
    from labdeploy.release import rollout

    monkeypatch.chdir(tmp_path)

    class Cloud:
        service = "demo"

        def describe(self):
            return {"status": {"url": "https://demo.run.app", "traffic": [{"revisionName": "demo-rold", "percent": 100}]}}

        def traffic(self, allocations):
            pass

        def require_secrets(self, spec):
            raise ValueError("stop before deploying")

        def run(self, args):
            return ""

    with pytest.raises(RuntimeError):
        rollout(Cloud(), IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, "receipt.json", provenance="cosign-kms")
    assert json.loads(Path("receipt.json").read_text())["provenance"] == "cosign-kms"


def test_the_exception_is_the_operator_s_builds_fresh_and_is_sent_only_when_set(tmp_path, monkeypatch):
    c, p, gh = make(tmp_path)
    first = release(c, p, gh, "1" * 40, "demo-r1", image=IMAGE)
    assert first["provenance"] == "required" and c.plan(p, "demo")["reuse_image"] == IMAGE
    c.config.applications["demo"].provenance = "unavailable"
    plan = c.plan(p, "demo")
    assert plan["provenance"] == "unavailable" and "reuse_image" not in plan  # a reused image needs its provenance
    assert any("provenance: unavailable" in warning for warning in plan["warnings"])
    c.config.applications["demo"].provenance = "required"
    with pytest.raises(ValueError, match="provenance setting changed"):
        c.deploy(p, plan["id"])
    # Developers cannot set it: platform.yaml has no such field, and an onboarded app takes it from application_settings.
    from labdeploy.models import AppSpec
    with pytest.raises(ValueError):
        AppSpec.model_validate({"name": "demo", "reviewed": True, "provenance": "unavailable"})
    raw = {"applications": {}, "principals": [], "application_settings": {"newapp": {"provenance": "unavailable"}}}
    assert Config.model_validate(raw).application_settings["newapp"].provenance == "unavailable"
    with pytest.raises(ValueError):
        Config.model_validate({**raw, "application_settings": {"newapp": {"provenance": "off"}}})

    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
    app = Config.model_validate({"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}}, "principals": []}).applications["demo"]
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"workflow_run_id": 5})
    github = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    base = {"sha": "a" * 40, "spec_hash": "b" * 64, "id": PLAN_ID, "target": TARGET}
    github.dispatch(app, {**base, "provenance": "required"})
    github.dispatch(app, {**base, "provenance": "unavailable"})
    assert "provenance" not in bodies[0]["inputs"] and bodies[1]["inputs"]["provenance"] == "unavailable"


def resource(kind, after, actions=("create",), address=None):
    return {"address": address or f"module.service.{kind}.this", "type": kind, "change": {"actions": list(actions), "after": after, "after_unknown": {}}}


def test_the_policy_admits_the_signing_key_and_keeps_it():
    ring = "projects/lab-deploy-staging-f29a/locations/us-central1/keyRings/lab-deploy"
    key = {"name": "sophia-provenance", "key_ring": ring, "purpose": "ASYMMETRIC_SIGN", "labels": {"lab-deploy-app": "sophia", "lab-deploy-managed": "true"}}
    grants = [resource("google_kms_crypto_key_iam_member", {"crypto_key_id": f"{ring}/cryptoKeys/sophia-provenance", "role": role,
                                                            "member": "serviceAccount:sophia-deploy@lab-deploy-staging-f29a.iam.gserviceaccount.com"},
                       address=f"module.service.google_kms_crypto_key_iam_member.provenance[{role!r}]")
              for role in ("roles/cloudkms.signerVerifier", "roles/cloudkms.viewer")]
    assert check_plan({"resource_changes": [resource("google_kms_crypto_key", key), *grants]}) == []
    rules = lambda *changes: sorted(v["rule"] for v in check_plan({"resource_changes": list(changes)}))  # noqa: E731
    assert rules(resource("google_kms_crypto_key", key, ("delete",))) == ["no_data_loss"]
    assert rules(resource("google_kms_crypto_key", key, ("delete", "create"))) == ["no_data_loss"]
    assert rules(resource("google_kms_crypto_key", {**key, "key_ring": ring.replace("us-central1", "europe-west1")})) == ["region_policy"]
    assert rules(resource("google_kms_crypto_key", {**key, "labels": {}})) == ["labels_present"]
    admin = resource("google_kms_crypto_key_iam_member", {"crypto_key_id": "k", "role": "roles/cloudkms.admin", "member": "user:someone@example.com"})
    assert rules(admin) == ["role_not_allowed"]
    assert {"roles/cloudkms.signerVerifier", "roles/cloudkms.viewer"} <= ALLOWED_ROLES and "roles/cloudkms.admin" not in ALLOWED_ROLES


def test_every_root_reads_the_key_ring_and_plans_without_it():
    files = app_root("sophia", project_id="p-12345", region="us-central1", state_bucket="p-12345-tfstate", github_repository=REPOSITORY,
                     github_repository_id="1", github_owner_id="2", bootstrap_image="us-docker.pkg.dev/cloudrun/container/hello")
    line = re.search(r'\n(  kms_key_ring\s+= try\(data\.terraform_remote_state\.project\.outputs\.kms_key_ring, ""\))\n', files["main.tf"])
    assert line
    for root in Path("infra/gcp/apps").iterdir():
        assert line.group(1) in (root / "main.tf").read_text(), root.name
    service = Path("infra/gcp/modules/service/provenance.tf").read_text()
    assert service.count('var.kms_key_ring != ""') == 2  # no key ring yet: no key, and nothing fails
    registry = Path("infra/gcp/modules/service/main.tf").read_text()
    keep = registry[registry.index('id     = "keep-provenance"'):]
    assert keep.index('action = "KEEP"') < keep.index("}") and 'tag_prefixes = ["sha256-"]' in keep[:keep.index("}\n  }")]
    foundation = Path("infra/gcp/modules/project/provenance.tf").read_text()
    assert 'service            = "cloudkms.googleapis.com"' in foundation
    assert 'role        = "roles/cloudkms.admin"' in foundation and 'role        = "roles/cloudkms.viewer"' in foundation
    assert "useToSign" not in foundation and "signerVerifier" not in foundation  # the pipeline identities can never sign
    assert 'output "kms_key_ring" { value = module.project.kms_key_ring }' in Path("infra/gcp/project/outputs.tf").read_text()


def test_a_failed_provenance_step_is_explained_by_its_own_advice():
    from labdeploy.status import STEP_ADVICE

    def advice(step):
        return next(text for needle, text in STEP_ADVICE if needle in step)
    assert "Cloud KMS key failed" in advice("Sign build provenance with Cloud KMS")
    assert "signed with the application's key" in advice("Verify build provenance with Cloud KMS")
    assert "docker-credential-gcloud" in advice("Verify build provenance")  # GitHub's path keeps its own
    assert "checksum" in advice("Install Cosign") and "reused image" in advice("Choose the provenance path")
