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
    clock[0] += 5
    monitor.detail(reader, "run-1")
    assert len(health_calls) == 1  # its own 10-second budget while the run goes on
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


# ---- 1.31.9: a release as stages a room can follow

from labdeploy import journey  # noqa: E402

BUILD = ["Set up job", "Validate dispatch inputs", "Run actions/checkout@3d3c42e5", "Install trusted platform dependencies", "Validate reviewed specification",
         "Read the application's test policy", "Install the scanners", "Check the Dockerfile against the platform's policy", "Scan the checkout for secrets",
         "Run actions/setup-python@5fda3b95", "Run unit tests", "Audit Python dependencies", "Audit Node.js dependencies", "Lint the Dockerfile",
         "Build without cloud credentials", "Scan the image for secrets", "Make the image's parts list (SBOM)", "Post Run actions/checkout@3d3c42e5", "Complete job"]
DEPLOY = ["Set up job", "Run actions/download-artifact@d3f86a10", "Validate dispatch inputs for reuse", "Validate artifact and deployment configuration",
          "Choose the provenance path", "Run google-github-actions/auth@7c6bc770", "Configure registry access", "Recover any interrupted previous release",
          "Push image and resolve immutable digest", "Attest build provenance", "Verify build provenance", "Install Cosign", "Summarize image vulnerabilities",
          "Deploy, probe, promote; restore traffic on failure", "Record the service URL", "Complete job"]
NOTIFY = ["Set up job", "Post the result", "Complete job"]


def groups_until(running, t0=1000.0, failed=None):
    """The real release's jobs with every step before `running` done (10 s each), `running` in progress, the rest not started."""
    groups, at, state = [], t0, "done"
    for name, steps in (("build", BUILD), ("deploy", DEPLOY), ("notify", NOTIFY)):
        items = []
        for step in steps:
            if step == running:
                state = "failed" if failed else "running"
                items.append({"name": step, "state": state, "started_at": at, "finished_at": at + 5 if failed else None})
                state = "skipped" if failed else "pending"
                continue
            skipped = step in ("Audit Node.js dependencies", "Validate dispatch inputs for reuse", "Install Cosign")
            items.append({"name": step, "state": "skipped" if skipped and state == "done" else state,
                          "started_at": at if state == "done" else None, "finished_at": at + 10 if state == "done" else None})
            at += 10 if state == "done" else 0
        groups.append({"name": name, "state": "running", "steps": items})
    return groups


def test_every_github_step_lands_in_one_stage_and_the_current_one_is_the_tests():
    groups = groups_until("Run unit tests")
    stages = journey.stages(groups, state="running")
    assert [s["key"] for s in stages] == ["request", "code", "tests", "libraries", "build", "sign", "scan", "start", "switch", "watch", "record"]
    assert sum(len(s["steps"]) for s in stages) == len(BUILD) + len(DEPLOY) + len(NOTIFY)  # nothing lost, nothing counted twice
    by = {s["key"]: s for s in stages}
    assert by["request"]["state"] == by["code"]["state"] == "done"
    assert by["tests"]["state"] == "running" and "Run actions/setup-python@5fda3b95" in [s["name"] for s in by["tests"]["steps"]]  # it prepares the tests
    assert by["libraries"]["state"] == "pending" and by["build"]["state"] == "pending"
    assert "Lint the Dockerfile" in [s["name"] for s in by["build"]["steps"]]  # with the build, so the code checks finish together


def test_the_release_step_is_split_by_what_cloud_run_shows():
    groups = groups_until("Deploy, probe, promote; restore traffic on failure")
    by = lambda cloud, bake=300: {s["key"]: s for s in journey.stages(groups, cloud, None, bake, "running")}  # noqa: E731
    ready = by({})
    assert ready["start"]["state"] == "running" and ready["start"]["note"] == "Getting the new version ready."
    assert ready["switch"]["state"] == ready["watch"]["state"] == "pending"
    beside = by({"present": True, "share": 0, "candidate_at": 1500})
    assert "no visitors yet" in beside["start"]["note"] and beside["switch"]["state"] == "pending"
    moving = by({"present": True, "share": 10, "traffic_at": 1600})
    assert moving["start"]["state"] == "done" and moving["switch"]["state"] == "running" and moving["switch"]["share"] == 10
    everyone = by({"present": True, "share": 100, "traffic_at": 1600, "switched_at": 1600})
    assert everyone["start"]["state"] == everyone["switch"]["state"] == "done"
    assert everyone["watch"]["state"] == "running" and everyone["watch"]["since"] == 1600 and everyone["watch"]["window"] == 300
    assert by({"present": True, "share": 100, "switched_at": 1600}, bake=0)["watch"]["state"] == "skipped"


def test_a_failed_release_names_the_stage_it_stopped_in_and_what_never_ran():
    groups = groups_until("Deploy, probe, promote; restore traffic on failure", failed=True)
    stages = {s["key"]: s for s in journey.stages(groups, {"share": 0, "traffic_at": 1600, "switched_at": 1600}, {"failed_phase": "baking"}, 300, "failed")}
    assert stages["start"]["state"] == stages["switch"]["state"] == "done" and stages["watch"]["state"] == "failed"
    assert stages["record"]["state"] == "not_run"
    tests = {s["key"]: s for s in journey.stages(groups_until("Run unit tests", failed=True), state="failed")}
    assert tests["tests"]["state"] == "failed" and tests["libraries"]["state"] == "not_run" and tests["start"]["state"] == "not_run"


def test_after_the_run_the_receipt_s_own_phase_times_win():
    groups = groups_until("Record the service URL")
    for job in groups:
        for step in job["steps"]:
            if step["name"].startswith("Deploy, probe"):
                step.update(state="done", started_at=2000, finished_at=2350)
    phases = {"timestamps": [{"state": "preparing", "at": iso(2005)}, {"state": "deploying", "at": iso(2010)}, {"state": "promoting", "at": iso(2033)},
                             {"state": "baking", "at": iso(2047)}, {"state": "succeeded", "at": iso(2347)}]}
    stages = {s["key"]: s for s in journey.stages(groups, {"share": 100, "switched_at": 2060}, phases, 300, "running")}
    assert (stages["start"]["started_at"], stages["start"]["finished_at"]) == (2000, 2033)
    assert (stages["switch"]["started_at"], stages["switch"]["finished_at"]) == (2033, 2047)
    assert (stages["watch"]["started_at"], stages["watch"]["finished_at"], stages["watch"]["state"]) == (2047, 2347, "done")


def test_cloud_evidence_keeps_first_sightings_from_the_service_s_own_change_time():
    health = {"updated_at": 1500, "traffic": [{"revision": "old", "percent": 100, "plan_id": "other"}, {"revision": "new", "percent": 0, "plan_id": "run-1"}]}
    seen = journey.cloud_evidence({}, "run-1", health, now=1510)
    assert seen["present"] and seen["share"] == 0 and seen["candidate_at"] == 1500 and "switched_at" not in seen
    seen = journey.cloud_evidence(seen, "run-1", {"updated_at": 1530, "traffic": [{"revision": "new", "percent": 100, "plan_id": "run-1"}]}, now=1540)
    assert seen["switched_at"] == 1530 and seen["candidate_at"] == 1500
    seen = journey.cloud_evidence(seen, "run-1", {"updated_at": 1600, "traffic": [{"revision": "new", "percent": 100, "plan_id": "run-1"}]}, now=1610)
    assert seen["switched_at"] == 1530  # a later change of the service never moves the moment of the switch
    seen = journey.cloud_evidence(seen, "run-1", {"updated_at": 1700, "traffic": [{"revision": "old", "percent": 100, "plan_id": "other"}]}, now=1705)
    assert seen["restored_at"] == 1705 and not seen["present"]
    assert journey.cloud_evidence({}, "run-1", {"updated_at": 9999, "traffic": [{"revision": "new", "percent": 0, "plan_id": "run-1"}]}, now=100)["candidate_at"] == 100


def test_a_workflow_without_a_stage_s_steps_leaves_it_out_and_other_runs_keep_the_step_list():
    older = [{"name": "build", "steps": [{"name": "Run unit tests", "state": "done"}, {"name": "Build without cloud credentials", "state": "running"}]}]
    keys = [s["key"] for s in journey.stages(older, state="running")]
    assert "code" not in keys and "libraries" not in keys and "tests" in keys and "start" in keys  # the deploy job is not listed yet
    rollback = [{"name": "restore", "steps": [{"name": "Restore the previous traffic", "state": "running"}]}]
    assert journey.stages(rollback, state="running") is None
    assert journey.typical([500, 510]) is None and journey.typical([500, 520, 540, 0])["seconds"] == 520


def test_the_page_shows_the_journey_the_visitors_and_follows_a_finished_run_until_it_is_recorded(system):
    c, github, monitor, reader, clock = system
    now = clock[0]
    github.result["jobs"] = [
        {"name": "build", "status": "completed", "conclusion": "success",
         "steps": [{"name": n, "status": "completed", "conclusion": "success", "started_at": iso(now - 300), "completed_at": iso(now - 290)}
                   for n in ("Validate dispatch inputs", "Run unit tests", "Audit Python dependencies", "Build without cloud credentials")]},
        {"name": "deploy", "status": "in_progress",
         "steps": [{"name": "Validate artifact and deployment configuration", "status": "completed", "conclusion": "success",
                    "started_at": iso(now - 200), "completed_at": iso(now - 199)},
                   {"name": "Deploy, probe, promote; restore traffic on failure", "status": "in_progress", "started_at": iso(now - 120)}]}]
    c.observer = types.SimpleNamespace(service=lambda target: {"ready": True, "url": "https://demo.example.com", "updated_at": iso(now - 60),
                                                               "traffic": [{"revision": "demo-rrun-1", "percent": 100, "plan_id": "run-1"}]})
    data = monitor.detail(reader, "run-1")
    by = {s["key"]: s for s in data["stages"]}
    assert by["watch"]["state"] == "running" and abs(by["watch"]["since"] - (now - 60)) < 1
    page = deployment_page.detail(data)
    assert "Release journey" in page and "Who gets the visitors" in page and "This release · 100%" in page
    assert 'data-format="bar"' in page and 'aria-current="step"' in page and "Now: Watch it with real traffic." in page
    assert "Every GitHub step" in page and "if the new one fails in the next 5 minutes" in page
    assert "Not enough history yet" in page
    # GitHub says it ended well, every check passed, but the controller has not recorded it: the page keeps following.
    github.result.update(status="completed", conclusion="success")
    for job in github.result["jobs"]:
        job.update(status="completed", conclusion="success")
        for step in job["steps"]:
            step.update(status="completed", conclusion="success", completed_at=step.get("completed_at") or iso(now))
    clock[0] += 11
    data = monitor.detail(reader, "run-1")
    assert data["state"] == "succeeded" and data["terminal"] is False
    assert "GitHub has finished." in deployment_page.detail(data)
    owner, plan = c.store.fetch("run-1")
    plan.update(state="succeeded", workflow=github.result, receipt={"state": "succeeded", "revision": "demo-rrun-1"})
    c.store.finish(owner, plan, "succeeded")
    data = monitor.detail(reader, "run-1")
    assert data["terminal"] is True and "Live." in deployment_page.detail(data)
    assert {s["state"] for s in data["stages"]} <= {"done", "skipped"}


def test_usually_takes_comes_from_the_app_s_own_recent_successful_releases(system):
    c, github, monitor, reader, clock = system
    for index, minutes in enumerate((8, 9, 10)):
        began = clock[0] - 86400 * (index + 1)
        c.store.add("operator", {**plan_of(f"past-{index}", "succeeded"), "workflow": {"run_started_at": iso(began)},
                                 "outcome": {"completed_at": iso(began + minutes * 60)}}, "succeeded")
    github.result["jobs"][0]["steps"][0]["name"] = "Run unit tests"
    data = monitor.detail(reader, "run-1")
    assert data["typical"] == {"seconds": 540, "count": 3}
    assert "About 9 min" in deployment_page.detail(data) and "Median of its last 3 successful releases." in deployment_page.detail(data)


def test_full_screen_and_the_live_clocks_are_in_the_same_origin_script(system):
    script = deployment_page.SCRIPT
    assert "getElementById('deployment-present')" in script and "requestFullscreen" in script and "classList.toggle('d-present', on)" in script
    assert "setInterval(tick, 1000)" in script and "dataset.since" in script and "offset = stamp ? Number(stamp.dataset.now)" in script
    assert "scrollIntoView" in script and "prefers-reduced-motion" in script
    assert "html.d-present .side{display:none}" in deployment_page.STYLE


# ---- 1.31.10: the live release page, drawn for a room

def test_the_live_page_has_the_track_the_spotlight_and_every_stage_s_three_plain_lines(system):
    c, github, monitor, reader, clock = system
    now = clock[0]
    github.result["jobs"] = [
        {"name": "build", "status": "in_progress",
         "steps": [{"name": "Validate dispatch inputs", "status": "completed", "conclusion": "success", "started_at": iso(now - 300), "completed_at": iso(now - 290)},
                   {"name": "Run unit tests", "status": "in_progress", "started_at": iso(now - 100)}]}]
    page = deployment_page.detail(monitor.detail(reader, "run-1"))
    # the track: one node a stage, grouped, each a link to its row, the current one marked
    assert 'class="d-track"' in page and "Check the change" in page and "Go live" in page
    assert 'href="#stage-tests"' in page and 'class="t-running in-flow ' in page and 'data-stage="track-tests" data-stage-state="running"' in page
    # the spotlight says what the stage does, why it is there and what happens if it fails, in plain words
    assert "Happening now" in page and "own automated tests run on a clean machine" in page
    assert "Why it is here" in page and "If it fails" in page and "One failing test stops the release. Visitors never see the change." in page
    # the journey carries the same three lines under every stage, and marks each row for the page's one-time effects
    assert page.count("Why it is here") >= 3 and 'data-stage="request" data-stage-state="done"' in page
    assert 'data-ring="stages"' in page and 'class="d-pill running"' in page


def test_the_spotlight_follows_the_release_from_the_start_to_live_and_names_a_stop(system):
    c, github, monitor, reader, clock = system
    now = clock[0]
    github.result["jobs"] = [
        {"name": "build", "status": "completed", "conclusion": "success",
         "steps": [{"name": "Run unit tests", "status": "completed", "conclusion": "success", "started_at": iso(now - 300), "completed_at": iso(now - 290)}]},
        {"name": "deploy", "status": "in_progress",
         "steps": [{"name": "Deploy, probe, promote; restore traffic on failure", "status": "in_progress", "started_at": iso(now - 120)}]}]
    c.observer = types.SimpleNamespace(service=lambda target: {"ready": True, "url": "https://demo.example.com", "updated_at": iso(now - 60),
                                                               "traffic": [{"revision": "old", "percent": 65, "plan_id": "other"}, {"revision": "new", "percent": 35, "plan_id": "run-1"}]})
    page = deployment_page.detail(monitor.detail(reader, "run-1"))
    assert "Move visitors to the new version" in page and 'class="d-move"' in page and "35%" in page and 'class="d-flow"' in page
    assert "Visitors on the new version" in page and "<animateMotion" in page
    c.observer = types.SimpleNamespace(service=lambda target: {"ready": True, "url": "https://demo.example.com", "updated_at": iso(now - 50),
                                                               "traffic": [{"revision": "new", "percent": 100, "plan_id": "run-1"}]})
    clock[0] += 11
    page = deployment_page.detail(monitor.detail(reader, "run-1"))
    assert 'class="d-beat"' in page and "For 5 minutes the platform watches" in page and "100%" in page
    owner, plan = c.store.fetch("run-1")
    plan.update(state="succeeded", workflow={**github.result, "status": "completed", "conclusion": "success"}, receipt={"state": "succeeded", "revision": "new"})
    c.store.finish(owner, plan, "succeeded")
    page = deployment_page.detail(monitor.detail(reader, "run-1"))
    assert "The new version is live" in page and "Open the app" in page and 'class="d-board b-live"' in page
    # a failed test: the board says what the stop means, the spotlight what the stage does and what to do now
    github.result["jobs"] = [{"name": "build", "status": "completed", "conclusion": "failure",
                              "steps": [{"name": "Run unit tests", "status": "completed", "conclusion": "failure", "started_at": iso(now - 300), "completed_at": iso(now - 290)}]}]
    plan.update(state="failed", workflow=github.result, receipt={})
    c.store.finish(owner, plan, "failed")
    page = deployment_page.detail(monitor.detail(reader, "run-1"))
    assert "Stopped at: Run the tests." in page and "Stopped here" in page and "What this stage does" in page and 'class="d-board b-stop"' in page
    assert 'class="t-not_run' in page


def test_the_script_animates_only_what_changed_and_the_page_honours_reduced_motion():
    script, style = deployment_page.SCRIPT, deployment_page.STYLE
    assert "dataset.stageState" in script and "classList.add('d-just')" in script and "strokeDasharray" in script
    assert "@media(prefers-reduced-motion:reduce)" in style and "d-car" in style and "@keyframes d-pop" in style
