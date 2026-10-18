"""Phase 16, part two: promotion by digest (docs/ROADMAP.md D37, roadmap item 16.2)."""
import base64
import json
import time
from pathlib import Path

import httpx
import pytest
import yaml
from conftest import TARGET
from test_phase16 import PRODUCTION, ProductionGitHub, apply_run, settings

from labdeploy import chat, promotion, provenance
from labdeploy.config import Config, LiveConfig
from labdeploy.controller import Controller, Store
from labdeploy.github import GitHub
from labdeploy.infra import ALLOWED_ROLES, app_root, check_plan
from labdeploy.models import AppSpec, enforce, spec_hash
from labdeploy.release import origin_from, promotion_from
from labdeploy.status import advice, summarize

DIGEST = "sha256:" + "d" * 64
STAGING_IMAGE = f"us-central1-docker.pkg.dev/test-project/demo/demo@{DIGEST}"
COMMIT, CONTRACT, STAGING_PLAN = "a" * 40, "b" * 40, "e" * 32
SPEC = AppSpec.model_validate({"name": "demo", "reviewed": True})
RUN = "https://github.com/lab/platform/actions/runs/77"


class PromotionGitHub(ProductionGitHub):
    def __init__(self):
        super().__init__()
        self.promotions, self.cancelled, self.run, self.receipt, self.receipt_from = [], [], None, None, None

    def dispatch_promotion(self, repository, plan):
        self.promotions.append((repository, plan))
        return {"workflow_run_id": 77, "html_url": RUN}

    def status(self, app, plan):
        return self.run if plan.get("kind") == "promotion" else super().status(app, plan)

    def cancel_run(self, app, run_id):
        self.cancelled.append((app.pipeline_repo, run_id))
        return {"cancel_requested": True, "workflow_run_id": run_id}

    def release_receipt(self, app, run_id):
        self.receipt_from = app.pipeline_repo
        return self.receipt


def ready_controller(tmp_path, **extra):
    """A controller whose demo application has a ready production: both pull requests merged, the apply succeeded."""
    config = LiveConfig(Config.model_validate(settings(tmp_path, production=PRODUCTION, **extra)))
    gh = PromotionGitHub()
    c = Controller(config, gh, Store(config.current.database))
    brian = config.current.principals[0]
    c.add_production(brian, "demo")
    gh.on_main["infra/gcp/apps-production/demo/main.tf"] = gh.platform_prs[-1]["files"]["infra/gcp/apps-production/demo/main.tf"]
    gh.apply_runs[("infra-apply.yml", gh.LAST)] = apply_run("completed", "success")
    c.add_production(brian, "demo")
    gh.on_main["infra/gcp/apps/demo/main.tf"] = gh.platform_prs[-1]["files"]["infra/gcp/apps/demo/main.tf"]
    assert c.add_production(brian, "demo")["state"] == "ready"
    return c, brian, gh


def staging_release(c, plan_id=STAGING_PLAN, **extra):
    plan = {"id": plan_id, "application": "demo", "repository": "lab/demo", "branch": "main", "sha": COMMIT,
            "commit": {"sha": COMMIT, "message": "Fix the login bug"}, "spec_hash": spec_hash(SPEC), "spec": SPEC.model_dump(), "cloud": "gcp",
            "environment": "staging", "target": TARGET, "expires_at": time.time(), "checks": {},
            "cost": {"idle_floor_usd_month": 0.0, "compute_ceiling_usd_month": 12.5, "needs_floor_usd_month": 0.0},
            "layout": {"path": "", "workflow": "deploy-gcp.yml", "github_environment": "staging"},
            "receipt": {"state": "succeeded", "image": STAGING_IMAGE, "provenance": "github", "revision": "demo-reeeeeeeeeeeeeeee"},
            "outcome": {"state": "succeeded", "recorded_at": time.time(), "completed_at": "2026-10-17T10:00:00Z"}}
    plan.update(extra)
    c.store.add("brian", plan, "succeeded")
    return plan


# ---- 16.2 the controller's promotion
def test_promote_refuses_without_production_ready_or_a_signed_recent_staging_release(tmp_path):
    config = LiveConfig(Config.model_validate(settings(tmp_path)))
    bare = Controller(config, PromotionGitHub(), Store(config.current.database))
    with pytest.raises(ValueError, match="add_production first"):
        bare.promote(config.current.principals[0], "demo")
    (tmp_path / "ready").mkdir()
    c, brian, gh = ready_controller(tmp_path / "ready")
    with pytest.raises(ValueError, match="deploy it to staging first"):
        c.promote(brian, "demo")
    staging_release(c, receipt={"state": "succeeded", "image": STAGING_IMAGE, "provenance": "unavailable"})
    with pytest.raises(ValueError, match="deploy it to staging first"):  # only signed releases count
        c.promote(brian, "demo")
    with pytest.raises(ValueError, match="no signed provenance"):
        c.promote(brian, "demo", STAGING_PLAN)
    staging_release(c, "f" * 32, outcome={"state": "succeeded", "recorded_at": time.time() - 31 * 86_400})
    with pytest.raises(ValueError, match="over 30 days old"):
        c.promote(brian, "demo")
    staging_release(c, "1" * 32, rolled_back=True)
    with pytest.raises(ValueError, match="rolled back"):
        c.promote(brian, "demo", "1" * 32)
    staging_release(c, "2" * 32)
    bypassable = gh.deployment_environment
    gh.deployment_environment = lambda repository, name: {**bypassable(repository, name), "can_admins_bypass": True}
    with pytest.raises(ValueError, match="administrators can skip the approval in production-demo"):  # GitHub's default (found live)
        c.promote(brian, "demo")
    del gh.deployment_environment
    del gh.environments[("lab/platform", "production-demo")]
    with pytest.raises(ValueError, match="production-demo does not exist.*Ask for add_production"):
        c.promote(brian, "demo")
    assert gh.promotions == []


def test_promote_dispatches_the_exact_staging_image_to_the_platform_pipeline(tmp_path):
    c, brian, gh = ready_controller(tmp_path)
    staging_release(c)
    result = c.promote(brian, "demo")
    repository, plan = gh.promotions[-1]
    assert repository == "lab/platform" and result["state"] == "dispatched" and result["kind"] == "promotion"
    assert plan["source"] == {"plan": STAGING_PLAN, "image": STAGING_IMAGE, "digest": DIGEST, "provenance": "github", "workflow": "deploy-gcp.yml",
                              "attested_commit": COMMIT, "revision": "demo-reeeeeeeeeeeeeeee", "released_at": "2026-10-17T10:00:00Z",
                              "cost": {"idle_floor_usd_month": 0.0, "compute_ceiling_usd_month": 12.5, "needs_floor_usd_month": 0.0}}
    assert plan["target"]["project_id"] == "lab-deploy-prod-abcd" and plan["layout"]["github_environment"] == "production-demo"
    approval = result["approval"]
    assert approval["image"]["digest"] == DIGEST and approval["image"]["staging_release"] == STAGING_PLAN and approval["approvers"] == ["brianboti"]
    assert approval["cost"]["staging"]["compute_ceiling_usd_month"] == 12.5 and approval["cost"]["production"]["compute_ceiling_usd_month"] is not None
    assert approval["identifiers"]["run"] == 77 and approval["target"]["environment"] == "production-demo"
    # a promotion is not a staging release: staging's live release, its rollback and its next reuse stay what they were
    assert c.store.latest_success("demo")["id"] == STAGING_PLAN and c.store.latest_success("demo", kind="promotion") is None
    assert {e["outcome"] for e in c.store.recent_events(20) if e["kind"] == "promotion"} == {"dispatched"}
    assert "Waiting for approval: promote `demo` to production" in chat.release(result) and "`brianboti`" in chat.release(result)


def test_github_attests_the_commit_its_build_run_started_on_so_that_is_what_a_promotion_checks(tmp_path):
    """Found reading the template on 2026-10-18: a release of an older commit of main is built by a run that started on
    main's tip, which GitHub's attestation names; a reused image was attested by the run that built it."""
    c, brian, gh = ready_controller(tmp_path)
    tip = "9" * 40
    staging_release(c, "3" * 32, workflow={"head_sha": tip})  # built an older commit on a run that started at the tip
    staging_release(c, "4" * 32, reused_from_plan="3" * 32, workflow={"head_sha": "8" * 40})  # reused that image later
    c.promote(brian, "demo", "4" * 32)
    assert gh.promotions[-1][1]["source"]["attested_commit"] == tip
    staging_release(c, "5" * 32, workflow={"head_sha": COMMIT}, receipt={"state": "succeeded", "image": STAGING_IMAGE, "provenance": "cosign-kms"})
    c.promote(brian, "demo", "5" * 32)
    assert gh.promotions[-1][1]["source"]["attested_commit"] == COMMIT  # a statement of the platform's own names the built commit
    assert "${ATTESTED_COMMIT:-$COMMIT_SHA}" in Path(".github/workflows/promote.yml").read_text()


def test_a_promotion_waits_for_a_reviewer_then_production_has_its_own_live_release(tmp_path):
    c, brian, gh = ready_controller(tmp_path)
    staging_release(c)
    plan_id = c.promote(brian, "demo")["id"]
    gh.run = {"id": 77, "status": "waiting", "html_url": RUN, "jobs": [{"name": "describe", "steps": []}, {"name": "promote", "steps": []}],
              "pending": [{"environment": "production-demo", "reviewers": ["brianboti"]}]}
    waiting = c.status(brian, plan_id)
    assert waiting["state"] == "awaiting_approval" and "Waiting for brianboti to approve" in waiting["next_action"] and RUN in waiting["next_action"]
    steps = [{"name": name, "status": "completed", "conclusion": "success"} for name in (
        "Refuse to run without a reviewer's approval", "Verify the staging image's provenance", "Copy the image into production by digest",
        "Deploy, probe, promote; restore traffic on failure")]
    gh.run = {"id": 77, "status": "completed", "conclusion": "success", "html_url": RUN, "created_at": "2026-10-17T11:00:00Z",
              "updated_at": "2026-10-17T11:30:00Z", "jobs": [{"name": "describe", "steps": []}, {"name": "promote", "steps": steps}]}
    gh.receipt = {"release": {"state": "succeeded", "revision": "demo-r1234", "url": "https://demo-prod.a.run.app",
                              "promotion": {"source_plan": STAGING_PLAN, "source_image": STAGING_IMAGE, "approved_by": ["brianboti"]}}}
    done = c.status(brian, plan_id)
    assert done["state"] == "succeeded" and gh.receipt_from == "lab/platform" and done["checks"]["provenance"] == "passed"
    assert "serves all production traffic" in done["next_action"] and "approved by brianboti" in done["next_action"]
    assert "**`demo` is live in production**" in chat.release(done) and "Approved by `brianboti`" in chat.release(done)
    assert c.store.latest_success("demo", kind="promotion")["id"] == plan_id and c.store.latest_success("demo")["id"] == STAGING_PLAN
    listed = c.applications(brian)["demo"]
    assert listed["production_release"]["revision"] == "demo-r1234" and listed["production_release"]["approved_by"] == ["brianboti"]
    assert listed["live_release"]["plan_id"] == STAGING_PLAN
    assert "`demo` in production:" in chat.applications(c.applications(brian))


def test_a_rejected_promotion_changed_nothing_and_a_cancel_reaches_the_platform_run(tmp_path):
    c, brian, gh = ready_controller(tmp_path)
    staging_release(c)
    plan_id = c.promote(brian, "demo")["id"]
    gh.run = {"id": 77, "status": "in_progress", "html_url": RUN, "jobs": []}
    c.cancel(brian, plan_id)
    assert gh.cancelled == [("lab/platform", 77)]
    gh.run = {"id": 77, "status": "completed", "conclusion": "failure", "html_url": RUN, "jobs": [{"name": "describe", "steps": []},
                                                                                                    {"name": "promote", "steps": []}]}
    rejected = c.status(brian, plan_id)
    assert rejected["state"] == "failed" and rejected["next_action"].startswith("Nothing ran in production")
    assert "did not run" in chat.release(rejected) and getattr(gh, "recovery_workflow", None) is None  # no staging recovery check for production


def test_the_github_adapter_dispatches_promote_and_reads_who_may_approve(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-credential")
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path.endswith("/dispatches"):
            return httpx.Response(200, json={"workflow_run_id": 77, "html_url": RUN})
        if request.url.path.endswith("/pending_deployments"):
            return httpx.Response(200, json=[{"environment": {"name": "production-demo"}, "reviewers": [{"type": "User", "reviewer": {"login": "brianboti"}}]}])
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": []})
        if request.url.path == "/repos/lab/platform/actions/runs/77":
            return httpx.Response(200, json={"id": 77, "status": "waiting", "display_title": "promote-" + "9" * 32, "html_url": RUN, "head_sha": "c" * 40})
        if request.url.path == "/repos/lab/platform/environments/production-demo":
            return httpx.Response(404, json={})
        return httpx.Response(200, json=[])
    gh = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    plan = {"id": "9" * 32, "kind": "promotion", "pipeline": "lab/platform", "sha": COMMIT, "spec_hash": "f" * 64, "repository": "lab/demo",
            "target": {**TARGET, "project_id": "lab-deploy-prod-abcd"}, "bake_seconds": 300, "layout": {"path": ""},
            "source": {"plan": STAGING_PLAN, "image": STAGING_IMAGE, "workflow": "deploy-gcp.yml", "provenance": "github"}}
    gh.dispatch_promotion("lab/platform", plan)
    method, path, body = seen[-1]
    assert path == "/repos/lab/platform/actions/workflows/promote.yml/dispatches" and body["ref"] == "main"
    assert body["inputs"] == {"plan_id": "9" * 32, "application": "demo", "source_plan": STAGING_PLAN, "source_image": STAGING_IMAGE, "commit_sha": COMMIT,
                              "spec_hash": "f" * 64, "target_json": json.dumps(plan["target"], sort_keys=True), "repository": "lab/demo", "app_path": "",
                              "contract_commit": "", "staging_workflow": "deploy-gcp.yml", "provenance": "github", "bake_seconds": "300"}
    assert "attested_commit" not in body["inputs"]  # a release built at the tip of main is promoted as before
    gh.dispatch_promotion("lab/platform", {**plan, "source": {**plan["source"], "attested_commit": "9" * 40}})
    assert seen[-1][2]["inputs"]["attested_commit"] == "9" * 40
    found = gh.status(None, {**plan, "dispatch_result": {"workflow_run_id": 77}})
    assert found["status"] == "waiting" and found["pending"] == [{"environment": "production-demo", "reviewers": ["brianboti"]}]
    assert gh.deployment_environment("lab/platform", "production-demo") is None


# ---- 16.2 what the promote workflow runs
def env(**extra):
    values = {"PLAN_ID": "9" * 32, "SOURCE_PLAN": STAGING_PLAN, "APPLICATION": "demo", "COMMIT_SHA": COMMIT, "SPEC_HASH": "f" * 64,
              "SOURCE_REPOSITORY": "lab/demo", "GITHUB_REF": "refs/heads/main", "APP_PATH": "", "CONTRACT_COMMIT": "", "STAGING_WORKFLOW": "deploy-gcp.yml",
              "PROVENANCE_INPUT": "github", "BAKE_SECONDS": "300", "SOURCE_IMAGE": STAGING_IMAGE, "GITHUB_REPOSITORY": "lab/platform",
              "TARGET_JSON": json.dumps({**TARGET, "project_id": "lab-deploy-prod-abcd"}), "GITHUB_RUN_ID": "77"}
    values.update(extra)
    return values


def test_promotion_inputs_are_checked_before_anything_runs():
    facts = promotion.check(env())
    assert facts["staging_project"] == "test-project" and facts["digest"] == DIGEST and facts["contract"] is None
    for change, message in (({"SOURCE_IMAGE": STAGING_IMAGE.replace("test-project", "lab-deploy-prod-abcd")}, "already in the production project"),
                            ({"SOURCE_IMAGE": STAGING_IMAGE.replace("/demo@", "/other@")}, "belongs to other"),
                            ({"SOURCE_IMAGE": "docker.io/library/demo:latest"}, "by digest"),
                            ({"PROVENANCE_INPUT": "unavailable"}, "signed provenance"),
                            ({"CONTRACT_COMMIT": CONTRACT}, "deploy.yml and signed with its Cloud KMS key"),
                            ({"BAKE_SECONDS": "1800"}, "0 to 900"), ({"GITHUB_REF": "refs/heads/feature"}, "from main only"),
                            ({"APP_PATH": "../x"}, "folder"), ({"STAGING_WORKFLOW": "evil.yml"}, "staging workflow"),
                            ({"ATTESTED_COMMIT": "abc"}, "attested_commit"), ({"ATTESTED_COMMIT": "9" * 40, "PROVENANCE_INPUT": "cosign-kms"}, "attested_commit")):
        with pytest.raises(promotion.PromotionError, match=message):
            promotion.check(env(**change))
    summary = promotion.summary(env(), SPEC, {"idle_floor_usd_month": 0.0, "compute_ceiling_usd_month": 9.5, "needs_floor_usd_month": 0.0})
    assert summary.startswith("### Promote `demo` to production") and "staging release `eeeeeeee`" in summary and "at most $9.50" in summary


def envelope(statement):
    return json.dumps({"payloadType": provenance.PAYLOAD_TYPE, "payload": base64.b64encode(json.dumps(statement).encode()).decode()})


def statement(image_name, **predicate):
    return {"_type": "https://in-toto.io/Statement/v1", "predicateType": provenance.PREDICATE_TYPE,
            "subject": [{"name": image_name, "digest": {"sha256": "d" * 64}}], "predicate": provenance.predicate(**predicate)}


def test_staging_provenance_is_verified_for_an_applications_own_pipeline_and_the_platforms():
    name = "us-central1-docker.pkg.dev/test-project/demo/demo"
    common = {"commit": COMMIT, "plan_id": STAGING_PLAN, "spec_hash": "f" * 64, "run_id": "5", "run_attempt": "1", "repository_id": "1", "owner_id": "2"}
    own = statement(name, repository="lab/demo", workflow_ref="lab/demo/.github/workflows/deploy-gcp.yml@refs/heads/main",
                    path=".github/workflows/deploy-gcp.yml", **common)
    assert promotion.verify_staging(envelope(own), env(PROVENANCE_INPUT="cosign-kms"))["predicate"]["runDetails"]
    central = statement(name, repository="lab/platform", workflow_ref="lab/platform/.github/workflows/deploy.yml@refs/heads/main",
                        path=".github/workflows/deploy.yml", source_repository="lab/demo", contract_commit=CONTRACT, application="demo", **common)
    platform_env = env(PROVENANCE_INPUT="cosign-kms", CONTRACT_COMMIT=CONTRACT, STAGING_WORKFLOW="deploy.yml")
    assert promotion.verify_staging(envelope(central), platform_env)
    with pytest.raises(provenance.ProvenanceError, match="different contract"):
        promotion.verify_staging(envelope(central), {**platform_env, "CONTRACT_COMMIT": "c" * 40})
    with pytest.raises(provenance.ProvenanceError, match="different repository, workflow or branch"):
        promotion.verify_staging(envelope(own), platform_env)


def test_the_promotion_statement_names_the_source_and_who_approved_it():
    approvals = [{"state": "approved", "user": {"login": "brianboti"}, "environments": [{"name": "production-demo"}]},
                 {"state": "rejected", "user": {"login": "mallory"}, "environments": [{"name": "production-demo"}]},
                 {"state": "approved", "user": {"login": "someone"}, "environments": [{"name": "staging-demo"}]}]
    assert promotion.approvers(approvals, "production-demo") == ["brianboti"] and promotion.approvers([], "production-demo") == []
    with pytest.raises(promotion.PromotionError, match="No approval recorded"):
        promotion.predicate(env())
    body = promotion.predicate(env(APPROVED_BY="brianboti"))
    assert body["verificationResult"] == "PASSED" and body["resourceUri"] == f"oci://{STAGING_IMAGE}"
    assert body["lab_deploy"]["approved_by"] == ["brianboti"] and body["lab_deploy"]["source"]["plan_id"] == STAGING_PLAN
    assert body["verifier"]["id"] == "https://github.com/lab/platform/.github/workflows/promote.yml@refs/heads/main"


def test_the_receipt_records_what_was_promoted_and_the_applications_repository():
    prod = STAGING_IMAGE.replace("test-project", "lab-deploy-prod-abcd")
    assert promotion_from({"SOURCE_PLAN": STAGING_PLAN, "SOURCE_IMAGE": prod, "APPROVED_BY": "brianboti,bad login"}) == {
        "source_plan": STAGING_PLAN, "source_image": prod, "approved_by": ["brianboti"]}
    assert promotion_from({}) is None
    origin = origin_from({"GITHUB_REPOSITORY": "lab/platform", "GITHUB_RUN_ID": "77", "SOURCE_REPOSITORY": "lab/demo", "COMMIT_SHA": COMMIT})
    assert origin["repository"] == "lab/demo" and origin["run_url"] == RUN
    assert origin_from({"GITHUB_REPOSITORY": "lab/demo", "GITHUB_RUN_ID": "5"})["repository"] == "lab/demo"  # a staging release as before


# ---- 16.2 the staging root's read access, and nothing more
ROOT = dict(region="us-central1", github_repository="lab/demo", github_repository_id="123", github_owner_id="45",
            bootstrap_image="us-docker.pkg.dev/cloudrun/container/hello@sha256:" + "a" * 64, state_bucket="test-project-tfstate")


def test_a_staging_root_lets_the_production_deployer_read_its_images_and_nothing_more():
    reader = "demo-deploy@lab-deploy-prod-abcd.iam.gserviceaccount.com"
    main = app_root("demo", project_id="test-project", promotion_readers=[reader], **ROOT)["main.tf"]
    assert f'promotion_readers            = ["{reader}"]' in main
    assert "promotion_readers" not in app_root("demo", project_id="test-project", **ROOT)["main.tf"]  # roots without production stay as they were
    with pytest.raises(ValueError, match="another project's service account"):
        app_root("demo", project_id="test-project", promotion_readers=["demo-deploy@test-project.iam.gserviceaccount.com"], **ROOT)
    module = Path("infra/gcp/modules/service/promotion.tf").read_text()
    assert '"roles/artifactregistry.reader"' in module and '["roles/cloudkms.viewer", "roles/cloudkms.publicKeyViewer"]' in module
    assert "roles/cloudkms.publicKeyViewer" in ALLOWED_ROLES and "roles/cloudkms.signerVerifier" in ALLOWED_ROLES
    grant = {"address": "module.app.google_kms_crypto_key_iam_member.promotion_verifier[\"x\"]", "type": "google_kms_crypto_key_iam_member",
             "change": {"actions": ["create"], "after": {"role": "roles/cloudkms.publicKeyViewer", "member": f"serviceAccount:{reader}", "crypto_key_id": "k"}}}
    assert check_plan({"resource_changes": [grant]}) == []
    signer = {**grant, "change": {"actions": ["create"], "after": {**grant["change"]["after"], "role": "roles/cloudkms.admin"}}}
    assert [v["rule"] for v in check_plan({"resource_changes": [signer]})] == ["role_not_allowed"]


def test_the_promote_workflow_waits_for_approval_before_any_cloud_step():
    workflow = yaml.safe_load(Path(".github/workflows/promote.yml").read_text())
    text = Path(".github/workflows/promote.yml").read_text()
    assert workflow["run-name"] == "promote-${{ inputs.plan_id }}" and workflow["concurrency"]["group"] == "gcp-production-${{ inputs.application }}"
    assert workflow["permissions"] == {"contents": "read"}
    describe, promote = workflow["jobs"]["describe"], workflow["jobs"]["promote"]
    assert "environment" not in describe and "permissions" not in describe  # no cloud identity before a reviewer approves
    assert promote["environment"]["name"] == "production-${{ inputs.application }}" and promote["needs"] == "describe"
    assert promote["permissions"] == {"contents": "read", "id-token": "write", "actions": "read"}
    names = [step.get("name") or step.get("uses") or step.get("run") for step in promote["steps"]]
    order = ["Refuse to run without a reviewer's approval", "Validate the production target", next(n for n in names if n.startswith("google-github-actions/auth")),
             "Verify the staging image's provenance", "Copy the image into production by digest", "Sign the promotion with production's key",
             "Deploy, probe, promote; restore traffic on failure"]
    assert [names.index(step) for step in order] == sorted(names.index(step) for step in order)
    deploy = Path(".github/workflows/deploy.yml").read_text()
    cosign = "4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71"
    assert cosign in text and cosign in deploy and "--prefer-index=false" in text
    assert "docker build " not in text and "pack" not in text and "docker push" not in text  # copied, never built
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            assert "${{ inputs." not in step.get("run", "")  # inputs reach scripts through the environment only
            if "uses" in step:
                assert "@" in step["uses"] and len(step["uses"].split("@")[1].split()[0]) == 40  # pinned by commit


def test_plans_stay_staging_only_and_production_takes_the_same_limits(tmp_path):
    enforce(SPEC, "gcp", "production")
    big = AppSpec.model_validate({"name": "demo", "reviewed": True, "resources": {"cpu": 4, "memory_mib": 2048}})
    with pytest.raises(ValueError, match="Production policy: maximum 2 CPUs"):
        enforce(big, "gcp", "production")
    with pytest.raises(ValueError, match="GCP staging"):
        enforce(SPEC, "aws", "staging")
    c, brian, gh = ready_controller(tmp_path)
    with pytest.raises(ValueError, match="production is reached by promote_to_production"):
        c.plan(brian, "demo", "gcp", "production")


def test_the_status_of_a_promotion_reads_its_own_steps():
    waiting = summarize({"status": "waiting", "jobs": []}, {"approval": "pending"}, "promotion")
    assert waiting["state"] == "awaiting_approval" and waiting["checks"]["provenance"] == "pending"
    failed = summarize({"status": "completed", "conclusion": "failure", "jobs": [{"name": "promote", "steps": [
        {"name": "Refuse to run without a reviewer's approval", "status": "completed", "conclusion": "success"},
        {"name": "Verify the staging image's provenance", "status": "completed", "conclusion": "failure"}]}]}, {}, "promotion")
    assert failed["state"] == "failed" and failed["checks"]["approval"] == "passed" and failed["checks"]["provenance"] == "failed"
    text = advice({"kind": "promotion", **failed})
    assert text.startswith("The staging image's provenance could not be verified, so nothing was copied or deployed")
    assert "verifying the staging image's signature" in chat.release({"kind": "promotion", "application": "demo", **failed})
