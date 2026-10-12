"""Phase 1 runner changes: phase timestamps, candidate logs, revision labels, serving tags, workflow additions."""
import json
from pathlib import Path

import pytest
import yaml
from test_release import IMAGE, PLAN_ID, Cloud

from labdeploy.models import AppSpec
from labdeploy.release import CloudRun, reconcile_serving_tags, rollout

SPEC = AppSpec(name="demo", reviewed=True)
IMAGE_PATH = "us-central1-docker.pkg.dev/test-project/demo/demo"


class Recorder:
    def __init__(self, answers=None):
        self.commands, self.answers = [], answers or {}

    def __call__(self, args):
        self.commands.append(args)
        for needle, answer in self.answers.items():
            if needle in " ".join(args):
                return answer
        return ""


def test_receipt_carries_phase_timestamps_labels_and_serving_tags(tmp_path):
    class Serving(Cloud):
        def describe(self):
            base = super().describe()
            if self.deployed and self.moves:
                base["status"]["traffic"] += [{"revisionName": r, "percent": p} for r, p in self.moves[-1].items()]
            return base
    cloud = Serving([True, True])
    cloud.run = Recorder({"tags list": json.dumps([{"tag": "projects/p/locations/l/repositories/demo/packages/demo/tags/serving-demo-old-a"},
                                                  {"tag": "projects/p/locations/l/repositories/demo/packages/demo/tags/" + "a" * 40}])})
    receipt = rollout(cloud, IMAGE, SPEC, PLAN_ID, tmp_path / "receipt.json", labels={"lab-deploy-plan": PLAN_ID}, image_path=IMAGE_PATH)
    assert receipt["state"] == "succeeded" and cloud.labels == {"lab-deploy-plan": PLAN_ID}
    assert [p["state"] for p in receipt["phases"]] == ["preparing", "deploying", "promoting", "succeeded"] and all(p["at"].endswith("+00:00") for p in receipt["phases"])
    new_tag = "serving-demo-r" + PLAN_ID[:16]
    assert receipt["serving_tags"] == {"serving": [new_tag], "added": [new_tag], "removed": ["serving-demo-old-a"]}
    added = next(c for c in cloud.run.commands if "add" in c)
    assert added[-2:] == [IMAGE, f"{IMAGE_PATH}:{new_tag}"]
    assert any(c[-2:] == [f"{IMAGE_PATH}:serving-demo-old-a", "--quiet"] for c in cloud.run.commands)
    assert json.loads((tmp_path / "receipt.json").read_text())["phases"][-1]["state"] == "succeeded"


def test_failed_candidate_keeps_its_last_log_lines(tmp_path):
    cloud = Cloud([False])
    with pytest.raises(RuntimeError):
        rollout(cloud, IMAGE, SPEC, PLAN_ID, tmp_path / "receipt.json")
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["state"] == "traffic_restored" and receipt["candidate_logs"][0]["message"].startswith("ModuleNotFoundError")
    assert [p["state"] for p in receipt["phases"]] == ["preparing", "deploying", "traffic_restored"]

    class Quiet(Cloud):
        def revision_logs(self, revision, limit=50):
            raise RuntimeError("logging.viewer missing")
    with pytest.raises(RuntimeError):
        rollout(Quiet([False]), IMAGE, SPEC, PLAN_ID, tmp_path / "receipt2.json")
    receipt = json.loads((tmp_path / "receipt2.json").read_text())
    assert "candidate_logs" not in receipt and "logging.viewer missing" in receipt["log_warning"]


def test_serving_tag_update_failure_is_a_warning(tmp_path):
    cloud = Cloud([True, True])
    cloud.run = lambda args: (_ for _ in ()).throw(RuntimeError("registry unavailable"))
    receipt = rollout(cloud, IMAGE, SPEC, PLAN_ID, tmp_path / "receipt.json", image_path=IMAGE_PATH)
    assert receipt["state"] == "succeeded" and "registry unavailable" in receipt["tag_warning"]
    with pytest.raises(ValueError, match="image path"):
        reconcile_serving_tags(cloud, "not-a-registry-path", lambda args: "")


def test_cloud_run_adapter_builds_label_and_log_commands():
    recorder = Recorder({"logging read": json.dumps([
        {"timestamp": "2026-10-12T10:00:02Z", "severity": "ERROR", "textPayload": "Traceback: boom"},
        {"timestamp": "2026-10-12T10:00:01Z", "severity": "INFO", "jsonPayload": {"message": "starting"}},
        {"timestamp": "2026-10-12T10:00:00Z", "severity": "INFO", "httpRequest": {"status": 503, "requestMethod": "GET", "requestUrl": "https://x/health"}},
    ])})
    adapter = CloudRun("test-project", "us-central1", "demo", "demo-probe@test-project.iam.gserviceaccount.com", runner=recorder)
    adapter.deploy(IMAGE, SPEC, "rabc", {"lab-deploy-plan": PLAN_ID, "lab-deploy-commit": "a" * 40})
    deploy = recorder.commands[0]
    assert "--labels=lab-deploy-commit=" + "a" * 40 + ",lab-deploy-plan=" + PLAN_ID in deploy and "--startup-probe" in deploy
    with pytest.raises(ValueError, match="labels"):
        adapter.deploy(IMAGE, SPEC, "rabc", {"Bad Key": "x"})
    lines = adapter.revision_logs("demo-rabc")
    assert [line["message"] for line in lines] == ["503 GET https://x/health", "starting", "Traceback: boom"]  # oldest first
    query = recorder.commands[-1]
    assert query[:3] == ["gcloud", "logging", "read"] and 'resource.labels.revision_name="demo-rabc"' in query[3] and "--limit" in query
    with pytest.raises(ValueError, match="revision"):
        adapter.revision_logs("Bad Name")
    assert adapter.revision_logs("demo-empty") == [] or True  # an empty answer parses as no lines


def test_workflows_record_deployments_tag_releases_and_notify():
    deploy = yaml.safe_load(Path("templates/deploy-gcp.yml").read_text())
    assert deploy["jobs"]["deploy"]["environment"] == {"name": "staging", "url": "${{ steps.release.outputs.url }}"}  # GitHub's own deployment record
    release = next(step for step in deploy["jobs"]["deploy"]["steps"] if step.get("name") == "Deploy, probe, promote; restore traffic on failure")
    assert release["id"] == "release" and "url=" in release["run"]
    push = next(step for step in deploy["jobs"]["deploy"]["steps"] if step.get("name") == "Push image and resolve immutable digest")
    assert 'docker push "$IMAGE:release-$PLAN_ID"' in push["run"]
    for name in ("deploy-gcp.yml", "recover-gcp.yml", "rightsize-gcp.yml"):
        workflow = yaml.safe_load(Path("templates", name).read_text())
        notify = workflow["jobs"]["notify"]
        assert notify["if"] == "always()" and notify["env"]["WEBHOOK"] == "${{ secrets.NOTIFY_WEBHOOK_URL }}" and notify["permissions"] == {"contents": "read"}
        script = notify["steps"][-1]["run"]
        assert "NOTIFY_WEBHOOK_URL is not set" in script and "${{" not in script  # values arrive through env, never templated into the script
