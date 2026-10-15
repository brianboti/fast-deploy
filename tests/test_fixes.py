import copy
import json
from pathlib import Path

import httpx
import pytest
from conftest import TARGET
from test_release import IMAGE, PLAN_ID, Cloud

from labdeploy.config import Application
from labdeploy.github import GitHub
from labdeploy.journal import GCSJournal
from labdeploy.models import AppSpec
from labdeploy.release import recover, rollout
from labdeploy.status import summarize
from labdeploy.target import Target, verify_target
from labdeploy.workflows import lay_out


def test_success_updates_status_and_individual_checks(setup):
    c, p, gh = setup
    plan = c.plan(p, "demo")
    c.deploy(p, plan["id"])
    result = c.status(p, plan["id"])
    assert result["state"] == "succeeded"
    assert result["dispatch_state"] == "dispatched"
    assert all(v == "passed" for v in result["checks"].values())
    c.deploy(p, plan["id"])
    assert gh.calls == 1


@pytest.mark.parametrize("conclusion,state", [("failure", "failed"), ("cancelled", "cancelled"), ("timed_out", "timed_out"), ("success", "completed_unverified")])
def test_missing_evidence_never_becomes_pass(conclusion, state):
    result = summarize({"status": "completed", "conclusion": conclusion}, {"tests": "pending_in_ci"})
    assert result["state"] == state
    assert result["checks"]["tests"] == "unconfirmed"


def test_step_failure_is_not_hidden_by_workflow_result():
    steps = [{"name": "Run unit tests", "status": "completed", "conclusion": "failure"},
             {"name": "Audit Python dependencies", "status": "completed", "conclusion": "skipped"}]
    data = {"status": "completed", "conclusion": "failure", "jobs": [{"name": "build", "steps": steps}]}
    result = summarize(data, {})
    assert result["checks"]["tests"] == "failed"
    assert result["checks"]["dependency_scan"] == "not_run"


def test_plan_includes_target_and_rejects_registry_drift(setup):
    c, p, gh = setup
    plan = c.plan(p, "demo")
    assert plan["target"] == TARGET
    c.config.applications["demo"].target.registry = "changed"
    with pytest.raises(ValueError, match="Registry changed"):
        c.deploy(p, plan["id"])
    assert gh.calls == 0


@pytest.mark.parametrize("key,new", [("registry", "other"), ("service", "other"), ("recovery_bucket", "another-bucket"),
                                     ("wif_provider", "projects/987654321/locations/global/workloadIdentityPools/demo-gh/providers/github")])
def test_runner_rejects_target_drift(key, new):
    actual = Target.model_validate({**TARGET, key: new})
    with pytest.raises(ValueError, match="reviewed deployment target"):
        verify_target(json.dumps(TARGET), actual)


def test_application_rejects_missing_target():
    with pytest.raises(ValueError):
        Application(repository="lab/demo", service="demo")


class Journal:
    def __init__(self):
        self.value = None
    def read(self):
        return copy.deepcopy(self.value)
    def write(self, data):
        self.value = copy.deepcopy(data)


class RecoveryCloud(Cloud):
    def __init__(self, checks):
        super().__init__(checks)
        self.current = {"demo-old-a": 60, "demo-old-b": 40}
    def traffic(self, allocations):
        super().traffic(allocations)
        self.current = allocations.copy()
    def describe(self):
        result = super().describe()
        if self.deployed:
            result["status"]["traffic"].extend({"revisionName": k, "percent": v} for k,v in self.current.items())
        return result


def test_keyboard_interrupt_attempts_immediate_rollback(tmp_path):
    class Interrupted(RecoveryCloud):
        def health(self, *args):
            raise KeyboardInterrupt()
    cloud, journal = Interrupted([]), Journal()
    with pytest.raises(KeyboardInterrupt):
        rollout(cloud, IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "receipt", journal, TARGET)
    assert cloud.current == {"demo-old-a": 60, "demo-old-b": 40}
    assert journal.value["state"] == "traffic_restored" and journal.value["cancelled"] is True and journal.value["error"] == "the run was cancelled"


def test_disappeared_runner_recovers_from_durable_checkpoint(tmp_path):
    # A BaseException outside normal interruption handling models abrupt process loss.
    class RunnerLost(BaseException):
        pass
    class Interrupted(RecoveryCloud):
        calls = 0
        def health(self, *args):
            self.calls += 1
            if self.calls == 1:
                return True
            raise RunnerLost()
    cloud, journal = Interrupted([]), Journal()
    with pytest.raises(RunnerLost):
        rollout(cloud, IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "receipt", journal, TARGET)
    assert journal.value["state"] == "promoting"
    assert cloud.current == {"demo-r" + PLAN_ID[:16]: 100}
    recovered = recover(cloud, journal, TARGET)
    assert recovered["state"] == "recovered" and recovered["service_ready"] is True and cloud.cleaned
    assert cloud.current == {"demo-old-a": 60, "demo-old-b": 40}
    assert recover(cloud, journal, TARGET)["state"] == "no_recovery_needed"


def test_failed_checkpoint_prevents_cloud_mutation(tmp_path):
    class Broken(Journal):
        def write(self, data):
            raise OSError("storage unavailable")
    cloud = RecoveryCloud([])
    with pytest.raises(OSError):
        rollout(cloud, IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "receipt", Broken(), TARGET)
    assert not cloud.moves
    assert not cloud.deployed


def test_recovery_refuses_unrelated_traffic_or_target():
    cloud, journal = RecoveryCloud([]), Journal()
    journal.value = {"state": "promoting", "target": TARGET, "plan_id": PLAN_ID, "previous_traffic": {"demo-old-a": 100}}
    with pytest.raises(ValueError, match="outside this release"):
        recover(cloud, journal, TARGET)
    with pytest.raises(ValueError, match="target differs"):
        recover(cloud, journal, {**TARGET, "registry": "other"})
    assert not cloud.moves


def test_gcs_generation_precondition_and_conflict():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        assert request.url.params["ifGenerationMatch"] == "0"
        assert request.headers["authorization"] == "Bearer test"
        return httpx.Response(412)
    journal = GCSJournal("test-bucket", "demo", lambda: "test", httpx.Client(transport=httpx.MockTransport(handler)))
    assert journal.read() is None
    with pytest.raises(RuntimeError, match="concurrently"):
        journal.write({"state": "preparing"})


def test_gcs_reads_exact_generation_then_advances_write_version():
    seen = []
    def handler(request):
        seen.append(request)
        if request.method == "POST":
            assert request.url.params["ifGenerationMatch"] == "17"
            assert json.loads(request.content)["state"] == "recovered"
            return httpx.Response(200, json={"generation": "18"})
        if request.url.params.get("alt") == "media":
            assert request.url.params["generation"] == "17"
            return httpx.Response(200, json={"state": "promoting"})
        return httpx.Response(200, json={"generation": "17"})
    journal = GCSJournal("test-bucket", "demo", lambda: "test", httpx.Client(transport=httpx.MockTransport(handler)))
    assert journal.read() == {"state": "promoting"}
    journal.write({"state": "recovered"})
    assert journal.generation == "18"
    assert len(seen) == 3


def test_gcs_permission_failure_is_not_an_empty_checkpoint():
    journal = GCSJournal("test-bucket", "demo", lambda: "test", httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403))))
    with pytest.raises(httpx.HTTPStatusError):
        journal.read()
    assert journal.generation is None


def test_successful_checkpoint_is_never_rolled_back(tmp_path):
    cloud, journal = RecoveryCloud([True, True]), Journal()
    rollout(cloud, IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "receipt", journal, TARGET)
    moves = list(cloud.moves)
    assert recover(cloud, journal, TARGET)["last_release"] == "succeeded"
    assert cloud.moves == moves


def test_recovery_failure_keeps_unfinished_checkpoint():
    class Unavailable(RecoveryCloud):
        def traffic(self, allocations):
            raise OSError("Cloud API unavailable")
    cloud, journal = Unavailable([]), Journal()
    journal.value = {"state": "promoting", "target": TARGET, "plan_id": PLAN_ID, "previous_traffic": {"demo-old-a": 60, "demo-old-b": 40}}
    with pytest.raises(OSError):
        recover(cloud, journal, TARGET)
    assert journal.value["state"] == "promoting"


def test_github_uses_current_attempt_steps_and_checks_run_identity(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test")
    def handler(request):
        if request.url.path.endswith("/runs/42"):
            return httpx.Response(200, json={"id": 42, "head_sha": "a" * 40, "display_title": "deploy-" + PLAN_ID, "run_attempt": 2, "status": "completed", "conclusion": "success"})
        assert request.url.path.endswith("/runs/42/attempts/2/jobs")
        return httpx.Response(200, json={"jobs": [{"name": "deploy", "steps": []}]})
    gh = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    plan = {"id": PLAN_ID, "sha": "a" * 40, "dispatch_result": {"workflow_run_id": 42}}
    assert gh.status(Application(repository="lab/demo", service="demo", target=TARGET), plan)["run_attempt"] == 2
    with pytest.raises(ValueError, match="identity"):
        gh.status(Application(repository="lab/demo", service="demo", target=TARGET), {**plan, "sha": "b" * 40})


def test_both_workflows_share_queued_lock_and_recovery_is_independent():
    import yaml
    deploy = yaml.safe_load(lay_out(Path("templates/deploy-gcp.yml").read_text()))
    recovery = yaml.safe_load(lay_out(Path("templates/recover-gcp.yml").read_text()))
    assert deploy["concurrency"] == recovery["concurrency"] == {"group": "gcp-staging", "cancel-in-progress": False, "queue": "max"}
    assert "build" not in recovery["jobs"]
    assert all(step.get("with", {}).get("ref", "PLATFORM_COMMIT") == "PLATFORM_COMMIT" for step in recovery["jobs"]["recover"]["steps"])
