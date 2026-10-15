"""Phase 9 (1.9.0): the bake after promotion, the native spend cap record, Dockerfile lint, data-protection policy rules,
weekly drift and per-event cost tolls."""
import json
import subprocess

import pytest

from labdeploy.bake import BakeFailed, bake, crashes, judge, p95, request_stats
from labdeploy.models import AppSpec
from labdeploy.release import CloudRun, lint_summary, recover, rollout
from labdeploy.status import advice, release_phases

PLAN_ID = "c" * 32
IMAGE = "us-central1-docker.pkg.dev/test-project/demo/demo@sha256:" + "d" * 64
REVISION = "demo-r" + PLAN_ID[:16]


def requests(n, status=200, latency_ms=20.0, path="/api"):
    return [{"at": "2026-10-14T12:00:00Z", "status": status, "latency_ms": latency_ms, "path": path} for _ in range(n)]


class Clock:
    """A clock that sleep() advances, so a five-minute bake runs in microseconds."""

    def __init__(self, start=1_000_000.0):
        self.now = start
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class Logs:
    """Log reads by call: `window` is what the new revision has served so far, as a function of the elapsed seconds."""

    def __init__(self, clock, window, baseline=(), system=(), fail_reads=0):
        self.clock, self.window, self.baseline, self.system = clock, window, list(baseline), list(system)
        self.fail_reads = fail_reads
        self.queries = []

    def revision_requests(self, revisions, since, until=None):
        self.queries.append((tuple(revisions), since, until))
        if until is not None:
            return list(self.baseline)
        if self.fail_reads:
            self.fail_reads -= 1
            raise subprocess.CalledProcessError(1, ["gcloud"], stderr="ERROR: (gcloud.logging.read) PERMISSION_DENIED")
        return self.window(self.clock() - since)

    def revision_system_logs(self, revision, since):
        return list(self.system)


# ---- the rules
def test_p95_is_nearest_rank():
    assert p95([]) is None
    assert p95([5]) == 5
    assert p95(list(range(1, 101))) == 95


def test_health_path_is_left_out_of_the_rates():
    stats = request_stats(requests(30, path="/health", status=503) + requests(10), "/health")
    assert stats["requests"] == 10 and stats["errors_5xx"] == 0 and stats["health_requests"] == 30 and stats["health_errors_5xx"] == 30


def test_crash_messages_count_even_as_warnings_and_sandbox_notices_do_not():
    lines = [{"severity": "WARNING", "message": "Container called exit(1)."},
             {"severity": "WARNING", "message": "Container Sandbox: Unsupported syscall setsockopt(0x3,0x1,0xd)."},
             {"severity": "INFO", "message": "Starting new instance. Reason: AUTOSCALING"},
             {"severity": "ERROR", "message": 'STARTUP HTTP probe failed 12 times consecutively for container "demo-1"'},
             {"severity": "ERROR", "message": "Memory limit of 512 MiB exceeded with 530 MiB used."}]
    assert [line["message"][:9] for line in crashes(lines)] == ["Container", "STARTUP H", "Memory li"]


def test_judge_errors_need_enough_requests():
    few = request_stats(requests(5, status=500) + requests(10), "/health")
    assert judge(few, None, [], final=False)["result"] == "watching"
    assert judge(few, None, [], final=True)["result"] == "inconclusive"
    many = request_stats(requests(4, status=502) + requests(36), "/health")  # 10% > 5%
    assert judge(many, None, [], final=False)["rule"] == "errors"
    fine = request_stats(requests(1, status=500) + requests(39), "/health")  # 2.5%
    assert judge(fine, None, [], final=True)["result"] == "passed"


def test_judge_latency_against_the_previous_revision():
    baseline = request_stats(requests(25, latency_ms=100.0), "/health")
    slow = request_stats(requests(25, latency_ms=350.0), "/health")
    assert judge(slow, baseline, [], final=True)["rule"] == "latency"
    assert judge(slow, baseline, [], final=False)["result"] == "watching"  # latency is judged at the end only
    thin = request_stats(requests(5, latency_ms=100.0), "/health")
    verdict = judge(slow, thin, [], final=True)
    assert verdict["result"] == "passed" and "not compared" in verdict["notes"][0]


# ---- the watch
def test_bake_passes_and_reports_the_window():
    clock = Clock()
    logs = Logs(clock, lambda elapsed: requests(int(elapsed // 10)), baseline=requests(30, latency_ms=30.0))
    report = bake(logs, REVISION, {"demo-old": 100}, "/health", 300, clock(), sleep=clock.sleep, clock=clock)
    assert report["result"] == "passed" and report["window"]["requests"] == 30 and report["checks"] == 5
    assert sum(clock.slept) == 300 and max(clock.slept) == 60
    assert logs.queries[0][0] == ("demo-old",) and logs.queries[0][2] is not None  # the baseline: previous revisions, the hour before


def test_bake_stops_at_the_first_crash():
    clock = Clock()
    logs = Logs(clock, lambda elapsed: [], system=[{"at": "x", "severity": "WARNING", "message": "Container called exit(1)."}])
    with pytest.raises(BakeFailed, match="stopped serving") as caught:
        bake(logs, REVISION, {"demo-old": 100}, "/health", 300, clock(), sleep=clock.sleep, clock=clock)
    assert caught.value.report["rule"] == "crash" and sum(clock.slept) == 60


def test_bake_with_no_traffic_is_inconclusive_not_failed():
    clock = Clock()
    report = bake(Logs(clock, lambda elapsed: requests(3, path="/health")), REVISION, {"demo-old": 100}, "/health", 120, clock(),
                  sleep=clock.sleep, clock=clock)
    assert report["result"] == "inconclusive" and "only 0 request" in report["notes"][0]


def test_bake_unreadable_logs_are_unobserved_and_retried():
    clock = Clock()
    logs = Logs(clock, lambda elapsed: requests(30), fail_reads=1)
    report = bake(logs, REVISION, {"demo-old": 100}, "/health", 120, clock(), sleep=clock.sleep, clock=clock)
    assert report["result"] == "passed" and "retried" in report["notes"][-1]
    always = Logs(clock, lambda elapsed: requests(30), fail_reads=99)
    report = bake(always, REVISION, {"demo-old": 100}, "/health", 120, clock(), sleep=clock.sleep, clock=clock)
    assert report["result"] == "unobserved" and "PERMISSION_DENIED" in report["read_warning"]


def test_bake_zero_seconds_is_skipped_and_out_of_range_refused():
    clock = Clock()
    assert bake(Logs(clock, lambda e: []), REVISION, {}, "/health", 0, clock())["result"] == "skipped"
    with pytest.raises(ValueError, match="between 0 and 900"):
        bake(Logs(clock, lambda e: []), REVISION, {}, "/health", 901, clock())


# ---- inside the release
class Cloud:
    """The release adapter of tests/test_release.py, with log reads for the bake."""
    service = "demo"

    def __init__(self, window, system=()):
        self.deployed, self.moves, self.cleaned = False, [], False
        self.window, self.system, self.clock = window, list(system), None

    def describe(self):
        if not self.deployed:
            return {"status": {"traffic": [{"revisionName": "demo-old", "percent": 100}]}}
        return {"status": {"url": "https://demo.run.app", "traffic": [{"revisionName": REVISION, "tag": "candidate", "url": "https://candidate-demo.run.app"}]}}

    def traffic(self, allocations):
        self.moves.append(allocations)

    def deploy(self, image, spec, suffix, labels=None):
        self.deployed = True

    def health(self, *args):
        return True

    def remove_candidate(self):
        self.cleaned = True

    def ready(self):
        return True

    def revision_logs(self, revision, limit=50):
        return [{"at": "2026-10-14T12:01:00Z", "severity": "ERROR", "message": "KeyError: 'user'"}]

    def revision_requests(self, revisions, since, until=None):
        return [] if until is not None else self.window(self.clock() - since)

    def revision_system_logs(self, revision, since):
        return list(self.system)


def released(cloud, tmp_path, seconds=300, lint=None):
    clock = Clock()
    cloud.clock = clock
    return rollout(cloud, IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "receipt.json", bake_seconds=seconds, lint=lint,
                   sleep=clock.sleep, clock=clock)


def test_release_bakes_before_it_counts_as_succeeded(tmp_path):
    receipt = released(Cloud(lambda elapsed: requests(40)), tmp_path)
    assert receipt["state"] == "succeeded" and receipt["bake"]["result"] == "passed"
    assert [phase["state"] for phase in receipt["phases"]][-2:] == ["baking", "succeeded"]


def test_a_failed_bake_restores_the_previous_traffic(tmp_path):
    cloud = Cloud(lambda elapsed: requests(int(elapsed // 3), status=500) + requests(40))  # a fifth and more answer 500
    with pytest.raises(RuntimeError, match="traffic_restored"):
        released(cloud, tmp_path)
    saved = json.loads((tmp_path / "receipt.json").read_text())
    assert cloud.moves[-1] == {"demo-old": 100}
    assert saved["state"] == "traffic_restored" and saved["failed_phase"] == "baking" and saved["bake"]["rule"] == "errors"
    assert saved["error"].startswith("Bake failed") and saved["candidate_logs"][0]["message"] == "KeyError: 'user'"
    view = release_phases(saved)
    assert view["bake_summary"].startswith("bake failed") and view["failed_phase"] == "baking"
    text = advice({"state": "failed", "failed_step": {"step": "Deploy, probe, promote; restore traffic on failure"}, "receipt": saved})
    assert "failed its bake after promotion" in text and "previous revision serves again" in text


def test_a_release_without_bake_seconds_does_not_bake(tmp_path):
    receipt = released(Cloud(lambda elapsed: requests(40, status=500)), tmp_path, seconds=0)
    assert receipt["state"] == "succeeded" and "bake" not in receipt


def test_inconclusive_bake_is_said_plainly_after_success(tmp_path):
    receipt = released(Cloud(lambda elapsed: []), tmp_path)
    text = advice({"state": "succeeded", "receipt": receipt})
    assert receipt["bake"]["result"] == "inconclusive" and "too little traffic" in text


def test_cancel_while_baking_is_reported_as_such(tmp_path):
    saved = {"state": "traffic_restored", "failed_phase": "baking", "cancelled": True}
    text = advice({"state": "cancelled", "failed_step": None, "receipt": saved})
    assert "cancelled while baking" in text and "restored" in text


def test_a_runner_lost_while_baking_is_recovered():
    class Journal:
        def __init__(self, saved):
            self.saved = saved

        def read(self):
            return self.saved

        def write(self, record):
            self.saved = record

    class Live(Cloud):
        def describe(self):
            return {"status": {"traffic": [{"revisionName": REVISION, "percent": 100}]}}

    target = {"project_id": "test-project"}
    journal = Journal({"state": "baking", "plan_id": PLAN_ID, "target": target, "previous_traffic": {"demo-old": 100}, "candidate": REVISION})
    cloud = Live(lambda e: [])
    assert recover(cloud, journal, target)["state"] == "recovered"
    assert cloud.moves[-1] == {"demo-old": 100}


def test_lint_summary_travels_with_the_receipt(tmp_path):
    path = tmp_path / "hadolint.json"
    path.write_text(json.dumps([{"code": "DL3008", "level": "warning", "line": 2, "message": "Pin versions in apt get install"},
                                {"code": "DL3015", "level": "info", "line": 2, "message": "Avoid additional packages"}]))
    lint = lint_summary(path)
    assert lint["counts"] == {"warning": 1, "info": 1} and lint["findings"][0]["code"] == "DL3008"
    assert lint_summary(tmp_path / "missing.json") is None
    receipt = released(Cloud(lambda elapsed: requests(40)), tmp_path, lint=lint)
    assert receipt["dockerfile_lint"]["counts"]["warning"] == 1 and release_phases(receipt)["dockerfile_lint"] == lint


# ---- the adapter's log reads
def test_log_reads_build_a_bounded_filter_and_parse_requests():
    seen = []

    def runner(args):
        seen.append(args)
        if "run.googleapis.com%2Frequests" in args[3]:
            return json.dumps([{"timestamp": "2026-10-14T22:39:29Z", "httpRequest": {
                "status": 503, "latency": "0.003206851s", "requestUrl": "https://demo-x-uc.a.run.app/api/items?page=2"}}])
        return json.dumps([{"timestamp": "2026-10-14T22:40:00Z", "severity": "WARNING", "textPayload": "Container called exit(1)."}])

    adapter = CloudRun("test-project", "us-central1", "demo", "demo-probe@test-project.iam.gserviceaccount.com", runner=runner)
    lines = adapter.revision_requests(["demo-old-a", "demo-old-b"], 1_760_000_000, 1_760_003_600)
    assert lines == [{"at": "2026-10-14T22:39:29Z", "status": 503, "latency_ms": pytest.approx(3.206851), "path": "/api/items"}]
    query = seen[0][3]
    assert 'resource.labels.service_name="demo"' in query and '(resource.labels.revision_name="demo-old-a" OR resource.labels.revision_name="demo-old-b")' in query
    assert 'timestamp>="2025-10-09T08:53:20Z"' in query and 'timestamp<"2025-10-09T09:53:20Z"' in query
    system = adapter.revision_system_logs("demo-new", 1_760_000_000)
    assert system[0]["message"] == "Container called exit(1)." and "severity>=WARNING" in seen[1][3]
    with pytest.raises(ValueError, match="Invalid revision"):
        adapter.revision_requests(['demo" OR 1=1'], 0)


# ---- the setting travels from the operator to the runner
def test_bake_seconds_is_dispatched_only_when_it_differs_from_the_default():
    from labdeploy.github import GitHub

    sent = []

    class Recorder(GitHub):
        def __init__(self):
            pass

        def request(self, method, path, **kwargs):
            sent.append(kwargs["json"]["inputs"])

            class Empty:
                content = b""
            return Empty()

    class App:
        repository, workflow, branch = "lab/demo", "deploy-gcp.yml", "main"

    plan = {"sha": "a" * 40, "spec_hash": "b" * 64, "id": PLAN_ID, "target": {"project_id": "p"}}
    Recorder().dispatch(App(), {**plan, "bake_seconds": 300})
    Recorder().dispatch(App(), {**plan, "bake_seconds": 0})
    Recorder().dispatch(App(), plan)
    assert "bake_seconds" not in sent[0] and sent[1]["bake_seconds"] == "0" and "bake_seconds" not in sent[2]


def test_bake_seconds_is_bounded_in_the_configuration():
    from pydantic import ValidationError

    from labdeploy.config import ApplicationSettings
    assert ApplicationSettings(bake_seconds=600).bake_seconds == 600
    with pytest.raises(ValidationError):
        ApplicationSettings(bake_seconds=901)


def test_the_runner_refuses_a_malformed_bake_input(monkeypatch, tmp_path):
    from labdeploy import release
    with pytest.raises(ValueError, match="between 0 and 900"):
        rollout(Cloud(lambda e: []), IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "r.json", bake_seconds=1200)
    assert release.MAX_BAKE_SECONDS == 900


# ---- 9.1 the native spend cap, recorded by the operator
def test_cost_answers_name_both_spending_guards(setup):
    controller, principal, github = setup
    summary = controller.cost(principal, "demo")
    assert summary["spending_guards"]["cloud_run_spend_cap"]["status"] == "not_recorded"
    assert summary["findings"][0]["id"] == "no_native_spend_cap" and summary["findings"][-1]["id"] == "authenticated_only"
    controller.config.cloud_run_spend_caps["test-project"] = 20
    summary = controller.cost(principal, "demo")
    cap = summary["spending_guards"]["cloud_run_spend_cap"]
    assert cap["status"] == "recorded" and cap["amount_usd_month"] == 20 and "5xx" in cap["when_reached"]
    assert all(finding["id"] != "no_native_spend_cap" for finding in summary["findings"])
    assert controller.cost_report(principal, "demo")["spending_guards"]["project_id"] == "test-project"


def test_spend_cap_records_are_validated():
    from pydantic import ValidationError

    from labdeploy.config import Config
    base = {"applications": {}, "principals": []}
    assert Config.model_validate({**base, "cloud_run_spend_caps": {"test-project": 20}}).cloud_run_spend_caps == {"test-project": 20}
    for bad in ({"Test_Project": 20}, {"test-project": 0}):
        with pytest.raises(ValidationError, match="cloud_run_spend_caps"):
            Config.model_validate({**base, "cloud_run_spend_caps": bad})


def test_controller_status_reports_guards_and_drift(tmp_path):
    import hashlib

    from conftest import TARGET, FakeGitHub
    from fastapi.testclient import TestClient

    from labdeploy.config import Config
    from labdeploy.server import build_app
    config = Config.model_validate({
        "live_dispatch": False, "database": str(tmp_path / "plans.db"), "cloud_run_spend_caps": {"test-project": 25},
        "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
        "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True}]})
    with TestClient(build_app(config, FakeGitHub())) as client:
        headers = {"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"}
        result = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                            "params": {"name": "get_controller_status", "arguments": {}}}).json()["result"]
    status = result["structuredContent"]
    assert status["spending_guards"]["cloud_run_spend_caps"] == {"test-project": 25} and status["spending_guards"]["missing"] == []
    assert status["infrastructure_drift"]["status"] == "not_configured"


# ---- 9.4 data-protection rules, each with a planted violation
def change(address, kind, after, actions=("create",), unknown=None):
    return {"address": address, "type": kind, "change": {"actions": list(actions), "after": after, "after_unknown": unknown or {}}}


GOOD_SQL = {"region": "us-central1", "deletion_protection": True, "settings": [{
    "user_labels": {"lab-deploy-app": "demo"}, "deletion_protection_enabled": True, "backup_configuration": [{"enabled": True}],
    "ip_configuration": [{"ipv4_enabled": False, "ssl_mode": "ENCRYPTED_ONLY"}]}]}
GOOD_BUCKET = {"name": "test-project-demo-recovery", "location": "US-CENTRAL1", "labels": {"lab-deploy-app": "demo"}, "uniform_bucket_level_access": True,
               "public_access_prevention": "enforced", "force_destroy": False, "versioning": [{"enabled": True}]}


def planted(kind, base, edit):
    import copy
    after = copy.deepcopy(base)
    edit(after)
    return sorted({v["rule"] for v in __import__("labdeploy.infra", fromlist=["check_plan"]).check_plan({"resource_changes": [change("x", kind, after)]})})


def test_the_good_shapes_pass():
    from labdeploy.infra import check_plan
    assert check_plan({"resource_changes": [change("a", "google_sql_database_instance", GOOD_SQL), change("b", "google_storage_bucket", GOOD_BUCKET)]}) == []


@pytest.mark.parametrize("edit, rule", [
    (lambda a: a["settings"][0]["backup_configuration"][0].update(enabled=False), "database_backups"),
    (lambda a: a["settings"][0].pop("backup_configuration"), "database_backups"),
    (lambda a: a["settings"][0]["ip_configuration"][0].update(ssl_mode="ALLOW_UNENCRYPTED_AND_ENCRYPTED"), "encrypted_connections"),
    (lambda a: a["settings"][0].update(deletion_protection_enabled=False), "protected_database"),
])
def test_planted_database_violations(edit, rule):
    assert planted("google_sql_database_instance", GOOD_SQL, edit) == [rule]


@pytest.mark.parametrize("edit, rule", [
    (lambda a: a.update(force_destroy=True), "no_force_destroy"),
    (lambda a: a.update(versioning=[]), "versioned_checkpoints"),
    (lambda a: a.update(versioning=[{"enabled": False}]), "versioned_checkpoints"),
    (lambda a: a.update(public_access_prevention="inherited"), "private_bucket"),
])
def test_planted_bucket_violations(edit, rule):
    assert planted("google_storage_bucket", GOOD_BUCKET, edit) == [rule]


def test_an_ordinary_bucket_may_be_unversioned_and_firestore_is_never_deleted():
    plain = {**GOOD_BUCKET, "name": "test-project-demo-uploads", "versioning": []}
    assert planted("google_storage_bucket", plain, lambda a: None) == []
    firestore = {"location_id": "us-central1", "delete_protection_state": "DELETE_PROTECTION_ENABLED", "deletion_policy": "ABANDON"}
    assert planted("google_firestore_database", firestore, lambda a: None) == []
    assert planted("google_firestore_database", firestore, lambda a: a.update(deletion_policy="DELETE")) == ["protected_database"]


@pytest.mark.parametrize("kind, after, refused", [
    ("google_secret_manager_secret_version", {"secret": "projects/p/secrets/demo-KEY", "secret_data": "hunter2"}, True),
    ("google_secret_manager_secret_version", {"secret": "projects/p/secrets/demo-KEY", "secret_data": None, "secret_data_wo_version": 1}, False),
    ("google_sql_user", {"name": "app", "instance": "demo", "password": "hunter2"}, True),
    ("google_sql_user", {"name": "app", "instance": "demo", "password": None, "password_wo_version": 1}, False),
    ("random_password", {"length": 24}, True),
    ("tls_private_key", {"algorithm": "ED25519"}, True),
])
def test_secret_material_never_enters_state(kind, after, refused):
    from labdeploy.infra import check_plan
    rules = {v["rule"] for v in check_plan({"resource_changes": [change("x", kind, after)]})}
    assert ("no_secret_in_state" in rules) is refused


def test_no_rendered_root_or_module_can_store_secret_material():
    import re
    from pathlib import Path

    from labdeploy.infra import app_root
    root = dict(project_id="test-project", region="us-central1", state_bucket="test-project-tfstate", github_repository="lab/demo",
                github_repository_id="123", github_owner_id="45", bootstrap_image="us-docker.pkg.dev/cloudrun/container/hello@sha256:" + "a" * 64)
    texts = [path.read_text() for path in Path("infra/gcp/modules").rglob("*.tf")]
    for needs in ({}, {"buckets": ["uploads"], "topics": ["events"], "database": "firestore"}, {"database": "postgres", "migrate": ["alembic", "upgrade", "head"]}):
        texts += list(app_root("demo", needs=needs, **root).values())
    joined = "\n".join(texts)
    for pattern in (r'resource\s+"(random_password|tls_private_key|google_service_account_key|google_kms_secret_ciphertext)"',
                    r"\bsecret_data\s*=", r'resource\s+"google_sql_user"[^}]*\bpassword\s*='):
        assert not re.search(pattern, joined), pattern


def test_the_real_demo_plan_passes_even_when_every_resource_changes():
    import copy
    from pathlib import Path

    from labdeploy.infra import check_plan, drift
    plan = json.loads(Path("tests/fixtures/lab-demo-plan-2026-10-14.json").read_text())
    assert check_plan(plan) == []
    everything = copy.deepcopy(plan)
    for item in everything["resource_changes"]:
        item["change"]["actions"] = ["update"] if item["change"]["actions"] == ["no-op"] else item["change"]["actions"]
    assert check_plan(everything) == []
    found = drift(plan, "lab-demo")  # the provenance key 1.8.6 left for the root's next apply: a difference the report must name
    assert found["drifted"] and found["categories"] == ["data", "iam"] and len(found["serious"]) == 2


# ---- 9.5 drift
@pytest.mark.parametrize("kind, before, after, category", [
    ("google_project_iam_member", {}, {"role": "roles/logging.viewer"}, "iam"),
    ("google_cloud_run_v2_service_iam_member", {}, {"role": "roles/run.invoker"}, "exposure"),
    ("google_cloud_run_v2_service", {"ingress": "INGRESS_TRAFFIC_ALL", "labels": {"a": "1"}}, {"ingress": "INGRESS_TRAFFIC_INTERNAL_ONLY", "labels": {"a": "1"}}, "exposure"),
    ("google_cloud_run_v2_service", {"ingress": "INGRESS_TRAFFIC_ALL", "labels": {"a": "1"}}, {"ingress": "INGRESS_TRAFFIC_ALL", "labels": {"a": "2"}}, "other"),
    ("google_compute_subnetwork", {}, {"name": "lab-deploy-run"}, "network"),
    ("google_storage_bucket", {}, {"name": "x"}, "data"),
    ("google_monitoring_alert_policy", {}, {"display_name": "x"}, "monitoring"),
])
def test_drift_categories(kind, before, after, category):
    from labdeploy.infra import drift_category
    assert drift_category({"type": kind, "change": {"actions": ["update"], "before": before, "after": after}}) == category


def test_drift_report_and_issue():
    from labdeploy.infra import drift, drift_issue, drift_report
    clean = drift({"resource_changes": [{"address": "a", "type": "google_storage_bucket", "change": {"actions": ["no-op"]}}]}, "lab-demo")
    assert clean["drifted"] is False
    report = drift_report([clean, {"app": "sophia", "state": "none", "drifted": False}], ["lab-demo", "sophia", "lab-notebook"], "https://run", "2026-10-14T00:00:00Z")
    assert report["failed"] == ["lab-notebook"] and report["drifted"] == []
    assert "did not finish" in drift_issue(report) and "`lab-notebook`" in drift_issue(report)
    quiet = drift_report([clean], ["lab-demo"], None, "2026-10-14T00:00:00Z")
    assert "No drift" in drift_issue(quiet)


def test_drift_cli(tmp_path, capsys):
    from labdeploy.infra import main
    (tmp_path / "apps" / "lab-demo").mkdir(parents=True)
    (tmp_path / "apps" / "Bad_Name").mkdir()
    assert main(["roots-all", str(tmp_path / "apps")]) == 0 and capsys.readouterr().out.strip() == 'apps=["lab-demo"]'
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"resource_changes": []}))
    assert main(["drift", str(plan), "Bad Name"]) == 1
    assert main(["drift", str(plan), "lab-demo"]) == 0 and json.loads(capsys.readouterr().out)["drifted"] is False


class DriftGitHub:
    def __init__(self, report):
        self.report = report

    def drift_report(self, repository):
        return {"run_url": "https://github.com/lab/platform/actions/runs/9", "report": self.report}


def drift_controller(setup, report):
    from labdeploy.config import Platform
    controller, principal, github = setup
    controller.config.platform = Platform(repository="lab/platform")
    github.drift_report = DriftGitHub(report).drift_report
    return controller


def test_drift_status_and_the_watchdog(setup):
    import datetime
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    report = {"checked_at": now, "drifted": ["demo"], "serious": ["demo"], "failed": ["other"],
              "applications": [{"app": "demo", "drifted": True, "categories": ["exposure"]}]}
    controller = drift_controller(setup, report)
    status = controller.drift_status()
    assert status["status"] == "drift" and status["drifted"] == {"demo": ["exposure"]} and status["stale"] is False
    problems = controller.watchdog_tick()["problems"]
    assert any("infrastructure drift in exposure" in p for p in problems) and any("drift plan did not finish" in p for p in problems)


def test_a_stale_drift_report_is_a_watchdog_problem(setup):
    report = {"checked_at": "2026-09-01T00:00:00+00:00", "drifted": [], "serious": [], "failed": [], "applications": []}
    controller = drift_controller(setup, report)
    assert controller.drift_status()["stale"] is True
    assert any("schedule may have stopped" in p for p in controller.watchdog_tick()["problems"])


def test_the_drift_workflow_only_plans_and_the_identity_admits_it_only_from_main():
    from pathlib import Path

    import yaml
    workflow = yaml.safe_load(Path(".github/workflows/infra-drift.yml").read_text())
    assert workflow["permissions"] == {} and set(workflow[True]) == {"schedule", "workflow_dispatch"}
    text = Path(".github/workflows/infra-drift.yml").read_text()
    assert "-lock=false" in text and "terraform apply" not in text and "INFRA_APPLY" not in text
    assert workflow["jobs"]["plan"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert workflow["jobs"]["report"]["permissions"] == {"contents": "read", "issues": "write"}
    pipeline = Path("infra/gcp/modules/project/pipeline.tf").read_text()
    assert ("assertion.event_name in ['schedule', 'workflow_dispatch'] && assertion.ref == 'refs/heads/main' && "
            "assertion.workflow_ref == '${local.pipeline_repo}/.github/workflows/infra-drift.yml@refs/heads/main'") in pipeline
    assert 'toset(["schedule", "workflow_dispatch"])' in pipeline


# ---- 9.6 per-event charges
def test_fresh_images_count_only_release_builds_of_the_month():
    from labdeploy.cost import fresh_images
    listing = [
        {"version": "sha256:a", "createTime": "2026-10-14T17:40:21Z", "tags": ["511e2886", "release-" + "9" * 32, "serving-demo-r1"]},
        {"version": "sha256:b", "createTime": "2026-10-02T10:00:00Z", "tags": ["release-" + "8" * 32]},
        {"version": "sha256:c", "createTime": "2026-09-30T10:00:00Z", "tags": ["release-" + "7" * 32]},
        {"version": "sha256:d", "createTime": "2026-10-03T10:00:00Z", "tags": ["sha256-abc.att"]},
    ]
    assert fresh_images(listing, "2026-10") == ["sha256:a", "sha256:b"]


def test_job_minutes_round_up_per_job():
    from labdeploy.cost import job_minutes
    assert job_minutes({"started_at": "2026-10-14T10:00:00Z", "completed_at": "2026-10-14T10:01:01Z", "conclusion": "success"}) == 2
    assert job_minutes({"started_at": "2026-10-14T10:00:00Z", "completed_at": "2026-10-14T10:00:00Z", "conclusion": "success"}) == 0
    assert job_minutes({"started_at": "2026-10-14T10:00:00Z", "completed_at": "2026-10-14T10:09:00Z", "conclusion": "skipped"}) == 0
    assert job_minutes({"started_at": None, "completed_at": None}) == 0


def test_actions_minutes_free_when_public_and_counted_when_private():
    from labdeploy.cost import actions_minutes

    def api_for(private):
        def api(path, token, params=None):
            if path == "/repos/lab/app":
                return {"private": private}
            if path.endswith("/actions/runs"):
                return {"workflow_runs": [{"id": 1, "created_at": "2026-10-02T00:00:00Z", "path": ".github/workflows/deploy-gcp.yml", "name": "deploy-abc"},
                                          {"id": 2, "created_at": "2026-10-03T00:00:00Z", "path": ".github/workflows/ci.yml", "name": "CI"}]}
            return {"jobs": [{"started_at": "2026-10-02T00:00:00Z", "completed_at": "2026-10-02T00:03:30Z", "conclusion": "success"}]}
        return api
    assert actions_minutes("lab/app", "t", "2026-10", api_for(False))["billable"] is False
    counted = actions_minutes("lab/app", "t", "2026-10", api_for(True))
    assert counted["minutes"] == 8 and counted["by_workflow"] == {"deploy-gcp.yml": 4, "ci.yml": 4} and counted["usd_at_list_price"] == 0.048


def test_report_lines_carry_the_tolls_and_the_recommender_scope():
    from labdeploy.cost import issue_body, summary_lines
    report = {"service": "demo", "window_days": 14, "estimate": {"compute_ceiling_usd_month": 10.0, "idle_floor_usd_month": 0.0},
              "rightsizing": {"observed": None, "recommendations": [{"setting": "resources.memory_mib", "current": 1024, "recommended": 512,
                                                                     "reason": "p95 memory 20%", "estimated_monthly_saving_usd": 1.5}]},
              "release_tolls": {"month": "2026-10", "fresh_images": 15, "image_scans_usd": 3.9, "actions": {"billable": False}},
              "actual_spend": {"bigquery_bytes_processed": 12_500_000}}
    lines = summary_lines(report)
    assert any("15 fresh image(s) scanned, about $3.90; Actions minutes are free" in line for line in lines)
    assert any("12.5 MB of BigQuery" in line for line in lines) and any("advises only the billing mode" in line for line in lines)
    body = issue_body(report, "https://run")
    assert body.count("resources.memory_mib") == 1  # the proposal appears once, in the table


def test_billing_query_bytes_are_recorded():
    import time as clock

    from labdeploy.cost import billing_actuals

    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class Client:
        calls = 0

        def post(self, url, json, headers, timeout):
            Client.calls += 1
            if Client.calls == 1:
                return Response({"jobComplete": True, "totalBytesProcessed": "1000000", "rows": [{"f": [{"v": str(clock.time())}]}]})
            return Response({"jobComplete": True, "totalBytesProcessed": "2500000", "rows": [{"f": [{"v": "Cloud Run"}, {"v": "demo"}, {"v": "1.5"}]}]})

    result = billing_actuals("test-project.billing_export.gcp_billing_export_v1_X", "test-project", "token", client=Client())
    assert result["bigquery_bytes_processed"] == 3_500_000 and result["total_net_cost_usd"] == 1.5


# ---- the templates
def test_deploy_template_carries_the_bake_and_the_lint():
    from pathlib import Path

    import yaml
    text = Path("templates/deploy-gcp.yml").read_text()
    workflow = yaml.safe_load(text)
    assert workflow[True]["workflow_dispatch"]["inputs"]["bake_seconds"]["default"] == "300"
    assert workflow["jobs"]["deploy"]["timeout-minutes"] == 35
    assert text.count('[[ "$BAKE_SECONDS" =~ ^[0-9]{1,3}$ ]] && [ "$BAKE_SECONDS" -le 900 ]') == 2
    lint = next(step for step in workflow["jobs"]["build"]["steps"] if step.get("name") == "Lint the Dockerfile")
    assert "hadolint/releases/download/v2.15.1/hadolint-linux-x86_64" in lint["run"] and "sha256sum --check --strict" in lint["run"]
    assert "--no-fail" in lint["run"] and "HADOLINT_BLOCKING" in lint["run"]
    names = [step.get("name") for step in workflow["jobs"]["build"]["steps"]]
    assert names.index("Lint the Dockerfile") < names.index("Build without cloud credentials")
    assert "FAILED AFTER PROMOTION" in text
    rightsize = yaml.safe_load(Path("templates/rightsize-gcp.yml").read_text())
    assert rightsize["jobs"]["rightsize"]["permissions"]["actions"] == "read"


def test_hadolint_is_pinned_identically_in_the_template_and_ci():
    import re
    from pathlib import Path
    pins = {path: re.findall(r"hadolint/releases/download/(v[0-9.]+)/hadolint-linux-x86_64\n.*?([a-f0-9]{64})", Path(path).read_text(), re.S)
            for path in ("templates/deploy-gcp.yml", ".github/workflows/ci.yml")}
    assert all(len(found) == 1 for found in pins.values()) and len({found[0] for found in pins.values()}) == 1
