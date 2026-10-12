"""Phase 4: env values and secrets, the tests policy, the request timeout, lock-file advice, decommissioning."""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import TARGET, FakeGitHub
from test_release import IMAGE, PLAN_ID, Cloud

from labdeploy.analyzer import analyze
from labdeploy.cli import decommission_steps
from labdeploy.config import Config
from labdeploy.controller import Controller, Store
from labdeploy.models import AppSpec, parse_spec, spec_hash
from labdeploy.release import CloudRun, rollout
from labdeploy.status import summarize


def test_spec_accepts_env_secrets_tests_policy_and_timeout():
    spec = AppSpec(name="demo", reviewed=True, env={"GREETING": "hello", "MODE": "staging"}, secrets=["API_KEY"], tests="none", timeout_seconds=120)
    assert spec.env["GREETING"] == "hello" and spec.secrets == ["API_KEY"] and spec.tests == "none" and spec.timeout_seconds == 120
    assert AppSpec(name="demo").timeout_seconds == 60 and AppSpec(name="demo").tests == "required"
    for bad in ({"env": {"port": "1"}}, {"env": {"PORT": "1"}}, {"env": {"A": "x\ny"}}, {"secrets": ["API_KEY", "API_KEY"]}, {"env": {"A": "1"}, "secrets": ["A"]},
                {"timeout_seconds": 301}, {"tests": "sometimes"}, {"env": {f"V{i}": "x" for i in range(33)}}):
        with pytest.raises(ValueError):
            AppSpec(name="demo", **bad)
    assert spec_hash(AppSpec(name="demo", env={"A": "1"})) != spec_hash(AppSpec(name="demo", env={"A": "2"}))
    assert parse_spec(Path("examples/demo-app/platform.yaml").read_text()).env == {}


class Recorder:
    def __init__(self, answers=None):
        self.commands, self.answers = [], answers or {}

    def __call__(self, args):
        self.commands.append(args)
        joined = " ".join(args)
        for needle, answer in self.answers.items():
            if needle in joined:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return ""


def test_deploy_passes_env_file_secrets_and_timeout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recorder = Recorder()
    adapter = CloudRun("test-project", "us-central1", "demo", "demo-probe@test-project.iam.gserviceaccount.com", runner=recorder)
    adapter.deploy(IMAGE, AppSpec(name="demo", reviewed=True, env={"GREETING": "hello, world"}, secrets=["API_KEY"], timeout_seconds=120), "rabc")
    command = recorder.commands[-1]
    assert "--timeout=120" in command and "--set-secrets=API_KEY=demo-API_KEY:latest" in command and "--env-vars-file=revision-env.yaml" in command
    assert json.loads(Path("revision-env.yaml").read_text()) == {"GREETING": "hello, world"} and yaml.safe_load(Path("revision-env.yaml").read_text()) == {"GREETING": "hello, world"}
    adapter.deploy(IMAGE, AppSpec(name="demo", reviewed=True), "rabc")
    assert "--clear-env-vars" in recorder.commands[-1] and "--clear-secrets" in recorder.commands[-1] and "--timeout=60" in recorder.commands[-1]


def test_missing_secret_is_named_before_anything_changes(tmp_path):
    recorder = Recorder({"secrets describe demo-MISSING": subprocess.CalledProcessError(1, ["gcloud"], stderr="ERROR: not found"),
                         "versions list demo-EMPTY": "", "versions list demo-OK": "projects/1/secrets/demo-OK/versions/1"})
    adapter = CloudRun("test-project", "us-central1", "demo", "demo-probe@test-project.iam.gserviceaccount.com", runner=recorder)
    adapter.require_secrets(AppSpec(name="demo", reviewed=True, secrets=["OK"]))
    with pytest.raises(ValueError, match="demo-MISSING.*demo-EMPTY \\(no enabled version\\)"):
        adapter.require_secrets(AppSpec(name="demo", reviewed=True, secrets=["OK", "MISSING", "EMPTY"]))

    class Strict(Cloud):
        def require_secrets(self, spec):
            raise ValueError("Missing secrets: demo-API_KEY")
    cloud = Strict([True, True])
    with pytest.raises(RuntimeError, match="traffic_restored"):
        rollout(cloud, IMAGE, AppSpec(name="demo", reviewed=True, secrets=["API_KEY"]), PLAN_ID, tmp_path / "receipt.json")
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["error"].startswith("ValueError: Missing secrets: demo-API_KEY") and not cloud.deployed  # nothing was deployed
    assert rollout(Strict([True, True]), IMAGE, AppSpec(name="demo", reviewed=True), PLAN_ID, tmp_path / "r2.json")["state"] == "succeeded"  # no secrets: no check


def test_tests_none_is_declared_and_warned_not_hidden(tmp_path):
    config = Config.model_validate({"live_dispatch": True, "database": str(tmp_path / "db"),
                                    "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
                                    "principals": [{"name": "m", "token_sha256": hashlib.sha256(b"t").hexdigest(), "applications": ["demo"], "can_deploy": True}]})
    gh = FakeGitHub()
    gh.spec = {"name": "demo", "reviewed": True, "tests": "none", "secrets": ["API_KEY"]}
    c = Controller(config, gh, Store(config.database))
    plan = c.plan(config.principals[0], "demo")
    assert plan["checks"]["tests"] == "skipped_by_config" and any("tests: none" in w for w in plan["warnings"]) and any("demo-API_KEY" in w for w in plan["warnings"])
    steps = [{"name": "Run unit tests", "status": "completed", "conclusion": "skipped"}, {"name": "Audit Python dependencies", "status": "completed", "conclusion": "success"}]
    deploy = [{"name": n, "status": "completed", "conclusion": "success"} for n in ("Validate artifact and deployment configuration", "Deploy, probe, promote; restore traffic on failure")]
    workflow = {"status": "completed", "conclusion": "success", "jobs": [{"name": "build", "steps": steps}, {"name": "deploy", "steps": deploy}]}
    result = summarize(workflow, plan["checks"])
    assert result["state"] == "succeeded" and result["checks"]["tests"] == "skipped_by_config"
    assert summarize(workflow, {**plan["checks"], "tests": "pending_in_ci"})["checks"]["tests"] == "not_run"  # an unexpected skip stays visible


def test_analyzer_flags_requirements_without_hashes():
    report = analyze({"requirements.txt": "fastapi==0.1\n"}, "demo")
    assert any("not reproducible" in e for e in report["evidence"]) and any("tests: none" in r for r in report["review_required"])
    assert not any("not reproducible" in e for e in analyze({"requirements.txt": "fastapi==0.1 --hash=sha256:abc\n"}, "demo")["evidence"])


def test_decommission_checklist_names_the_apps_resources():
    config = Config.model_validate({"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}}, "principals": []})
    steps = decommission_steps("demo", config.applications["demo"])
    text = "\n".join(steps)
    assert steps[0].startswith("Pause it") and "gcloud run services delete demo" in text and "gs://test-project-demo-recovery" in text
    assert "terraform destroy" in text and "lab/demo" in text and len(steps) == 9


def test_deploy_workflow_reads_the_test_policy_and_prefers_lock_files():
    workflow = yaml.safe_load(Path("templates/deploy-gcp.yml").read_text())
    steps = {s.get("name"): s for s in workflow["jobs"]["build"]["steps"]}
    assert steps["Read the application's test policy"]["id"] == "policy"
    assert steps["Run unit tests"]["if"] == "steps.policy.outputs.tests == 'required'" and "requirements.lock" in steps["Run unit tests"]["run"]
    names = [s.get("name") for s in workflow["jobs"]["build"]["steps"]]
    assert names.index("Install trusted platform dependencies") < names.index("Read the application's test policy") < names.index("Run unit tests")
