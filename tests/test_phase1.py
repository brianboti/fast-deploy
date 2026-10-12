"""Phase 1: every step visible, release history, commit facts, waiting status, recovery follow-up."""
import copy
import hashlib
import io
import json
import zipfile

import httpx
import pytest
from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient

from labdeploy.config import Config
from labdeploy.controller import Controller, Store
from labdeploy.github import GitHub
from labdeploy.server import build_app
from labdeploy.status import advice, failed_step, release_phases, summarize, timeline

STEP = "Deploy, probe, promote; restore traffic on failure"
RECEIPT = {"plan_id": "p" * 32, "state": "traffic_restored", "error": "Candidate health check failed", "previous_traffic": {"demo-r1": 100},
           "service_ready": False, "candidate_logs": [{"severity": "ERROR", "message": "ModuleNotFoundError: numpy"}]}


def make(tmp_path, github=None):
    config = Config.model_validate({
        "live_dispatch": True, "database": str(tmp_path / "plans.db"),
        "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
        "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True}],
    })
    github = github or FakeGitHub()
    return Controller(config, github, Store(config.database)), config.principals[0], github


def failing_workflow(step=STEP, conclusion="failure", status="completed"):
    def steps(*names):
        return [{"name": n, "status": "completed", "conclusion": "success", "started_at": "2026-10-12T10:00:00Z", "completed_at": "2026-10-12T10:00:30Z"} for n in names]
    build = steps("Run unit tests", "Audit Python dependencies", "Build without cloud credentials")
    deploy = steps("Validate artifact and deployment configuration", "Push image and resolve immutable digest", STEP)
    for item in build + deploy:
        if item["name"] == step:
            item["conclusion"] = "failure"
    return {"id": 123, "status": status, "conclusion": conclusion, "html_url": "https://github.com/lab/demo/actions/runs/123",
            "created_at": "2026-10-12T10:00:00Z", "updated_at": "2026-10-12T10:08:00Z",
            "jobs": [{"name": "build", "steps": build}, {"name": "deploy", "steps": deploy}]}


def test_timeline_names_every_step_with_duration_and_the_first_failure():
    workflow = failing_workflow("Build without cloud credentials")
    items = timeline(workflow)
    assert [i["step"] for i in items][:3] == ["Run unit tests", "Audit Python dependencies", "Build without cloud credentials"]
    assert items[0]["seconds"] == 30.0 and items[0]["job"] == "build"
    assert failed_step(items) == {"job": "build", "step": "Build without cloud credentials"}
    summary = summarize(workflow, {"tests": "pending_in_ci"})
    assert summary["state"] == "failed" and summary["failed_step"]["step"] == "Build without cloud credentials" and len(summary["timeline"]) == 6
    assert summarize({"status": "not_visible_yet"}, {})["timeline"] == []


def test_advice_is_specific_to_the_failure():
    assert "deploy_staging" in advice({"state": "planned"})
    assert "wait_seconds" in advice({"state": "running"})
    assert "Unit tests failed" in advice({"state": "failed", "failed_step": {"job": "build", "step": "Run unit tests"}})
    assert "did not build" in advice({"state": "failed", "failed_step": {"job": "build", "step": "Build without cloud credentials"}})
    assert "Cloud sign-in" in advice({"state": "failed", "failed_step": {"job": "deploy", "step": "Run google-github-actions/auth@7c6bc77"}})
    restored = advice({"state": "failed", "failed_step": {"job": "deploy", "step": STEP}, "receipt": RECEIPT})
    assert "previous revision kept serving" in restored and "Candidate health check failed" in restored and "log lines" in restored
    broken = {"state": "rollback_failed_manual_recovery_required", "restore_error": "boom", "previous_traffic": {"demo-r1": 100}}
    manual = advice({"state": "failed", "failed_step": {"job": "deploy", "step": STEP}, "receipt": broken})
    assert "Run the recovery workflow now" in manual and "boom" in manual
    lost = advice({"state": "cancelled", "receipt": {"state": "promoting"}, "recovery_run": {"conclusion": "success", "receipt": {"state": "recovered"}}})
    assert "mid-release at 'promoting'" in lost and "recovered" in lost
    assert "serves all traffic" in advice({"state": "succeeded", "receipt": {"revision": "demo-r1", "url": "https://demo.run.app"}})
    assert "recovery" in advice({"state": "cancelled"}).lower()
    phases = release_phases(RECEIPT)
    assert phases["state"] == "traffic_restored" and phases["candidate_logs"][0]["message"].startswith("ModuleNotFoundError") and "timestamps" not in phases
    assert release_phases(None) is None


def test_plans_carry_commit_facts_and_history_lists_them(tmp_path):
    c, p, gh = make(tmp_path)
    plan = c.plan(p, "demo")
    assert plan["commit"]["message"] == "Fix the login bug" and plan["commit"]["author"] == "Alice" and "deploy_staging" in plan["next_action"]
    c.deploy(p, plan["id"])
    gh.receipt = {"release": {"plan_id": plan["id"], "state": "succeeded", "revision": "demo-rabc", "url": "https://demo.run.app", "previous_traffic": {"demo-r0": 100}},
                  "vulnerabilities": {"analysis_status": "FINISHED_SUCCESS", "by_severity": {"CRITICAL": 0}}}
    status = c.status(p, plan["id"])
    assert status["state"] == "succeeded" and status["receipt"]["revision"] == "demo-rabc" and status["vulnerabilities"]["by_severity"]["CRITICAL"] == 0
    assert status["outcome"]["run_url"].endswith("/123") and status["phases"]["revision"] == "demo-rabc" and "demo-rabc serves all traffic" in status["next_action"]
    assert [e["event"] for e in status["events"]] == ["planned", "dispatching", "dispatched", "succeeded"]
    listed = c.plans(p)["plans"][0]
    assert listed["state"] == "succeeded" and listed["summary"] == "Fix the login bug" and listed["revision"] == "demo-rabc" and listed["completed_at"] == "2026-10-12T10:08:00Z"
    # Stored once: a second status call answers from the record without asking GitHub again, and the slim workflow keeps its link.
    gh.status = lambda app, plan: (_ for _ in ()).throw(AssertionError("GitHub should not be consulted again"))
    again = c.status(p, plan["id"])
    assert again["state"] == "succeeded" and again["workflow"]["html_url"].endswith("/123") and "jobs" not in again["workflow"]
    apps = c.applications(p)["demo"]
    assert apps["live_release"]["revision"] == "demo-rabc" and apps["live_release"]["commit"]["message"] == "Fix the login bug"
    assert apps["undeployed_commits"]["ahead_by"] == 2 and apps["undeployed_commits"]["commits"][-1]["message"] == "Third change"


def test_failed_release_keeps_receipt_and_follows_the_recovery_run(tmp_path):
    gh = FakeGitHub()
    gh.status = lambda app, plan: failing_workflow()
    gh.receipt = {"release": RECEIPT, "vulnerabilities": None}
    c, p, _ = make(tmp_path, gh)
    plan = c.plan(p, "demo")
    c.deploy(p, plan["id"])
    status = c.status(p, plan["id"])
    assert status["state"] == "failed" and status["failed_step"]["step"] == STEP and status["checks"]["health"] == "release_failed_health_unconfirmed"
    assert status["phases"]["candidate_logs"][0]["message"].startswith("ModuleNotFoundError") and "previous revision kept serving" in status["next_action"]
    assert status["recovery_run"] is None  # nothing has run yet
    gh.recovery = {"run_id": 124, "status": "completed", "conclusion": "success", "html_url": "x", "created_at": "2026-10-12T10:09:00Z",
                   "receipt": {"state": "no_recovery_needed", "last_release": "traffic_restored"}}
    later = c.status(p, plan["id"])  # the stored failure now looks up the recovery run and keeps it
    assert later["recovery_run"]["conclusion"] == "success" and "no_recovery_needed" in later["next_action"]
    gh.recovery = None
    assert c.status(p, plan["id"])["recovery_run"]["conclusion"] == "success"  # persisted, not re-fetched
    assert [e["event"] for e in c.status(p, plan["id"])["events"]].count("failed") == 1  # re-saving the record is not a new event
    assert c.applications(p)["demo"]["live_release"] is None and "No release recorded" in c.applications(p)["demo"]["note"]


def test_waiting_status_returns_as_soon_as_the_state_changes(tmp_path, monkeypatch):
    gh = FakeGitHub()
    answers = iter([failing_workflow(status="in_progress", conclusion=None), failing_workflow(status="in_progress", conclusion=None), failing_workflow()])
    gh.status = lambda app, plan: next(answers)
    gh.receipt = None
    c, p, _ = make(tmp_path, gh)
    plan = c.plan(p, "demo")
    c.deploy(p, plan["id"])
    naps = []
    monkeypatch.setattr("labdeploy.controller.time.sleep", lambda s: naps.append(s))
    status = c.status(p, plan["id"], wait_seconds=45)
    assert status["state"] == "failed" and len(naps) == 2 and all(n <= 5 for n in naps)
    assert c.status(p, plan["id"], wait_seconds=0)["state"] == "failed"


def test_status_tool_accepts_wait_seconds_and_lists_live_release(tmp_path):
    config = Config.model_validate({"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}}, "database": str(tmp_path / "db"),
                                    "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True}]})
    headers = {"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"}
    with TestClient(build_app(config, FakeGitHub())) as client:
        def call(name, **arguments):
            return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}).json()["result"]
        apps = call("list_applications")["structuredContent"]
        assert apps["demo"]["live_release"] is None and apps["demo"]["repository"] == "lab/demo"
        plan = call("create_deployment_plan", application="demo")["structuredContent"]
        status = call("get_deployment_status", plan_id=plan["id"], wait_seconds=1)["structuredContent"]
        assert status["state"] == "planned" and "deploy_staging" in status["next_action"]
        tools = {t["name"]: t for t in client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]}
        assert "wait_seconds" in tools["get_deployment_status"]["inputSchema"]["properties"]


def zip_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, json.dumps(payload))
    return buffer.getvalue()


def test_github_adapter_reads_commits_compare_receipts_and_recovery(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
    app = Config.model_validate({"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
                                 "principals": []}).applications["demo"]
    deploy_run = {"id": 123, "created_at": "2026-10-12T10:00:00Z"}

    def handler(request):
        path = request.url.path
        if path == "/repos/lab/demo/commits/main":
            return httpx.Response(200, json={"sha": "a" * 40, "html_url": "https://github.com/lab/demo/commit/aaa",
                                             "commit": {"message": "Fix the login bug\n\nDetails", "author": {"name": "Alice", "date": "2026-10-12T10:00:00Z"}}})
        if path == "/repos/lab/demo/compare/" + "b" * 40 + "...main":
            return httpx.Response(200, json={"status": "ahead", "ahead_by": 1, "behind_by": 0, "commits": [{"sha": "a" * 40, "commit": {"message": "Fix the login bug"}}]})
        if path == "/repos/lab/demo/actions/runs/123/artifacts":
            return httpx.Response(200, json={"artifacts": [{"id": 9, "name": "release-receipt", "size_in_bytes": 300, "expired": False}]})
        if path == "/repos/lab/demo/actions/artifacts/9/zip":
            assert "Authorization" in request.headers
            return httpx.Response(302, headers={"location": "https://blob.example/9.zip"})
        if request.url.host == "blob.example":
            assert "Authorization" not in request.headers  # the GitHub credential never reaches the download host
            return httpx.Response(200, content=zip_bytes({"release-receipt.json": {"state": "succeeded", "revision": "demo-r1"}, "vulnerability-summary.json": {"total": 0}}))
        if path == "/repos/lab/demo/actions/workflows/recover-gcp.yml/runs":
            return httpx.Response(200, json={"workflow_runs": [
                {"id": 124, "status": "completed", "conclusion": "success", "html_url": "r124", "created_at": "2026-10-12T10:09:00Z"},
                {"id": 100, "status": "completed", "conclusion": "success", "html_url": "r100", "created_at": "2026-10-11T10:09:00Z"}]})
        if path == "/repos/lab/demo/actions/runs/124/artifacts":
            return httpx.Response(200, json={"artifacts": [{"id": 10, "name": "recovery-receipt", "size_in_bytes": 100, "expired": False}]})
        if path == "/repos/lab/demo/actions/artifacts/10/zip":
            return httpx.Response(200, content=zip_bytes({"recovery-receipt.json": {"state": "no_recovery_needed"}}))
        if path == "/repos/lab/demo/actions/runs/125/artifacts":
            return httpx.Response(200, json={"artifacts": []})
        raise AssertionError(path)
    github = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    commit = github.commit(app)
    assert commit == {"sha": "a" * 40, "message": "Fix the login bug", "author": "Alice", "date": "2026-10-12T10:00:00Z", "url": "https://github.com/lab/demo/commit/aaa"}
    assert github.head(app) == "a" * 40
    assert github.compare(app, "b" * 40)["commits"] == [{"sha": "a" * 40, "message": "Fix the login bug"}]
    receipts = github.release_receipt(app, 123)
    assert receipts["release"]["revision"] == "demo-r1" and receipts["vulnerabilities"]["total"] == 0
    assert github.release_receipt(app, 125) is None
    recovery = github.recovery_after(app, deploy_run)
    assert recovery["run_id"] == 124 and recovery["receipt"]["state"] == "no_recovery_needed"
    assert github.recovery_after(app, {"id": 124, "created_at": "2026-10-13T00:00:00Z"}) is None


def test_demo_mode_reports_a_local_commit(tmp_path):
    from pathlib import Path

    from labdeploy.demo import LocalRepository
    repo = LocalRepository(Path("examples/demo-app"))
    assert repo.commit(None)["sha"] == repo.head(None) and "demo mode" in repo.commit(None)["message"]


def test_summaries_of_old_plans_without_commit_facts_still_list(tmp_path):
    c, p, gh = make(tmp_path)
    plan = c.plan(p, "demo")
    stored = copy.deepcopy(plan)
    stored.pop("commit")
    c.store.finish(p.name, stored, "planned")
    listed = c.plans(p)["plans"][0]
    assert listed["summary"] is None and listed["revision"] is None and listed["completed_at"] is None
    with pytest.raises(ValueError):
        c.status(p, "unknown")
