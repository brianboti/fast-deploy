import json
import subprocess

import pytest

from labdeploy.models import AppSpec
from labdeploy.release import CloudRun, rollout

PLAN_ID = "a" * 32
IMAGE = "us-central1-docker.pkg.dev/test-project/demo/demo@sha256:" + "b" * 64


class Cloud:
    service = "demo"
    checks = None
    def __init__(self, checks, rollback_fail=False):
        self.checks = iter(checks)
        self.deployed = False
        self.moves = []
        self.cleaned = False
        self.rollback_fail = rollback_fail

    def describe(self):
        if not self.deployed:
            return {"status": {"traffic": [{"revisionName": "demo-old-a", "percent": 60}, {"revisionName": "demo-old-b", "percent": 40}]}}
        return {"status": {"url": "https://demo.run.app", "traffic": [{"revisionName": "demo-r" + PLAN_ID[:16], "tag": "candidate", "url": "https://candidate-demo.run.app"}]}}

    def traffic(self, allocations):
        if self.rollback_fail and self.deployed and "demo-old-a" in allocations:
            raise RuntimeError("cloud unavailable")
        self.moves.append(allocations)

    def deploy(self, image, spec, suffix):
        self.deployed = True

    def health(self, *args):
        return next(self.checks)

    def remove_candidate(self):
        self.cleaned = True

    def ready(self):
        return not getattr(self, "broken", False)


def test_success(tmp_path):
    cloud = Cloud([True, True])
    result = rollout(cloud, IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "receipt.json")
    assert result["state"] == "succeeded"
    assert cloud.moves[-1] == {"demo-r" + PLAN_ID[:16]: 100}
    assert cloud.cleaned


@pytest.mark.parametrize("checks", [[False], [True, False]])
def test_health_failure_restores_exact_previous_split(tmp_path, checks):
    cloud = Cloud(checks)
    receipt = tmp_path / "receipt.json"
    with pytest.raises(RuntimeError, match="traffic_restored"):
        rollout(cloud, IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, receipt)
    assert cloud.moves[-1] == {"demo-old-a": 60, "demo-old-b": 40}
    saved = json.loads(receipt.read_text())
    assert saved["state"] == "traffic_restored" and "health check failed" in saved["error"] and saved["service_ready"] is True
    assert cloud.cleaned


def test_rollback_failure_reported_truthfully(tmp_path):
    cloud = Cloud([False], rollback_fail=True)
    with pytest.raises(RuntimeError, match="manual_recovery"):
        rollout(cloud, IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "receipt.json")


def test_mutable_image_rejected(tmp_path):
    with pytest.raises(ValueError, match="immutable"):
        rollout(Cloud([]), "demo:latest", AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "receipt.json")


def failing(stderr):
    def runner(args):
        raise subprocess.CalledProcessError(1, args, stderr=stderr)
    return runner


def adapter_with(describe_payload, runner):
    adapter = CloudRun("test-project", "us-central1", "demo", "demo-probe@test-project.iam.gserviceaccount.com", runner=runner)
    adapter.describe = lambda: describe_payload
    return adapter


def test_traffic_update_is_judged_by_routing_state_not_exit_code():
    stderr = "ERROR: (gcloud.run.services.update-traffic) The user-provided container failed the configured startup probe checks."
    applied = {"status": {"traffic": [{"revisionName": "demo-old-a", "percent": 100}]}}
    adapter_with(applied, failing(stderr)).traffic({"demo-old-a": 100})  # gcloud failed, but the split is in place
    with pytest.raises(RuntimeError, match="startup probe"):
        adapter_with({"status": {"traffic": [{"revisionName": "demo-old-b", "percent": 100}]}}, failing(stderr)).traffic({"demo-old-a": 100})


def test_candidate_tag_removal_is_judged_by_tag_presence():
    stderr = "ERROR: (gcloud.run.services.update-traffic) revision is not ready"
    adapter_with({"spec": {"traffic": [{"revisionName": "demo-old-a", "percent": 100}]}, "status": {"traffic": []}}, failing(stderr)).remove_candidate()
    with pytest.raises(RuntimeError, match="not ready"):
        adapter_with({"spec": {"traffic": [{"revisionName": "demo-r1", "tag": "candidate"}]}, "status": {"traffic": []}}, failing(stderr)).remove_candidate()


def test_failed_deploy_records_the_command_error(tmp_path):
    class Rejected(Cloud):
        broken = True  # the failed candidate is now the newest revision, so the service reports not Ready

        def deploy(self, image, spec, suffix):
            raise subprocess.CalledProcessError(1, ["gcloud"], stderr="ERROR: (gcloud.run.deploy) The user-provided container failed the configured startup probe checks.")
    cloud = Rejected([])
    with pytest.raises(RuntimeError, match="traffic_restored"):
        rollout(cloud, IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "receipt.json")
    saved = json.loads((tmp_path / "receipt.json").read_text())
    assert saved["error"].startswith("ERROR: (gcloud.run.deploy)") and "startup probe" in saved["error"]
    assert saved["service_ready"] is False and "previous revision" in saved["note"]


def test_describe_error_prefers_the_error_line_over_trailing_hints():
    from labdeploy.release import describe_error
    stderr = "Deploying...\nERROR: (gcloud.run.deploy) The container failed the startup probe.\nLogs URL: https://example\nFor more troubleshooting guidance, see https://example"
    assert describe_error(subprocess.CalledProcessError(1, ["gcloud"], stderr=stderr)) == "ERROR: (gcloud.run.deploy) The container failed the startup probe."
    assert describe_error(ValueError("bad")) == "ValueError: bad"
