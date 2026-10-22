"""Deployment visibility: provider truth, isolated reads, access boundaries, and stale evidence."""

import copy
import datetime
import hashlib
import json
import time
import types

import pytest
from conftest import TARGET, FakeGitHub
from fastapi import FastAPI
from fastapi.testclient import TestClient

from labdeploy import deployment_page, settings
from labdeploy.config import Config, LiveConfig, Principal
from labdeploy.controller import Controller, Store
from labdeploy.deployments import Deployments, clean, url


def iso(at):
    return datetime.datetime.fromtimestamp(at, datetime.UTC).isoformat()


class Provider(FakeGitHub):
    def __init__(self):
        self.reads = 0
        self.fail_read = False
        now = time.time()
        self.result = {
            "id": 123,
            "status": "in_progress",
            "created_at": iso(now - 120),
            "updated_at": iso(now),
            "html_url": "https://github.com/lab/demo/actions/runs/123",
            "jobs": [{"name": "build", "status": "in_progress", "steps": [{"name": "Build image", "status": "in_progress", "started_at": iso(now - 100)}]}],
        }

    def status(self, app, plan):
        self.reads += 1
        if self.fail_read:
            raise RuntimeError("authorization=very-secret")
        return copy.deepcopy(self.result)


@pytest.fixture
def system(tmp_path):
    config = Config.model_validate(
        {
            "database": str(tmp_path / "db"),
            "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
            "principals": [{"name": "reader", "token_sha256": hashlib.sha256(b"reader-token").hexdigest(), "applications": ["demo"]}],
        }
    )
    live = LiveConfig(config, str(tmp_path / "config.yaml"))
    provider = Provider()
    controller = Controller(live, provider, Store(config.database))
    plan = {"id": "run-1", "application": "demo", "repository": "lab/demo", "sha": "a" * 40, "expires_at": time.time() + 780, "target": TARGET, "checks": {}, "state": "dispatched"}
    controller.store.add("operator", plan, "dispatched")
    clock = [time.time()]
    monitor = Deployments(controller, clock=lambda: clock[0])
    return controller, provider, monitor, config.principals[0], clock


def test_shared_fresh_reads_recover_after_provider_failure_without_mutations(system):
    c, github, monitor, reader, clock = system
    first = monitor.detail(reader, "run-1")
    assert first["current"] == "Build image"
    assert first["state"] == "running"
    assert first["groups"][0]["steps"][0]["started_at"]
    monitor.detail(reader, "run-1")
    assert github.reads == 1
    assert c.store.fetch("run-1")[1]["state"] == "dispatched"
    assert github.calls == 0
    observed_at = first["observed_at"]
    github.fail_read = True
    clock[0] += 11
    failed = monitor.detail(reader, "run-1")
    assert failed["error"] and failed["current"] == "Build image"
    assert failed["observed_at"] == observed_at
    assert "very-secret" not in json.dumps(failed)
    github.fail_read = False
    clock[0] += 11
    assert monitor.detail(reader, "run-1")["error"] is None
    assert github.reads == 3


def test_access_checked_before_cached_data_and_unknown_setup(system):
    c, github, monitor, reader, clock = system
    monitor.detail(reader, "run-1")
    nobody = Principal(name="nobody", token_sha256="0" * 64, applications=[])
    for kwargs in ({"plan_id": "run-1"}, {"application": "demo"}):
        with pytest.raises(ValueError):
            monitor.detail(nobody, **kwargs)
    assert not monitor.listing(nobody)["rows"]
    assert github.reads == 1


def test_unverified_workflow_never_claims_app_is_live(system):
    c, github, monitor, reader, clock = system
    github.result.update(status="completed", conclusion="success")
    github.result["jobs"][0].update(status="completed", conclusion="success")
    github.result["jobs"][0]["steps"][0].update(status="completed", conclusion="success")
    data = monitor.detail(reader, "run-1")
    assert data["state"] == "completed_unverified"
    assert not data["terminal"]
    page = deployment_page.detail(data)
    assert "Result needs verification" in page
    assert "Your app is live" not in page


def test_setup_handoff_keeps_exact_first_release_identity(system):
    c, github, monitor, reader, clock = system
    c.store.save_onboarding("demo", {"application": "demo", "repository": "lab/demo", "state": "merging_contract", "steps": {}, "created_at": clock[0] - 30})
    setup = monitor.detail(reader, application="demo")
    assert setup["kind"] == "setup" and not setup["terminal"]
    c.store.save_onboarding("demo", {"application": "demo", "repository": "lab/demo", "state": "complete", "steps": {}, "plan_id": "run-1"})
    data = monitor.detail(reader, application="demo")
    assert data["id"] == "run-1" and data["state"] == "running"
    assert data["setup"]["state"] == "complete"
    assert len(data["setup"]["groups"][0]["steps"]) == 11


def test_terminal_controller_evidence_wins_over_saved_running_snapshot(system):
    c, github, monitor, reader, clock = system
    monitor.detail(reader, "run-1")
    owner, plan = c.store.fetch("run-1")
    plan.update(state="failed", timeline=[{"step": "Build image", "job": "build", "status": "completed", "conclusion": "failure", "seconds": 43}])
    c.store.finish(owner, plan, "failed")
    data = monitor.detail(reader, "run-1")
    assert data["state"] == "failed"
    assert data["groups"][0]["steps"][0]["state"] == "failed"
    assert github.reads == 1


def test_health_read_is_independent_and_stale_health_is_marked(system):
    c, github, monitor, reader, clock = system
    health_calls = []

    def service(target):
        health_calls.append(target)
        return {"ready": True, "url": "https://demo.example.com", "traffic": [{"revision": "previous", "percent": 100}]}

    c.observer = types.SimpleNamespace(service=service)
    data = monitor.detail(reader, "run-1")
    assert data["state"] == "running" and data["health"]["ready"]
    assert data["health"]["traffic"][0]["revision"] == "previous"
    clock[0] += 11
    monitor.detail(reader, "run-1")
    assert len(health_calls) == 1
    c.observer.service = lambda target: (_ for _ in ()).throw(ValueError("private error"))
    clock[0] += 31
    data = monitor.detail(reader, "run-1")
    assert data["health_error"] and data["health"]["ready"]
    assert "out of date" in deployment_page.detail(data)


def test_persistent_snapshot_survives_restart_and_handles_malicious_step(system):
    c, github, monitor, reader, clock = system
    github.result["jobs"][0]["steps"][0]["name"] = "<script>alert(1)</script> token=hidden"
    monitor.detail(reader, "run-1")
    restarted = Deployments(c, clock=lambda: clock[0] + 11)
    github.fail_read = True
    data = restarted.detail(reader, "run-1")
    page = deployment_page.detail(data)
    assert data["error"] and "&lt;script&gt;" in page and "<script>alert" not in page
    assert "token=hidden" not in page
    assert url("javascript:alert(1)") is None
    assert url("https://name:pass@example.com") is None
    assert url("https://example.com/?token=secret") == "https://example.com/"
    assert clean("Authorization=secret Bearer abc123") == "Authorization=[redacted] Bearer [redacted]"


def test_authenticated_routes_and_scoped_report(system):
    c, github, monitor, reader, clock = system
    failures = types.SimpleNamespace(allow=lambda host: (True, 0))
    app = FastAPI(routes=settings.routes(c.config, c.store, failures, c))
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/settings/deployment/status?plan=run-1").status_code == 401
        assert client.get("/settings/deployment/report?plan=run-1").status_code == 401
        response = client.post("/settings/sign-in", data={"token": "reader-token"}, follow_redirects=False)
        assert response.status_code == 303
        page = client.get("/settings?tab=deployments&plan=run-1")
        assert page.status_code == 200
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert "Build image" in page.text and "deployment/script.js" in page.text
        data = client.get("/settings/deployment/status?plan=run-1")
        assert data.json()["state"] == "running"
        report = client.get("/settings/deployment/report?plan=run-1")
        assert report.json()["application"] == "demo"
        assert "attachment" in report.headers["content-disposition"]
        assert report.headers["cache-control"] == "no-store"
        assert client.get("/settings/deployment/status?plan=other-run").status_code == 404
        assert client.post("/settings/deployment/status?plan=run-1").status_code == 405
        assert client.get("/settings/deployment/script.js").status_code == 200
        assert "script-src" not in client.get("/settings?tab=releases").headers["content-security-policy"]
        c.config.current.principals = []
        assert client.get("/settings/deployment/status?plan=run-1").status_code == 401


@pytest.mark.parametrize("status,conclusion,expected", [
    ("waiting", None, "awaiting_approval"), ("queued", None, "queued"),
    ("completed", "cancelled", "cancelled"), ("completed", "timed_out", "timed_out"),
])
def test_waits_and_terminal_states_remain_distinct(system, status, conclusion, expected):
    c, github, monitor, reader, clock = system
    github.result.update(status=status, conclusion=conclusion)
    github.result["jobs"][0]["steps"][0]["status"] = "pending"
    if status == "waiting":
        github.result["pending"] = [{"environment": "production", "reviewers": ["owner"]}]
    data = monitor.detail(reader, "run-1")
    assert data["state"] == expected
    assert data["events"][-1]["text"]
    if status == "waiting":
        assert "Reviewers: owner" in deployment_page.detail(data)
    assert github.calls == 0
