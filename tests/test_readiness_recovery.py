import copy
import datetime
import json

import httpx
import pytest
from conftest import TARGET

from labdeploy.journal import GCSJournal
from labdeploy.recovery_supervisor import Settings, Supervisor
from labdeploy.release import CloudRun, recover

PLAN = "a" * 32


def checkpoint():
    return {"schema_version": 2, "plan_id": PLAN, "state": "baking", "target": TARGET,
            "heartbeat_at": "2026-10-26T00:00:00+00:00", "previous_traffic": {"demo-old": 100},
            "expected_traffic": {"demo-new": 100}, "candidate": "demo-new",
            "origin": {"run_url": "https://github.com/lab/platform/actions/runs/5"}}


class Journal:
    def __init__(self, record):
        self.record = copy.deepcopy(record)
    def read(self):
        return copy.deepcopy(self.record)
    def write(self, record):
        self.record = copy.deepcopy(record)


def test_supervisor_never_dispatches_until_original_run_is_completed():
    records = {"releases/demo.json": Journal(checkpoint()), "recovery-requests/demo.json": Journal(None)}
    class Factory:
        def __init__(self, *args):
            self.key = "releases/demo.json"
        def read(self):
            return records[self.key].read()
        def write(self, value):
            records[self.key].write({**value, "heartbeat_at": "2026-10-26T00:10:00+00:00"})  # as GCSJournal stamps every write
    calls, state = [], {"status": "in_progress"}
    class Github:
        def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path.endswith("/runs/10"):  # the recovery run itself
                return httpx.Response(200, json={"status": "in_progress"}, request=httpx.Request(method, "https://api.github.com" + path))
            data = {"status": state["status"], "path": ".github/workflows/promote.yml", "head_branch": "main", "display_title": "promote-" + PLAN}
            return httpx.Response(200, json=data if method == "GET" else {"workflow_run_id": 10}, request=httpx.Request(method, "https://api.github.com" + path))
    settings = Settings(repository="lab/platform", app_id="123", key_secret="projects/test-project/secrets/RECOVERY_KEY", targets={"demo": TARGET}, observe_only=False)
    now = datetime.datetime(2026, 10, 26, 0, 10, tzinfo=datetime.UTC).timestamp()
    supervisor = Supervisor(settings, Github(), lambda: "test", clock=lambda: now, journal_factory=Factory)
    assert supervisor.tick()["demo"] == "waiting_for_runner_completion"
    assert all("dispatches" not in path for _, path, _ in calls)
    state["status"] = "completed"
    assert supervisor.tick()["demo"] == "recovery_requested"
    assert supervisor.tick()["demo"] == "recovery_requested_or_unknown"
    assert sum(path.endswith("dispatches") for _, path, _ in calls) == 1
    assert records["recovery-requests/demo.json"].record["plan_id"] == PLAN


def test_unknown_cloud_mutation_and_manual_traffic_changes_stop_recovery():
    class Cloud:
        service = "demo"
        def describe(self):
            return {"status": {"traffic": [{"revisionName": "demo-old", "percent": 20}, {"revisionName": "demo-new", "percent": 80}]}}
        def traffic(self, _):
            pytest.fail("must not overwrite a manual split")
    journal = Journal({**checkpoint(), "pending_mutation": "traffic"})
    with pytest.raises(ValueError, match="unknown outcome"):
        recover(Cloud(), journal, TARGET)
    journal = Journal(checkpoint())
    with pytest.raises(ValueError, match="outside this release"):
        recover(Cloud(), journal, TARGET)


def test_failed_checkpoint_fences_further_writes_and_operation_claim_cannot_repeat():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(412)
    journal = GCSJournal("test-bucket", "demo", lambda: "test", httpx.Client(transport=httpx.MockTransport(handler)))
    journal.generation = "1"
    with pytest.raises(RuntimeError, match="concurrently"):
        journal.write(checkpoint())
    with pytest.raises(RuntimeError, match="heartbeat failed"):
        journal.write(checkpoint())
    assert len(calls) == 1
    with pytest.raises(ValueError, match="already claimed"):
        journal.claim(PLAN, {"image": "digest"})
    assert calls[-1].url.params["ifGenerationMatch"] == "0"


def test_conditional_traffic_checks_exact_split_and_sends_etag():
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"etag": "original", "trafficStatuses": [{"revision": "demo-old", "percent": 100}]})
        return httpx.Response(200, json={"done": True, "name": "projects/test-project/locations/us-central1/operations/one"})
    adapter = CloudRun("test-project", "us-central1", "demo", TARGET["probe_service_account"], runner=lambda _: "test")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="Traffic changed"):
        adapter.conditional_traffic({"demo-new": 100}, [{"demo-old": 50, "demo-new": 50}], client)
    assert len(calls) == 1
    adapter.conditional_traffic({"demo-new": 100}, [{"demo-old": 100}], client)
    assert json.loads(calls[-1].content)["etag"] == "original"
    assert calls[-1].url.params["updateMask"] == "traffic"


class DurableJournal(Journal):
    heartbeat_error = None
    def __init__(self):
        super().__init__(None)
        self.claims = set()
    def write(self, record):
        if self.heartbeat_error:
            raise RuntimeError("Release heartbeat failed")
        super().write(record)
    def claim(self, identifier, binding, attempt=1):
        if (identifier, attempt) in self.claims:
            raise ValueError("already claimed")
        self.claims.add((identifier, attempt))


def test_a_failed_deploy_restores_traffic_and_leaves_the_checkpoint_recoverable(tmp_path):
    import subprocess

    from test_release import IMAGE, PLAN_ID, Cloud

    from labdeploy.models import AppSpec
    from labdeploy.release import rollout
    class Broken(Cloud):
        def deploy(self, *args, **kwargs):
            raise subprocess.CalledProcessError(1, ["gcloud"], stderr="ERROR: Revision is not ready")
    journal, cloud = DurableJournal(), Broken([])
    with pytest.raises(RuntimeError, match="traffic_restored"):
        rollout(cloud, IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "receipt.json", journal=journal, target=TARGET)
    assert journal.record["state"] == "traffic_restored" and "pending_mutation" not in journal.record
    assert journal.record["failed_mutation"] == "deploy"
    assert recover(cloud, journal, TARGET)["state"] == "no_recovery_needed"  # the next release is not blocked
    assert journal.claims == {(PLAN_ID, 1)}  # one claim per attempt: a GitHub re-run claims attempt 2


def test_a_journal_failure_mid_rollout_still_restores_traffic(tmp_path):
    from test_release import IMAGE, PLAN_ID, Cloud

    from labdeploy.models import AppSpec
    from labdeploy.release import rollout
    journal = DurableJournal()
    class Split(Cloud):
        def traffic(self, allocations):
            super().traffic(allocations)
            if len(allocations) > 1 and any(name.endswith(PLAN_ID[:16]) for name in allocations):
                journal.heartbeat_error = "RuntimeError"  # Cloud Storage stayed unavailable through the retries
    cloud = Split([True, False])
    with pytest.raises(RuntimeError):
        rollout(cloud, IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "receipt.json", journal=journal, target=TARGET,
                steps=[10, 100], sleep=lambda _: None)
    assert cloud.moves[-1] == {"demo-old-a": 60, "demo-old-b": 40}
    assert json.loads((tmp_path / "receipt.json").read_text())["state"] == "traffic_restored"


def test_a_checkpoint_conflict_holds_instead_of_restoring(tmp_path):
    from test_release import IMAGE, PLAN_ID, Cloud

    from labdeploy.models import AppSpec
    from labdeploy.release import rollout
    journal = DurableJournal()
    class Conflict(Cloud):
        def health(self, *args):
            journal.heartbeat_error = "JournalConflict"  # someone else wrote the checkpoint
            return False
    cloud = Conflict([])
    with pytest.raises(RuntimeError, match="reconciliation"):
        rollout(cloud, IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "receipt.json", journal=journal, target=TARGET)
    assert not any("demo-old-a" in move for move in cloud.moves[1:])


def test_journal_retries_a_transient_error_and_accepts_its_own_landed_write():
    stored, calls = {}, []
    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            if len(calls) == 1:
                stored["body"], stored["generation"] = request.content, "2"
                raise httpx.ReadTimeout("lost response", request=request)
            return httpx.Response(412)
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=stored["body"])
        return httpx.Response(200, json={"generation": stored["generation"]})
    journal = GCSJournal("test-bucket", "demo", lambda: "test", httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None)
    journal.generation = "1"
    journal.write(checkpoint())
    assert journal.generation == "2" and journal.heartbeat_error is None


def test_recovery_refused_before_sending_does_not_hold_the_next_attempt():
    from labdeploy.release import MutationRefused
    class Cloud:
        service = "demo"
        def describe(self):
            return {"status": {"traffic": [{"revisionName": "demo-new", "percent": 100}]}}
        def conditional_traffic(self, allocations, allowed):
            raise MutationRefused("still reconciling")
    journal = Journal(checkpoint())
    with pytest.raises(ValueError, match="reconciling"):
        recover(Cloud(), journal, TARGET)
    assert "pending_mutation" not in journal.record


def test_a_recovery_that_ends_without_finishing_the_checkpoint_is_escalated():
    records = {"releases/demo.json": Journal(checkpoint()),
               "recovery-requests/demo.json": Journal({"plan_id": PLAN, "state": "dispatched", "run_id": 11, "heartbeat_at": "2026-10-26T00:05:00+00:00"})}
    class Factory:
        def __init__(self, *args):
            self.key = "releases/demo.json"
        def read(self):
            return records[self.key].read()
        def write(self, value):
            records[self.key].write(value)
    runs = {"5": {"status": "completed", "path": ".github/workflows/promote.yml", "head_branch": "main", "display_title": "promote-" + PLAN},
            "11": {"status": "in_progress"}}
    class Github:
        def request(self, method, path, **kwargs):
            return httpx.Response(200, json=runs[path.rsplit("/", 1)[1]], request=httpx.Request(method, "https://api.github.com" + path))
    settings = Settings(repository="lab/platform", app_id="123", key_secret="projects/test-project/secrets/RECOVERY_KEY", targets={"demo": TARGET}, observe_only=False)
    at = {"now": datetime.datetime(2026, 10, 26, 0, 10, tzinfo=datetime.UTC).timestamp()}
    supervisor = Supervisor(settings, Github(), lambda: "test", clock=lambda: at["now"], journal_factory=Factory)
    assert supervisor.tick()["demo"] == "recovery_requested_or_unknown"
    at["now"] += 900
    assert supervisor.tick()["demo"].startswith("manual_review: recovery did not complete")
    runs["11"] = {"status": "completed", "conclusion": "failure"}
    assert supervisor.tick()["demo"].startswith("manual_review: recovery run ended (failure)")
