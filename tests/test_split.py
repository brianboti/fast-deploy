"""Phase 16, part three: the split release and production's rollback (docs/ROADMAP.md D38, roadmap item 16.3)."""
import json
import time
from pathlib import Path

import httpx
import pytest
import yaml
from conftest import TARGET
from test_phase9 import IMAGE, PLAN_ID, REVISION, Clock, Cloud, requests
from test_promotion import RUN, STAGING_PLAN, env, ready_controller, staging_release

from labdeploy import chat, promotion
from labdeploy.github import GitHub
from labdeploy.models import AppSpec
from labdeploy.release import CloudRun, parse_steps, rollout, shares
from labdeploy.status import advice, summarize


# ---- the steps and their traffic
def test_each_step_keeps_the_earlier_split_in_its_proportions_in_whole_percents():
    assert shares({"demo-a": 100}, "demo-new", 10) == {"demo-a": 90, "demo-new": 10}
    assert shares({"demo-a": 70, "demo-b": 30}, "demo-new", 50) == {"demo-a": 35, "demo-b": 15, "demo-new": 50}
    odd = shares({"demo-a": 67, "demo-b": 33}, "demo-new", 10)
    assert sum(odd.values()) == 100 and odd["demo-new"] == 10 and odd == {"demo-a": 60, "demo-b": 30, "demo-new": 10}
    assert shares({"demo-a": 100}, "demo-new", 100) == {"demo-new": 100}
    with pytest.raises(ValueError, match="whole percent"):
        shares({"demo-a": 100}, "demo-new", 0)
    with pytest.raises(ValueError, match="already be serving"):
        shares({"demo-new": 100}, "demo-new", 10)
    assert parse_steps("") == [100] and parse_steps("10,50,100") == [10, 50, 100]
    for bad in ("50,10,100", "10,50", "ten", "0,100"):
        with pytest.raises(ValueError, match="ROLLOUT_STEPS"):
            parse_steps(bad)


class SplitCloud(Cloud):
    """The bake tests' adapter, remembering which address each probe asked and serving what `window` gives for the
    current share of the new revision."""

    def __init__(self, window):
        super().__init__(lambda elapsed: [])
        self.share_window, self.probes = window, []

    def health(self, url, audience, path):
        self.probes.append(url)
        return True

    def revision_requests(self, revisions, since, until=None):
        if until is not None:
            return []
        share = (self.moves[-1] if self.moves else {}).get(REVISION, 0)
        return self.share_window(share, self.clock() - since)


def split_release(cloud, tmp_path, steps=(10, 50, 100), seconds=300):
    clock = Clock()
    cloud.clock = clock
    return rollout(cloud, IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "receipt.json", bake_seconds=seconds,
                   sleep=clock.sleep, clock=clock, steps=list(steps))


def test_a_split_release_takes_each_share_in_turn_and_watches_it(tmp_path):
    cloud = SplitCloud(lambda share, elapsed: requests(40))
    receipt = split_release(cloud, tmp_path)
    assert cloud.moves == [{"demo-old": 100}, {"demo-old": 90, REVISION: 10}, {"demo-old": 50, REVISION: 50}, {REVISION: 100}]
    # while the traffic is split the candidate's own address is probed; the service's address once it serves everyone
    assert cloud.probes == ["https://candidate-demo.run.app", "https://candidate-demo.run.app", "https://candidate-demo.run.app", "https://demo.run.app"]
    assert receipt["state"] == "succeeded" and "step" not in receipt
    assert [(step["percent"], step["bake"], step["requests"]) for step in receipt["rollout"]] == [(10, "passed", 40), (50, "passed", 40), (100, "passed", 40)]
    assert [(phase["state"], phase.get("step")) for phase in receipt["phases"]][2:8] == [
        ("promoting", 10), ("baking", 10), ("promoting", 50), ("baking", 50), ("promoting", 100), ("baking", 100)]


def test_a_broken_rule_at_any_step_brings_back_the_earlier_split(tmp_path):
    cloud = SplitCloud(lambda share, elapsed: requests(30, status=500) + requests(10) if share == 10 else requests(40))
    with pytest.raises(RuntimeError, match="traffic_restored"):
        split_release(cloud, tmp_path)
    saved = json.loads((tmp_path / "receipt.json").read_text())
    assert cloud.moves[-1] == {"demo-old": 100} and {REVISION: 100} not in cloud.moves
    assert saved["state"] == "traffic_restored" and saved["failed_phase"] == "baking" and saved["step"] == 10 and saved["bake"]["rule"] == "errors"
    text = advice({"kind": "promotion", "state": "failed", "receipt": saved, "failed_step": {"step": "Deploy, probe, promote; restore traffic on failure"}})
    assert text.startswith("The new revision stopped with 10 percent of production's traffic") and "earlier split serves everyone again" in text
    shown = chat.release({"kind": "promotion", "application": "demo", "state": "failed", "receipt": saved, "phases": {"state": "traffic_restored"}})
    assert "failed its watch with 10% of the traffic" in shown and "previous version is serving" in shown


def test_a_staging_release_still_moves_everyone_at_once(tmp_path):
    cloud = SplitCloud(lambda share, elapsed: requests(40))
    receipt = split_release(cloud, tmp_path, steps=(100,))
    assert cloud.moves == [{"demo-old": 100}, {REVISION: 100}] and "rollout" not in receipt and "step" not in receipt["phases"][-1]


def test_production_runs_each_secret_at_the_version_newest_at_its_release():
    asked = []

    def runner(args):
        asked.append(args)
        if args[:4] == ["gcloud", "secrets", "versions", "list"]:
            return "projects/p/secrets/demo-API_KEY/versions/7"
        return "{}"
    spec = AppSpec.model_validate({"name": "demo", "reviewed": True, "secrets": ["API_KEY"]})
    pinned = CloudRun("p", "us-central1", "demo", "demo-probe@p.iam.gserviceaccount.com", runner=runner, pin_secrets=True)
    assert "--set-secrets=API_KEY=demo-API_KEY:7" in pinned.configuration(spec)
    assert "--set-secrets=API_KEY=demo-API_KEY:7" in pinned.configuration(spec)  # the migration and the revision get the same version
    assert sum(1 for args in asked if args[:4] == ["gcloud", "secrets", "versions", "list"]) == 1
    assert pinned.pinned == {"API_KEY": {"secret": "demo-API_KEY", "version": "7"}}
    staging = CloudRun("p", "us-central1", "demo", "demo-probe@p.iam.gserviceaccount.com", runner=runner)
    assert "--set-secrets=API_KEY=demo-API_KEY:latest" in staging.configuration(spec)


# ---- the workflow's inputs for a rollback, and the approval block it writes
def test_a_rollback_names_the_promotion_it_undoes_and_the_split_coming_back():
    rollback = {"OPERATION": "rollback", "PLAN_ID": "7" * 32, "APPLICATION": "demo", "GITHUB_REF": "refs/heads/main", "ROLLBACK_PLAN": "6" * 32,
                "TARGET_JSON": json.dumps({**TARGET, "project_id": "lab-deploy-prod-abcd"}), "ROLLBACK_TO": json.dumps({"demo-r1": 100})}
    facts = promotion.check(rollback)
    assert facts["operation"] == "rollback" and facts["rollback_plan"] == "6" * 32 and facts["rollback_to"] == {"demo-r1": 100}
    block = promotion.summary(rollback)
    assert block.startswith("### Roll `demo` back in production") and "`demo-r1` 100%" in block and "promotion `66666666`" in block
    for change, message in (({"ROLLBACK_PLAN": "x"}, "promotion to roll back"), ({"ROLLBACK_TO": "{\"demo-r1\": 50}"}, "adds up to 100"),
                            ({"OPERATION": "destroy"}, "promote or rollback")):
        with pytest.raises(promotion.PromotionError, match=message):
            promotion.check({**rollback, **change})
    assert promotion.check(env(ROLLOUT_STEPS="10,50,100"))["rollout"] == [10, 50, 100]
    with pytest.raises(promotion.PromotionError, match="rising list"):
        promotion.check(env(ROLLOUT_STEPS="50,10,100"))
    assert "10% then 50% then 100% of the traffic" in promotion.summary(env(ROLLOUT_STEPS="10,50,100"))


def test_the_workflow_promotes_or_rolls_back_through_the_one_file_production_trusts():
    text = Path(".github/workflows/promote.yml").read_text()
    workflow = yaml.safe_load(text)
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert workflow["run-name"] == "${{ inputs.operation }}-${{ inputs.plan_id }}" and len(inputs) <= 25
    assert inputs["operation"]["options"] == ["promote", "rollback"] and inputs["operation"]["default"] == "promote"
    assert inputs["rollout"]["default"] == "10,50,100"
    steps = {step.get("name") or step.get("uses") or step.get("run"): step for step in workflow["jobs"]["promote"]["steps"]}
    assert workflow["jobs"]["promote"]["timeout-minutes"] == 75
    roll = steps["Roll production's traffic back"]
    assert roll["if"] == "inputs.operation == 'rollback'" and roll["env"]["RECOVERY_ONLY"] == "true" and "ROLLBACK_PLAN_ID" in roll["env"]
    for name in ("Verify the staging image's provenance", "Copy the image into production by digest", "Sign the promotion with production's key",
                 "Deploy, probe, promote; restore traffic on failure", "Recover any interrupted previous release"):
        assert steps[name]["if"] == "inputs.operation == 'promote'"
    assert "if" not in steps["Refuse to run without a reviewer's approval"]  # a rollback waits for a reviewer as well
    assert steps["Deploy, probe, promote; restore traffic on failure"]["env"]["PIN_SECRET_VERSIONS"] == "true"


# ---- the controller's production rollback
def promoted(tmp_path):
    c, brian, gh = ready_controller(tmp_path)
    staging_release(c)
    plan_id = c.promote(brian, "demo")["id"]
    steps = [{"name": name, "status": "completed", "conclusion": "success"} for name in (
        "Refuse to run without a reviewer's approval", "Verify the staging image's provenance", "Copy the image into production by digest",
        "Deploy, probe, promote; restore traffic on failure")]
    gh.run = {"id": 77, "status": "completed", "conclusion": "success", "html_url": RUN, "created_at": "2026-10-18T11:00:00Z",
              "updated_at": "2026-10-18T11:30:00Z", "jobs": [{"name": "promote", "steps": steps}]}
    gh.receipt = {"release": {"state": "succeeded", "revision": "demo-r2", "url": "https://demo-prod.a.run.app", "previous_traffic": {"demo-r1": 100},
                              "promotion": {"source_plan": STAGING_PLAN, "approved_by": ["brianboti"]},
                              "rollout": [{"percent": 10, "bake": "passed"}, {"percent": 50, "bake": "passed"}, {"percent": 100, "bake": "passed"}]}}
    done = c.status(brian, plan_id)
    assert done["state"] == "succeeded" and "Took 10%, 50% and 100% of the traffic in turn" in chat.release(done)
    return c, brian, gh, plan_id


def test_production_rolls_back_through_promote_yml_after_an_approval(tmp_path):
    c, brian, gh, promotion_id = promoted(tmp_path)
    rollbacks = []
    gh.dispatch_production_rollback = lambda repository, record: rollbacks.append((repository, record)) or {"workflow_run_id": 88, "html_url": RUN}
    record = c.rollback_production(brian, "demo")
    repository, sent = rollbacks[-1]
    assert repository == "lab/platform" and record["kind"] == "production_rollback" and record["state"] == "dispatched"
    assert sent["from_plan"] == promotion_id and sent["to_traffic"] == {"demo-r1": 100} and sent["target"]["project_id"] == "lab-deploy-prod-abcd"
    assert "Waiting for approval: roll `demo` back in production" in chat.release(record)
    gh.run = {"id": 88, "status": "waiting", "html_url": RUN, "jobs": []}
    waiting = c.status(brian, record["id"])
    assert waiting["state"] == "awaiting_approval" and waiting["next_action"].startswith("Waiting for brianboti to approve the rollback")
    fetched = []

    def artifact_json(app, run_id, name, filenames):
        fetched.append((app.pipeline_repo, name))
        return {"recovery-receipt.json": {"state": "rolled_back", "restored_traffic": {"demo-r1": 100}, "rolled_back_from": "demo-r2"}}
    gh.artifact_json = artifact_json
    gh.run = {"id": 88, "status": "completed", "conclusion": "success", "html_url": RUN, "jobs": []}
    done = c.status(brian, record["id"])
    assert done["state"] == "succeeded" and fetched == [("lab/platform", "recovery-receipt")]
    assert "back on its previous version in production" in chat.release(done)
    assert c.store.latest_success("demo", kind="promotion") is None  # the promotion rolled back is no longer production's live release
    with pytest.raises(ValueError, match="Nothing promoted to production"):
        c.rollback_production(brian, "demo")


def test_a_production_rollback_needs_production_a_promotion_and_its_earlier_split(tmp_path):
    c, brian, gh = ready_controller(tmp_path)
    with pytest.raises(ValueError, match="Nothing promoted to production"):
        c.rollback_production(brian, "demo")
    record = {"id": "8" * 32, "kind": "promotion", "application": "demo", "sha": "a" * 40, "expires_at": time.time(),
              "receipt": {"state": "succeeded", "revision": "demo-r2"}}
    c.store.add("brian", record, "succeeded")
    with pytest.raises(ValueError, match="records no earlier split"):
        c.rollback_production(brian, "demo")
    assert summarize({"status": "waiting", "jobs": []}, {}, "production_rollback")["state"] == "awaiting_approval"


def test_the_github_adapter_starts_a_rollback_and_names_the_split_steps(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-credential")
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path == "/repos/lab/platform/actions/runs/88":
            return httpx.Response(200, json={"id": 88, "status": "completed", "display_title": "rollback-" + "7" * 32, "html_url": RUN})
        return httpx.Response(200, json={"jobs": []} if request.url.path.endswith("/jobs") else {})
    gh = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    target = {**TARGET, "project_id": "lab-deploy-prod-abcd"}
    gh.dispatch_production_rollback("lab/platform", {"id": "7" * 32, "target": target, "from_plan": "6" * 32, "to_traffic": {"demo-r1": 100}})
    assert seen[-1][1]["inputs"] == {"operation": "rollback", "plan_id": "7" * 32, "application": "demo", "target_json": json.dumps(target, sort_keys=True),
                                     "rollback_plan": "6" * 32, "rollback_to": "{\"demo-r1\": 100}"}
    found = gh.status(None, {"id": "7" * 32, "kind": "production_rollback", "pipeline": "lab/platform", "dispatch_result": {"workflow_run_id": 88}})
    assert found["status"] == "completed"
    plan = {"id": "9" * 32, "pipeline": "lab/platform", "sha": "a" * 40, "spec_hash": "f" * 64, "repository": "lab/demo", "target": target,
            "bake_seconds": 300, "layout": {"path": ""}, "rollout": [10, 50, 100],
            "source": {"plan": STAGING_PLAN, "image": IMAGE, "workflow": "deploy-gcp.yml", "provenance": "github"}}
    gh.dispatch_promotion("lab/platform", plan)
    assert seen[-1][1]["inputs"]["rollout"] == "10,50,100"
