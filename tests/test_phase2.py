"""Phase 2: cancel, one-call deploy, compare with the live release, sessions, pause, live reload, platform pin check."""
import hashlib
import time

import pytest
import yaml
from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient
from test_oauth import make_config, tokens_for

from labdeploy.config import Config, LiveConfig
from labdeploy.controller import Controller, Store
from labdeploy.oauth import OAuthStore
from labdeploy.server import build_app
from labdeploy.workflows import render

MCP = {"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"}


def raw(tmp_path, **extra):
    return {"live_dispatch": True, "database": str(tmp_path / "db"),
            "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
            "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True}], **extra}


def make(tmp_path, github=None, **extra):
    config = Config.model_validate(raw(tmp_path, **extra))
    github = github or FakeGitHub()
    return Controller(config, github, Store(config.database)), config.principals[0], github


class Cancellable(FakeGitHub):
    cancelled = None

    def cancel_run(self, app, run_id):
        self.cancelled = run_id
        return {"cancel_requested": True, "workflow_run_id": run_id}

    def status(self, app, plan):
        base = super().status(app, plan)
        if self.cancelled:
            base.update(status="completed", conclusion="cancelled")
        else:
            base.update(status="in_progress", conclusion=None)
        return base


def test_cancel_requests_cancellation_and_records_it(tmp_path):
    c, p, gh = make(tmp_path, Cancellable())
    plan = c.plan(p, "demo")
    with pytest.raises(ValueError, match="no running workflow"):
        c.cancel(p, plan["id"])
    c.deploy(p, plan["id"])
    result = c.cancel(p, plan["id"])
    assert gh.cancelled == 123 and result["cancel"]["cancel_requested"] and "Cancellation requested" in result["next_action"]
    assert "cancel_requested" in [e["event"] for e in result["events"]]
    later = c.status(p, plan["id"])
    assert later["state"] == "cancelled"
    with pytest.raises(ValueError, match="no running workflow"):
        c.cancel(p, plan["id"])


def test_deploy_latest_plans_and_dispatches_in_one_call(tmp_path):
    c, p, gh = make(tmp_path)
    result = c.deploy_latest(p, "demo")
    assert result["state"] == "dispatched" and gh.calls == 1 and result["commit"]["message"] == "Fix the login bug"


def test_compare_with_live_reports_commits_files_kinds_and_cost(tmp_path):
    class Changing(FakeGitHub):
        def compare(self, app, base_sha, head=None):
            return {"status": "ahead", "ahead_by": 2, "behind_by": 0, "commits": [{"sha": "b" * 40, "message": "Raise memory"}],
                    "files": [{"path": "platform.yaml", "status": "modified"}, {"path": "app.py", "status": "modified"}, {"path": "requirements.txt", "status": "modified"}]}

        def file(self, app, sha, filename):
            spec = dict(self.spec)
            if sha == self.sha:  # head: bigger than the live release
                spec["resources"] = {"cpu": 1, "memory_mib": 1024}
            return yaml.safe_dump(spec)
    c, p, gh = make(tmp_path, Changing())
    assert "nothing to compare" in c.compare_with_live(p, "demo")["note"]
    gh.sha = "0" * 40  # the live release was built from this commit
    plan = c.plan(p, "demo")
    c.deploy(p, plan["id"])
    gh.receipt = {"release": {"plan_id": plan["id"], "state": "succeeded", "revision": "demo-r0"}, "vulnerabilities": None}
    assert c.status(p, plan["id"])["state"] == "succeeded"
    gh.sha = "a" * 40  # main moved on
    result = c.compare_with_live(p, "demo")
    assert result["live_release"]["sha"] == "0" * 40 and result["head"]["sha"] == "a" * 40 and result["ahead_by"] == 2
    assert result["changes"] == {"dockerfile": False, "dependencies": True, "platform_config": True, "workflows": False, "application_code": True}
    assert result["cost"]["worst_case_after_usd_month"] > result["cost"]["worst_case_before_usd_month"]
    assert result["cost"]["ceiling_policy_after"]["status"] == "not_configured"  # this test app sets no ceiling


def test_sessions_are_listed_and_ended(tmp_path):
    with TestClient(build_app(make_config(tmp_path), FakeGitHub())) as client:
        registration, issued = tokens_for(client)
        store = OAuthStore(Store(str(tmp_path / "db")))
        sessions = store.sessions()
        assert len(sessions) == 1 and sessions[0]["identity"] == "manager" and sessions[0]["client"] == "Claude" and sessions[0]["state"] == "live"
        assert client.get("/applications", headers={"Authorization": f"Bearer {issued['access_token']}"}).status_code == 200
        store.revoke_family(sessions[0]["session"])
        assert store.sessions()[0]["state"] == "revoked"
        assert client.get("/applications", headers={"Authorization": f"Bearer {issued['access_token']}"}).status_code == 401


def test_pause_switches_refuse_writes_but_not_reads(tmp_path):
    c, p, gh = make(tmp_path, paused=True)
    with pytest.raises(ValueError, match="paused by the operator"):
        c.plan(p, "demo")
    assert c.cost(p, "demo")["compute_ceiling_usd_month"] > 0 and c.applications(p)["demo"]["paused"] is True
    c2, p2, _ = make(tmp_path)
    plan = c2.plan(p2, "demo")
    c2.config.applications["demo"].paused = True
    with pytest.raises(ValueError, match="paused by the operator"):
        c2.deploy(p2, plan["id"])
    with pytest.raises(ValueError, match="paused"):
        c2.propose_config(p2, "demo")


def test_configuration_reloads_when_the_file_changes(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw(tmp_path)))
    with TestClient(build_app(Config.model_validate(raw(tmp_path)), FakeGitHub(), config_path=str(path))) as client:
        def status():
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_controller_status", "arguments": {}}}
            return client.post("/mcp", headers=MCP, json=body).json()["result"]["structuredContent"]
        assert status()["paused"] is False and status()["configuration"]["reloaded_at"] is None
        time.sleep(0.01)
        path.write_text(yaml.safe_dump(raw(tmp_path, paused=True)))
        assert status()["paused"] is True and status()["configuration"]["reloaded_at"] is not None
        time.sleep(0.01)
        path.write_text("applications: {}\nprincipals: []\nnot_a_field: 1\n")
        assert status()["paused"] is True and "not_a_field" in status()["configuration"]["error"]  # invalid file ignored, reported
        time.sleep(0.01)
        new_token = hashlib.sha256(b"other-token").hexdigest()
        path.write_text(yaml.safe_dump({**raw(tmp_path), "principals": [{"name": "other", "token_sha256": new_token, "applications": ["demo"]}]}))
        assert client.get("/applications", headers=MCP).status_code == 401  # the old token is gone after the reload
        assert client.get("/applications", headers={"Authorization": "Bearer other-token"}).status_code == 200
    live = LiveConfig(Config.model_validate(raw(tmp_path)))
    assert live.refresh() is False and live.applications["demo"].service == "demo"


def test_platform_pin_is_reported_and_enforced_at_dispatch(tmp_path):
    OLD, MIN, NEW = "1" * 40, "2" * 40, "3" * 40

    class Pinned(FakeGitHub):
        pin = OLD

        def contents(self, repository, ref, path):
            if path == ".github/workflows/deploy-gcp.yml" and self.pin:
                return {"sha": "w", "text": render(open("templates/deploy-gcp.yml").read(), "lab/lab-deploy", self.pin)}
            return None

        def compare_repository(self, repository, base, head):
            assert repository == "lab/lab-deploy" and base == MIN
            return {"status": "behind" if head == OLD else "ahead", "ahead_by": 0, "behind_by": 1, "commits": [], "files": []}
    c, p, gh = make(tmp_path, Pinned(), platform={"repository": "lab/lab-deploy", "minimum_commit": MIN})
    apps = c.applications(p)["demo"]
    assert apps["platform_pin"]["commit"] == OLD and apps["platform_pin"]["supported"] is False and "update_app_workflows" in apps["platform_pin"]["note"]
    plan = c.plan(p, "demo")
    with pytest.raises(ValueError, match="older than the minimum"):
        c.deploy(p, plan["id"])
    gh.pin = NEW
    c._cache.clear()  # list_applications remembers pins for five minutes; the test moves the pin faster than that
    assert c.applications(p)["demo"]["platform_pin"]["supported"] is True
    assert c.deploy(p, plan["id"])["state"] == "dispatched"
    gh.pin = None
    assert c.applications(p)["demo"]["platform_pin"]["supported"] is True  # still the cached answer
    c._cache.clear()
    assert c.applications(p)["demo"]["platform_pin"]["supported"] is False
    unenforced, p2, _ = make(tmp_path, Pinned(), platform={"repository": "lab/lab-deploy"})
    assert unenforced.applications(p2)["demo"]["platform_pin"]["supported"] is None
    plain, p3, _ = make(tmp_path)
    assert plain.applications(p3)["demo"]["platform_pin"] is None
