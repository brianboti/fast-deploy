"""Deployment visibility: provider truth, isolated reads, access boundaries, and stale evidence."""

import copy
import datetime
import hashlib
import json
import threading
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
from labdeploy.onboarding import STEPS


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


# ---- 1.31.8: the review of #175

def plan_of(plan_id, state="dispatched", expires_in=780):
    return {"id": plan_id, "application": "demo", "repository": "lab/demo", "sha": "b" * 40, "expires_at": time.time() + expires_in,
            "target": TARGET, "checks": {}, "state": state}


def test_one_plan_s_slow_github_read_never_holds_up_another_and_a_second_viewer_waits_for_it(system):
    c, github, monitor, reader, clock = system
    c.store.add("operator", plan_of("run-2"), "dispatched")
    entered, release, reads = threading.Event(), threading.Event(), []
    original = github.status

    def status(app, plan):
        reads.append(plan["id"])
        if plan["id"] == "run-1":
            entered.set()
            assert release.wait(5)
        return original(app, plan)

    github.status = status
    viewers = [threading.Thread(target=monitor.detail, args=(reader, "run-1")) for _ in range(2)]
    viewers[0].start()
    assert entered.wait(5)
    viewers[1].start()
    try:
        other = []
        reader_two = threading.Thread(target=lambda: other.append(monitor.detail(reader, "run-2")))
        reader_two.start()
        reader_two.join(5)
        assert other and other[0]["state"] == "running"  # done while run-1's read was still waiting on GitHub
    finally:
        release.set()
        for viewer in viewers:
            viewer.join(5)
    assert reads.count("run-1") == 1  # the second viewer of run-1 had the first one's answer, not a read of its own


def test_a_finished_plan_is_not_read_on_github_and_an_unchanged_observation_is_not_written_again(system, monkeypatch):
    c, github, monitor, reader, clock = system
    writes = []
    save = monitor.save
    monkeypatch.setattr(monitor, "save", lambda key, value: writes.append(key) or save(key, value))
    monitor.detail(reader, "run-1")
    assert github.reads == 1 and len(writes) == 1
    for state in ("failed", "completed_unverified"):
        owner, plan = c.store.fetch("run-1")
        plan.update(state=state, timeline=[{"step": "Build image", "job": "build", "status": "completed", "conclusion": "failure", "seconds": 43}])
        c.store.finish(owner, plan, state)
        for _ in range(3):
            clock[0] += 11
            assert monitor.detail(reader, "run-1")["state"] == state
    assert github.reads == 1  # nothing read on GitHub once the controller recorded the end, unverified or not
    assert len(writes) == 2  # once while running, once to drop the running snapshot; never again for the same evidence


def test_a_finished_deployment_stops_polling_but_can_still_be_refreshed(system):
    c, github, monitor, reader, clock = system
    c.store.add("operator", plan_of("run-2"), "dispatched")
    owner, plan = c.store.fetch("run-1")
    plan.update(state="failed", timeline=[{"step": "Build image", "job": "build", "status": "completed", "conclusion": "failure", "seconds": 43}])
    c.store.finish(owner, plan, "failed")
    failures = types.SimpleNamespace(allow=lambda host: (True, 0))
    app = FastAPI(routes=settings.routes(c.config, c.store, failures, c))
    with TestClient(app, base_url="https://testserver") as client:
        client.post("/settings/sign-in", data={"token": "reader-token"}, follow_redirects=False)
        assert client.get("/settings/deployment/status?plan=run-1").json()["terminal"] is True
        assert client.get("/settings/deployment/status?plan=run-2").json()["terminal"] is False
        assert client.get("/settings/deployment/status").json()["terminal"] is False  # the list keeps updating
        finished = client.get("/settings?tab=deployments&plan=run-1").text
        assert 'data-terminal="true"' in finished and "automatic updates off" in finished and 'id="deployment-refresh"' in finished
        assert 'data-terminal="false"' in client.get("/settings?tab=deployments&plan=run-2").text
    script = deployment_page.SCRIPT
    assert "finished = root.dataset.terminal === 'true'" in script and "finished = data.terminal === true" in script
    assert "if (!stopped && !finished && !document.hidden) timer = setTimeout(update" in script  # no timer once finished
    assert "if (busy || stopped || document.hidden) return;" in script  # but Refresh now still asks
    assert "addEventListener('click', update)" in script


def test_the_list_reads_every_observation_at_once_and_counts_only_what_is_still_going(system, monkeypatch):
    c, github, monitor, reader, clock = system
    everyone = Principal(name="operator", token_sha256="0" * 64, applications=["*"])
    c.store.add("operator", plan_of("planned-new", "planned", 600), "planned")
    c.store.add("operator", plan_of("planned-expired", "planned", -10), "planned")
    c.store.add("operator", plan_of("unverified", "completed_unverified"), "completed_unverified")
    c.store.add("operator", plan_of("failed", "failed"), "failed")
    for name, state in (("going", "infrastructure"), ("stuck", "blocked"), ("done", "complete")):
        c.store.save_onboarding(name, {"application": name, "repository": f"lab/{name}", "state": state, "blocked_at": "planning", "steps": {}})
    monitor.detail(everyone, "run-1")

    def one_by_one(key):
        raise AssertionError("one query for the whole list")

    monkeypatch.setattr(monitor, "saved", one_by_one)
    data = monitor.listing(everyone)
    assert {row["id"]: row["state"] for row in data["rows"]}["run-1"] == "running"  # the saved observation was read
    # going: run-1, planned-new and the setup under way; attention: unverified, failed and the blocked setup
    assert data["counts"] == {"active": 3, "attention": 3}
    page = deployment_page.listing(data)
    assert "<span>In progress / prepared</span><strong>3</strong>" in page and "<span>Needs attention</span><strong>3</strong>" in page


def test_a_setup_that_stopped_or_finished_reads_as_such_and_stops_polling(system):
    c, github, monitor, reader, clock = system
    blocked = monitor.setup({"application": "demo", "state": "blocked", "blocked_at": STEPS[3], "steps": {}})
    assert blocked["groups"][0]["state"] == "failed" and blocked["terminal"]
    done = monitor.setup({"application": "demo", "state": "complete", "steps": {}})
    assert done["groups"][0]["state"] == "done" and done["terminal"]
    assert {step["state"] for step in done["groups"][0]["steps"]} == {"done"}
    going = monitor.setup({"application": "demo", "state": STEPS[2], "steps": {}})
    assert going["groups"][0]["state"] == "running" and not going["terminal"]
