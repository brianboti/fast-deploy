"""Phase 2: traffic rollback, deploy by commit, image reuse, drift, and the workflow changes behind them."""
import hashlib
import json
from pathlib import Path

import httpx
import pytest
import yaml
from conftest import TARGET, FakeGitHub
from test_release import IMAGE, PLAN_ID

from labdeploy.config import Config
from labdeploy.controller import Controller, Store
from labdeploy.cost import drift, summary_lines
from labdeploy.github import GitHub
from labdeploy.models import AppSpec
from labdeploy.release import recover, rollback
from labdeploy.status import summarize


class Journal:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value

    def write(self, payload):
        self.value = dict(payload)


class Live:
    service = "demo"

    def __init__(self, traffic):
        self.traffic_state, self.moves = dict(traffic), []

    def describe(self):
        return {"status": {"traffic": [{"revisionName": k, "percent": v} for k, v in self.traffic_state.items()]}}

    def traffic(self, allocations):
        self.moves.append(dict(allocations))
        self.traffic_state = dict(allocations)

    def ready(self):
        return True


def checkpoint(**extra):
    return {"plan_id": PLAN_ID, "state": "succeeded", "revision": "demo-r2", "previous_traffic": {"demo-r1": 100}, "target": TARGET, **extra}


def test_rollback_restores_the_previous_split_and_is_idempotent():
    cloud, journal = Live({"demo-r2": 100}), Journal(checkpoint())
    result = rollback(cloud, journal, PLAN_ID)
    assert result["state"] == "rolled_back" and cloud.moves == [{"demo-r1": 100}] and result["rolled_back_from"] == "demo-r2"
    assert journal.value["state"] == "rolled_back" and result["restored_traffic"] == {"demo-r1": 100} and result["service_ready"] is True
    again = rollback(cloud, journal, PLAN_ID)
    assert "already rolled back" in again["note"] and cloud.moves == [{"demo-r1": 100}]
    assert recover(cloud, journal, TARGET)["state"] == "no_recovery_needed"  # a rollback is a finished state, not an interrupted release


def test_rollback_refuses_the_wrong_release_an_incomplete_release_or_moved_traffic():
    with pytest.raises(ValueError, match="nothing to roll back"):
        rollback(Live({"demo-r2": 100}), Journal(None), PLAN_ID)
    with pytest.raises(ValueError, match="not " + PLAN_ID[:8]):
        rollback(Live({"demo-r2": 100}), Journal(checkpoint(plan_id="b" * 32)), PLAN_ID)
    with pytest.raises(ValueError, match="not a completed release"):
        rollback(Live({"demo-r2": 100}), Journal(checkpoint(state="traffic_restored")), PLAN_ID)
    moved = Live({"demo-r3": 100})
    with pytest.raises(ValueError, match="no longer only on this release"):
        rollback(moved, Journal(checkpoint()), PLAN_ID)
    assert moved.moves == []
    already = Live({"demo-r1": 100})  # someone moved traffic back by hand: record it, change nothing
    assert rollback(already, Journal(checkpoint()), PLAN_ID)["state"] == "rolled_back" and already.moves == []
    with pytest.raises(ValueError, match="Invalid plan"):
        rollback(already, Journal(checkpoint()), "short")


class History(FakeGitHub):
    def __init__(self):
        self.dispatched, self.rollback_receipt = [], None

    def dispatch_workflow(self, app, workflow, inputs):
        self.dispatched.append((workflow, inputs))
        return {"workflow_run_id": 777}

    def artifact_json(self, app, run_id, name, filenames, limit=None):
        return {"recovery-receipt.json": self.rollback_receipt} if self.rollback_receipt else None


def make(tmp_path, github=None):
    config = Config.model_validate({
        "live_dispatch": True, "database": str(tmp_path / "db"),
        "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
        "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True}]})
    github = github or History()
    return Controller(config, github, Store(config.database)), config.principals[0], github


def release(c, p, gh, sha, revision, image=IMAGE):
    gh.sha = sha
    plan = c.plan(p, "demo")
    c.deploy(p, plan["id"])
    gh.receipt = {"release": {"plan_id": plan["id"], "state": "succeeded", "revision": revision, "image": image, "previous_traffic": {"demo-r0": 100}}, "vulnerabilities": None}
    assert c.status(p, plan["id"])["state"] == "succeeded"
    return plan


def test_rollback_record_follows_the_recovery_run_and_updates_the_live_release(tmp_path):
    c, p, gh = make(tmp_path)
    with pytest.raises(ValueError, match="No release recorded"):
        c.rollback(p, "demo")
    first = release(c, p, gh, "1" * 40, "demo-r1")
    second = release(c, p, gh, "2" * 40, "demo-r2")
    assert c.applications(p)["demo"]["live_release"]["plan_id"] == second["id"]
    record = c.rollback(p, "demo")
    assert record["kind"] == "rollback" and record["from_plan"] == second["id"] and record["to_traffic"] == {"demo-r0": 100}
    assert gh.dispatched == [("recover-gcp.yml", {"rollback_plan_id": second["id"]})] and "within about a minute" in record["next_action"]
    gh.rollback_receipt = {"plan_id": second["id"], "state": "rolled_back", "rolled_back_from": "demo-r2", "restored_traffic": {"demo-r0": 100}}
    status = c.status(p, record["id"])
    assert status["state"] == "succeeded" and status["checks"] == {} and "Traffic is back on" in status["next_action"]
    assert c.applications(p)["demo"]["live_release"]["plan_id"] == first["id"]  # the rolled-back release no longer counts as live
    listed = {x["id"]: x for x in c.plans(p)["plans"]}
    assert listed[record["id"]]["state"] == "succeeded" and listed[second["id"]]["state"] == "succeeded"
    assert summarize({"status": "completed", "conclusion": "failure", "jobs": []}, {}, "rollback")["state"] == "failed"


def test_plan_by_commit_requires_main_history_and_is_rechecked_at_dispatch(tmp_path):
    c, p, gh = make(tmp_path)
    plan = c.plan(p, "demo", commit_sha="c" * 40)
    assert plan["pinned_commit"] is True and plan["behind_branch_by"] == 2 and plan["sha"] == "c" * 40
    assert c.deploy(p, plan["id"])["state"] == "dispatched"  # the branch tip differs from the pinned commit, and that is fine
    with pytest.raises(ValueError, match="full 40-character"):
        c.plan(p, "demo", commit_sha="abc")
    gh.compare = lambda app, base, head=None: {"status": "diverged", "ahead_by": 1, "behind_by": 1, "commits": [], "files": []}
    with pytest.raises(ValueError, match="not part of main"):
        c.plan(p, "demo", commit_sha="d" * 40)
    pinned = c.plan(p, "demo")
    gh.compare = lambda app, base, head=None: {"status": "ahead", "ahead_by": 1, "behind_by": 0, "commits": [], "files": []}
    older = c.plan(p, "demo", commit_sha="e" * 40)
    gh.compare = lambda app, base, head=None: {"status": "diverged", "ahead_by": 1, "behind_by": 1, "commits": [], "files": []}
    with pytest.raises(ValueError, match="not part of main"):
        c.deploy(p, older["id"])
    assert pinned["pinned_commit"] is False


def test_a_commit_released_before_reuses_its_attested_image(tmp_path):
    c, p, gh = make(tmp_path)
    first = release(c, p, gh, "1" * 40, "demo-r1", image=IMAGE)
    again = c.plan(p, "demo")
    assert again["reuse_image"] == IMAGE and again["reused_from_plan"] == first["id"]
    assert again["checks"]["tests"] == "passed_in_earlier_release" and again["checks"]["dependency_scan"] == "passed_in_earlier_release"
    foreign = release(c, p, gh, "2" * 40, "demo-r2", image="us-central1-docker.pkg.dev/other/demo/demo@sha256:" + "f" * 64)
    assert "reuse_image" not in c.plan(p, "demo") and foreign["id"]


def test_dispatch_sends_the_image_digest_only_when_reusing(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
    app = Config.model_validate({"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}}, "principals": []}).applications["demo"]
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"workflow_run_id": 5})
    github = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    base = {"sha": "a" * 40, "spec_hash": "b" * 64, "id": PLAN_ID, "target": TARGET}
    github.dispatch(app, base)
    github.dispatch(app, {**base, "reuse_image": IMAGE})
    assert "image_digest" not in bodies[0]["inputs"] and bodies[1]["inputs"]["image_digest"] == IMAGE


def test_github_status_identifies_rollback_runs_by_title(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
    app = Config.model_validate({"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}}, "principals": []}).applications["demo"]

    def handler(request):
        if request.url.path == "/repos/lab/demo/actions/runs/777":
            return httpx.Response(200, json={"id": 777, "status": "completed", "conclusion": "success", "head_sha": "m" * 40, "display_title": "rollback-" + PLAN_ID, "run_attempt": 1})
        if request.url.path == "/repos/lab/demo/actions/runs/777/attempts/1/jobs":
            return httpx.Response(200, json={"jobs": []})
        raise AssertionError(request.url.path)
    github = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    record = {"kind": "rollback", "from_plan": PLAN_ID, "id": "r" * 32, "sha": "2" * 40, "dispatch_result": {"workflow_run_id": 777}}
    assert github.status(app, record)["conclusion"] == "success"
    with pytest.raises(ValueError, match="identity"):
        github.status(app, {"id": PLAN_ID, "sha": "2" * 40, "dispatch_result": {"workflow_run_id": 777}})


def test_drift_lists_live_settings_that_differ_from_the_reviewed_spec():
    spec = AppSpec(name="demo", reviewed=True)
    assert drift({"cpu": 1, "memory_mib": 512, "concurrency": 80, "minimum": 0, "maximum": 2}, spec) == []
    found = drift({"cpu": 2, "memory_mib": 512, "concurrency": 80, "minimum": 1, "maximum": 2}, spec)
    assert found == [{"setting": "cpu", "platform_yaml": 1, "live": 2}, {"setting": "minimum", "platform_yaml": 0, "live": 1}]
    report = {"service": "demo", "window_days": 14, "estimate": {"compute_ceiling_usd_month": 1.0, "idle_floor_usd_month": 0.0},
              "rightsizing": {"observed": None, "recommendations": []}, "drift": found}
    assert any("cpu is 2 live but 1 reviewed" in line for line in summary_lines(report))


def test_workflows_support_reuse_rollback_and_history_checks():
    deploy = yaml.safe_load(Path("templates/deploy-gcp.yml").read_text())
    assert deploy[True]["workflow_dispatch"]["inputs"]["image_digest"]["required"] is False  # PyYAML reads the `on` key as True
    assert deploy["jobs"]["build"]["if"] == "inputs.image_digest == ''"
    assert "needs.build.result != 'failure'" in deploy["jobs"]["deploy"]["if"]
    names = [step.get("name") for step in deploy["jobs"]["deploy"]["steps"]]
    assert "Validate dispatch inputs for reuse" in names and names.index("Validate dispatch inputs for reuse") < names.index("Validate artifact and deployment configuration")
    validate = deploy["jobs"]["build"]["steps"][0]
    assert "compare/$COMMIT_SHA...$GITHUB_SHA" in validate["run"] and 'test "$COMMIT_SHA" = "$GITHUB_SHA"' not in validate["run"]
    recover = yaml.safe_load(Path("templates/recover-gcp.yml").read_text())
    assert recover[True]["workflow_dispatch"]["inputs"]["rollback_plan_id"]["default"] == "" and "rollback-{0}" in recover["run-name"]
    assert recover["jobs"]["recover"]["env"]["ROLLBACK_PLAN_ID"] == "${{ inputs.rollback_plan_id }}"
    assert Path(".github/workflows/ci.yml").read_text().count("templates/*.yml") == 1  # the linter audits the templates as files
