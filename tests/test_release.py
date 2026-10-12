import json

import pytest

from labdeploy.models import AppSpec
from labdeploy.release import rollout

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
    assert json.loads(receipt.read_text())["state"] == "traffic_restored"
    assert cloud.cleaned


def test_rollback_failure_reported_truthfully(tmp_path):
    cloud = Cloud([False], rollback_fail=True)
    with pytest.raises(RuntimeError, match="manual_recovery"):
        rollout(cloud, IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "receipt.json")


def test_mutable_image_rejected(tmp_path):
    with pytest.raises(ValueError, match="immutable"):
        rollout(Cloud([]), "demo:latest", AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "receipt.json")

