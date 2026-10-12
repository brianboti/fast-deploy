"""Phase 5 on a laptop: live views through the observer identity, auto-deploy, the watchdog, backups, the scheduler."""
import hashlib
import json
import time
from pathlib import Path

import httpx
import pytest
import yaml
from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient

from labdeploy.cloud import Observer, log_line
from labdeploy.config import Config
from labdeploy.controller import Controller, Store
from labdeploy.scheduler import Scheduler
from labdeploy.server import build_app

OBSERVER = "lab-deploy-observer@test-project.iam.gserviceaccount.com"


def config(tmp_path, **extra):
    base = {"live_dispatch": True, "database": str(tmp_path / "db"),
            "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
            "principals": [{"name": "m", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True}]}
    base.update(extra)
    return Config.model_validate(base)


def test_observer_token_comes_from_impersonation_and_is_cached(tmp_path):
    calls = []

    def runner(args):
        calls.append(args)
        return "ya29.token"
    down = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    observer = Observer(config(tmp_path, observer={"service_account": OBSERVER}), client=down, runner=runner)
    assert observer.token() == "ya29.token" and observer.token() == "ya29.token" and len(calls) == 1
    assert calls[0][-2:] == ["--impersonate-service-account", OBSERVER]

    def broken(args):
        raise OSError("gcloud not found")
    with pytest.raises(ValueError, match="observer identity"):
        Observer(config(tmp_path, observer={"service_account": OBSERVER}), client=down, runner=broken).token()

    import subprocess

    def denied(args):
        raise subprocess.CalledProcessError(1, args, stderr="WARNING: impersonation in use\nERROR: (gcloud.auth.print-access-token) PERMISSION_DENIED: not found\n")
    with pytest.raises(ValueError, match="PERMISSION_DENIED: not found"):  # the reason, not gcloud's warning line
        Observer(config(tmp_path, observer={"service_account": OBSERVER}), client=down, runner=denied).token()
    metadata = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"access_token": "machine", "expires_in": 3600})))
    assert Observer(config(tmp_path, observer={"service_account": OBSERVER}), client=metadata, runner=broken).token() == "machine"  # on a machine: no gcloud needed


def test_observer_reads_service_metrics_and_logs(tmp_path):
    def handler(request):
        path = request.url.path
        if path.endswith("/services/demo"):
            return httpx.Response(200, json={"uri": "https://demo.run.app", "ingress": "INGRESS_TRAFFIC_ALL", "updateTime": "2026-10-12T10:00:00Z",
                                             "latestReadyRevision": "projects/p/locations/l/services/demo/revisions/demo-r2",
                                             "terminalCondition": {"type": "Ready", "state": "CONDITION_SUCCEEDED"},
                                             "conditions": [{"type": "RoutesReady", "state": "CONDITION_SUCCEEDED"}, {"type": "ConfigurationsReady", "state": "CONDITION_SUCCEEDED"}],
                                             "trafficStatuses": [{"revision": "demo-r2", "percent": 100, "uri": "https://demo.run.app"}]})
        if path.endswith("/revisions/demo-r2"):
            return httpx.Response(200, json={"labels": {"lab-deploy-plan": "p" * 32, "lab-deploy-commit": "a" * 40}})
        if "monitoring.googleapis.com" in str(request.url):
            metric = request.url.params["filter"]
            value = 7 if "request_count" in metric and "5xx" not in metric else (2 if "5xx" in metric else 0.4)
            return httpx.Response(200, json={"timeSeries": [{"points": [{"value": {"doubleValue": value}}]}]})
        if path == "/v2/entries:list":
            body = json.loads(request.content)
            assert 'severity>=ERROR' in body["filter"] and body["orderBy"] == "timestamp desc"
            return httpx.Response(200, json={"entries": [{"timestamp": "t", "severity": "ERROR", "textPayload": "boom", "resource": {"labels": {"revision_name": "demo-r2"}}}]})
        raise AssertionError(str(request.url))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    observer = Observer(config(tmp_path, observer={"service_account": OBSERVER}), client=client, runner=lambda args: "t")
    observer.until, observer.cached = time.time() + 999, "t"
    service = observer.service(config(tmp_path).applications["demo"].target)
    assert service["ready"] and service["traffic"][0]["commit"] == "a" * 40 and service["latest_ready_revision"] == "demo-r2"
    metrics = observer.metrics(config(tmp_path).applications["demo"].target, 60)
    assert metrics["requests"] == 7 and metrics["server_errors"] == 2 and metrics["request_latency_ms_p95"] == 0.4
    logs = observer.logs(config(tmp_path).applications["demo"].target, 30, "ERROR", 10)
    assert logs == [{"at": "t", "severity": "ERROR", "revision": "demo-r2", "message": "boom"}]
    with pytest.raises(ValueError, match="severity"):
        observer.logs(config(tmp_path).applications["demo"].target, 30, "LOUD", 10)
    assert log_line({"httpRequest": {"status": 503, "requestMethod": "GET", "requestUrl": "/x"}})["message"] == "503 GET /x"


class FakeObserver:
    def __init__(self, serving="demo-rabc", ready=True, ingress="INGRESS_TRAFFIC_ALL", errors=0):
        self.serving, self.ready, self.ingress, self.errors = serving, ready, ingress, errors

    def service(self, target):
        return {"url": "u", "ready": self.ready, "ready_message": None if self.ready else "Revision failed", "ingress": self.ingress,
                "latest_ready_revision": self.serving, "updated_at": "t", "traffic": [{"revision": self.serving, "percent": 100, "plan_id": None, "commit": None}]}

    def metrics(self, target, minutes=60):
        return {"window_minutes": minutes, "requests": 10, "server_errors": self.errors, "request_latency_ms_p95": 120.0, "max_active_instances": 1, "memory_utilization_p95": 0.2}

    def logs(self, target, minutes, severity, limit):
        return [{"at": "t", "severity": severity, "revision": self.serving, "message": "line"}]

    def needs(self, target, needs):
        return [{"need": "bucket", "name": f"{target.project_id}-{target.service}-{name}", "exists": name != "missing"} for name in needs.get("buckets", [])]


def test_service_health_compares_cloud_run_with_the_recorded_release(tmp_path):
    cfg = config(tmp_path)
    gh = FakeGitHub()
    c = Controller(cfg, gh, Store(cfg.database), FakeObserver())
    p = cfg.principals[0]
    health = c.service_health(p, "demo")
    assert health["agreement"] is None and health["notes"] == [] and health["metrics"]["requests"] == 10
    plan = c.plan(p, "demo")
    c.deploy(p, plan["id"])
    gh.receipt = {"release": {"plan_id": plan["id"], "state": "succeeded", "revision": "demo-rabc", "image": "i"}, "vulnerabilities": None}
    assert c.status(p, plan["id"])["state"] == "succeeded"
    assert c.service_health(p, "demo")["agreement"] is True
    drifted = Controller(cfg, gh, Store(cfg.database), FakeObserver(serving="demo-rother", ready=False, ingress="INGRESS_TRAFFIC_INTERNAL_ONLY", errors=3))
    health = drifted.service_health(p, "demo")
    assert health["agreement"] is False and len(health["notes"]) == 4 and any("spending cap" in n for n in health["notes"])
    assert c.recent_logs(p, "demo", 15, "WARNING", 5)["lines"][0]["severity"] == "WARNING"
    with pytest.raises(ValueError, match="observer.service_account"):
        Controller(cfg, gh, Store(cfg.database)).service_health(p, "demo")


def test_auto_deploy_releases_new_commits_once_and_respects_pauses(tmp_path):
    cfg = config(tmp_path)
    cfg.applications["demo"].auto_deploy = True
    gh = FakeGitHub()
    c = Controller(cfg, gh, Store(cfg.database))
    first = c.auto_deploy_tick()
    assert first["demo"].startswith("dispatched: plan ") and gh.calls == 1
    assert c.auto_deploy_tick()["demo"].startswith("commit aaaaaaaaaaaa already handled") and gh.calls == 1  # no second attempt for the same commit
    plan_id = first["demo"].split("plan ")[1].split(" ")[0]
    gh.receipt = {"release": {"plan_id": plan_id, "state": "succeeded", "revision": "demo-r1", "image": "i"}, "vulnerabilities": None}
    auto = [e for e in c.store.recent_events(10) if e["kind"] == "auto_deploy"]
    assert auto and auto[0]["identity"] == "auto-deploy" and auto[0]["outcome"] == "dispatched"
    c.status(cfg.principals[0], plan_id)
    assert c.auto_deploy_tick()["demo"] == "up to date"
    gh.sha = "b" * 40
    assert c.auto_deploy_tick()["demo"].startswith("dispatched") and gh.calls == 2
    cfg.applications["demo"].paused = True
    assert c.auto_deploy_tick()["demo"] == "paused"
    cfg.applications["demo"].paused = False
    cfg.paused = True
    assert c.auto_deploy_tick() == {"paused": True}
    cfg.paused = False
    cfg.applications["demo"].auto_deploy = False
    assert c.auto_deploy_tick() == {}


def test_watchdog_reports_problems_and_posts_them(tmp_path):
    posted = []

    class Trouble(FakeGitHub):
        def credential_status(self):
            return {"kind": "fine_grained_token", "expires_at": "x", "days_left": 3, "warning": "The GitHub credential expires in 3 day(s)"}

        def schedule_health(self, app, workflow):
            return {"workflow": workflow, "state": "disabled_inactivity", "last_scheduled_run": None, "last_conclusion": None}
    cfg = config(tmp_path, notify_webhook_url="https://hooks.example/abc")
    c = Controller(cfg, Trouble(), Store(cfg.database))
    import labdeploy.controller as module
    original = module.__dict__.get("httpx")

    class Hook:
        @staticmethod
        def post(url, json=None, timeout=None):
            posted.append((url, json))

            class Done:
                def raise_for_status(self):
                    return None
            return Done()
    import sys

    import httpx as real_httpx
    sys.modules["httpx"].post, real_post = Hook.post, real_httpx.post
    try:
        report = c.watchdog_tick()
    finally:
        sys.modules["httpx"].post = real_post
    assert report["notified"] and any("expires in 3" in p for p in report["problems"]) and any("disabled_inactivity" in p for p in report["problems"])
    assert posted[0][0] == "https://hooks.example/abc" and "watchdog found" in posted[0][1]["text"]
    assert c.store.recent_events(5)[0]["kind"] == "watchdog" and c.store.recent_events(5)[0]["outcome"] == "problems"
    quiet = Controller(config(tmp_path), FakeGitHub(), Store(cfg.database))
    report = quiet.watchdog_tick()
    assert report["problems"] == [] and report["notified"] is False
    assert original is None or original is real_httpx


def test_backup_writes_a_consistent_copy_and_prunes_old_ones(tmp_path):
    cfg = config(tmp_path, backup_keep_days=1)
    c = Controller(cfg, FakeGitHub(), Store(cfg.database))
    c.plan(cfg.principals[0], "demo")
    folder = Path(str(tmp_path / "db") + ".backups")
    folder.mkdir()
    stale = folder / "deployments-20200101-000000.db"
    stale.write_text("old")
    import os
    os.utime(stale, (time.time() - 3 * 86_400, time.time() - 3 * 86_400))
    result = c.backup_tick()
    copy = Path(result["backup"])
    assert copy.exists() and result["removed"] == ["deployments-20200101-000000.db"] and not stale.exists()
    import sqlite3
    assert sqlite3.connect(copy).execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
    assert c.store.recent_events(3)[0]["kind"] == "backup"


def test_scheduler_runs_due_jobs_and_records_outcomes(tmp_path):
    cfg = config(tmp_path)
    c = Controller(cfg, FakeGitHub(), Store(cfg.database))
    scheduler = Scheduler(c, cfg)
    assert set(scheduler.state) == {"auto_deploy", "watchdog", "backup"} and all(e["last_run"] is None for e in scheduler.state.values())
    entry = scheduler.run_job("auto_deploy")
    assert entry["last_result"] == {} and entry["last_error"] is None and entry["next_run"] > time.time() + 200
    scheduler.jobs["backup"] = (lambda: (_ for _ in ()).throw(RuntimeError("disk full")), lambda: 10, 0)
    assert "disk full" in scheduler.run_job("backup")["last_error"]
    assert scheduler.snapshot()["backup"]["last_error"].startswith("RuntimeError")
    scheduler.stop()


def test_status_and_tools_expose_the_laptop_jobs(tmp_path):
    cfg = config(tmp_path, observer={"service_account": OBSERVER}, auto_deploy_interval_seconds=120)
    cfg.applications["demo"].auto_deploy = True
    with TestClient(build_app(cfg, FakeGitHub(), observer_factory=lambda config: FakeObserver())) as client:
        headers = {"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"}

        def call(name, **arguments):
            return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}).json()["result"]
        status = call("get_controller_status")["structuredContent"]
        assert status["observer"] == OBSERVER and status["auto_deploy"] == ["demo"] and status["background"] is None  # tests run without the thread
        health = call("get_service_health", application="demo", minutes=30)["structuredContent"]
        assert health["metrics"]["window_minutes"] == 30 and health["console_url"].startswith("https://console.cloud.google.com/run/detail/")
        assert call("get_recent_logs", application="demo", minutes=10, severity="WARNING", limit=5)["structuredContent"]["count"] == 1
    workflow = yaml.safe_load(Path(".github/workflows/controller-watch.yml").read_text())
    assert workflow["jobs"]["ping"]["if"] == "vars.CONTROLLER_HEALTH_URL != ''" and "*/30" in workflow[True]["schedule"][0]["cron"]


def test_observer_reads_readiness_from_the_terminal_condition(tmp_path):
    # Cloud Run v2 returns the Ready condition as terminalCondition; "conditions" only holds RoutesReady and ConfigurationsReady.
    payloads = iter([
        {"uri": "u", "terminalCondition": {"type": "Ready", "state": "CONDITION_FAILED", "message": "Revision demo-r3 failed"},
         "conditions": [{"type": "RoutesReady", "state": "CONDITION_SUCCEEDED"}], "trafficStatuses": []},
        {"uri": "u", "conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED"}], "trafficStatuses": []},
    ])
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=next(payloads))))
    observer = Observer(config(tmp_path, observer={"service_account": OBSERVER}), client=client, runner=lambda args: "t")
    observer.until, observer.cached = time.time() + 999, "t"
    target = config(tmp_path).applications["demo"].target
    failed = observer.service(target)
    assert failed["ready"] is False and failed["ready_message"] == "Revision demo-r3 failed"
    assert observer.service(target)["ready"] is True
