"""Phase 6, items 6.1 and 6.2: buckets, topics and a Firestore database declared under `needs` in platform.yaml."""
import json
import subprocess
from pathlib import Path

import pytest
from conftest import TARGET, FakeGitHub

from labdeploy.config import Config
from labdeploy.controller import Controller, Store
from labdeploy.cost import estimate, findings
from labdeploy.infra import app_root, check_plan
from labdeploy.models import AppSpec, needs_env, parse_spec, spec_hash
from labdeploy.release import CloudRun

ROOT = dict(project_id="test-project", region="us-central1", state_bucket="test-project-tfstate", github_repository="lab/demo",
            github_repository_id="123", github_owner_id="45", bootstrap_image="us-docker.pkg.dev/cloudrun/container/hello@sha256:" + "a" * 64)


def test_needs_derive_fixed_variable_names_and_reject_clashes():
    spec = AppSpec(name="demo", needs={"buckets": ["uploads", "big-files"], "topics": ["events"], "database": "firestore"})
    assert needs_env(spec, "test-project") == {
        "BUCKET_UPLOADS": "test-project-demo-uploads", "BUCKET_BIG_FILES": "test-project-demo-big-files",
        "TOPIC_EVENTS": "projects/test-project/topics/demo-events", "SUBSCRIPTION_EVENTS": "projects/test-project/subscriptions/demo-events",
        "FIRESTORE_DATABASE": "demo",
    }
    assert needs_env(AppSpec(name="demo"), "p") == {}
    with pytest.raises(ValueError, match="set by the platform"):
        AppSpec(name="demo", needs={"buckets": ["uploads"]}, env={"BUCKET_UPLOADS": "mine"})
    with pytest.raises(ValueError, match="set by the platform"):
        AppSpec(name="demo", needs={"database": "firestore"}, secrets=["FIRESTORE_DATABASE"])
    for bad in ({"buckets": ["Uploads"]}, {"topics": ["a"]}, {"buckets": ["x", "x"]}, {"database": "postgres"}, {"buckets": [f"b{i}" for i in range(9)]}):
        with pytest.raises(ValueError):
            AppSpec(name="demo", needs=bad)
    text = "api_version: lab/v1\nname: demo\nneeds:\n  buckets: [uploads]\n  database: firestore\n"
    assert parse_spec(text).needs.buckets == ["uploads"]


def test_spec_hash_ignores_defaults_so_new_optional_fields_do_not_break_old_specs():
    assert spec_hash(AppSpec(name="demo")) == spec_hash(AppSpec(name="demo", needs={}))
    assert spec_hash(AppSpec(name="demo")) != spec_hash(AppSpec(name="demo", needs={"buckets": ["uploads"]}))
    assert spec_hash(AppSpec(name="demo", timeout_seconds=60)) == spec_hash(AppSpec(name="demo"))


class Recorder:
    """Stands in for gcloud: records every command, fails those that mention a missing resource."""

    def __init__(self):
        self.commands, self.missing = [], set()

    def __call__(self, command):
        self.commands.append(command)
        if any(part in self.missing for part in command):
            raise subprocess.CalledProcessError(1, command, stderr="ERROR: not found")
        return "ok"


def test_release_sets_the_derived_variables_and_checks_the_needs_exist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec = AppSpec(name="demo", needs={"buckets": ["uploads"], "topics": ["events"], "database": "firestore"}, env={"GREETING": "hi"})
    recorder = Recorder()
    adapter = CloudRun("test-project", "us-central1", "demo", "demo-probe@test-project.iam.gserviceaccount.com", runner=recorder)
    adapter.deploy("img@sha256:" + "a" * 64, spec, "r1")
    values = json.loads(Path("revision-env.yaml").read_text())
    assert values == {"BUCKET_UPLOADS": "test-project-demo-uploads", "TOPIC_EVENTS": "projects/test-project/topics/demo-events",
                      "SUBSCRIPTION_EVENTS": "projects/test-project/subscriptions/demo-events", "FIRESTORE_DATABASE": "demo", "GREETING": "hi"}
    assert "--env-vars-file=revision-env.yaml" in recorder.commands[-1]
    adapter.require_needs(spec)
    described = [c for c in recorder.commands if "describe" in c]
    assert len(described) == 4 and ["gcloud", "storage", "buckets", "describe", "gs://test-project-demo-uploads", "--format=value(name)"] in described
    assert ["gcloud", "firestore", "databases", "describe", "--database=demo", "--project", "test-project", "--format=value(name)"] in described
    recorder.missing = {"gs://test-project-demo-uploads", "--database=demo"}
    with pytest.raises(ValueError, match=r"Missing needs: bucket test-project-demo-uploads, Firestore database demo\. Ask for propose_infrastructure"):
        adapter.require_needs(spec)


def test_root_carries_the_needs_and_policies_guard_them():
    files = app_root("demo", needs={"buckets": ["uploads"], "topics": ["events"], "database": "firestore"}, **ROOT)
    assert 'needs                        = { buckets = ["uploads"], topics = ["events"], database = "firestore" }' in files["main.tf"]
    assert 'needs                        = { buckets = [], topics = [], database = "none" }' in app_root("demo", **ROOT)["main.tf"]
    with pytest.raises(ValueError, match="Bucket name"):
        app_root("a-long-service-name-x", needs={"buckets": ["a-very-long-bucket-nm"]}, **{**ROOT, "project_id": "a-very-long-project-name-x2345"})
    with pytest.raises(ValueError):
        app_root("demo", needs={"database": "postgres"}, **ROOT)

    def change(address, kind, actions, after):
        return {"address": address, "type": kind, "change": {"actions": actions, "after": after}}
    plan = {"resource_changes": [
        change("module.service.google_storage_bucket.need[\"uploads\"]", "google_storage_bucket", ["create"],
               {"location": "US-CENTRAL1", "labels": {"lab-deploy-app": "demo"}, "uniform_bucket_level_access": False, "public_access_prevention": "inherited"}),
        change("module.service.google_firestore_database.need[0]", "google_firestore_database", ["create"], {"location_id": "us-central1", "delete_protection_state": "DELETE_PROTECTION_DISABLED"}),
        change("module.service.google_pubsub_topic.need[\"events\"]", "google_pubsub_topic", ["create"], {"labels": {}}),
    ]}
    assert sorted(v["rule"] for v in check_plan(plan)) == ["labels_present", "private_bucket", "protected_database"]
    good = {"resource_changes": [
        change("module.service.google_storage_bucket.need[\"uploads\"]", "google_storage_bucket", ["create"],
               {"location": "US-CENTRAL1", "labels": {"lab-deploy-app": "demo"}, "uniform_bucket_level_access": True, "public_access_prevention": "enforced"}),
        change("module.service.google_firestore_database.need[0]", "google_firestore_database", ["create"], {"location_id": "us-central1", "delete_protection_state": "DELETE_PROTECTION_ENABLED"}),
    ]}
    assert check_plan(good) == []


def test_cost_model_reports_needs_as_use_based_with_a_zero_floor():
    spec = AppSpec(name="demo", needs={"buckets": ["uploads"], "topics": ["events"], "database": "firestore"})
    needs = estimate(spec)["needs"]
    assert [n["need"] for n in needs] == ["bucket", "topic", "firestore"] and all(n["floor_usd_month"] == 0.0 for n in needs)
    assert needs[0]["pricing"]["storage_usd_per_gib_month"] == 0.02 and needs[2]["pricing"]["free_per_day"]["reads"] == 50_000
    assert estimate(AppSpec(name="demo"))["needs"] == []
    ids = [f["id"] for f in findings(spec)]
    assert "needs_by_use" in ids and "needs_by_use" not in [f["id"] for f in findings(AppSpec(name="demo"))]


def test_plan_and_root_render_carry_the_needs(tmp_path):
    base = {"live_dispatch": True, "database": str(tmp_path / "db"),
            "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
            "principals": [{"name": "m", "token_sha256": "0" * 64, "applications": ["demo"], "can_deploy": True}],
            "platform": {"repository": "lab/platform"}}
    cfg = Config.model_validate(base)
    gh = FakeGitHub()
    gh.spec = {"name": "demo", "reviewed": True, "needs": {"buckets": ["uploads"], "database": "firestore"}}
    c = Controller(cfg, gh, Store(cfg.database))
    p = cfg.principals[0]
    plan = c.plan(p, "demo")
    assert plan["needs"]["env"]["BUCKET_UPLOADS"] == "test-project-demo-uploads" and plan["needs"]["database"] == "firestore"
    assert any("Declares needs" in w for w in plan["warnings"])
    rendered = c.render_infrastructure(p, "demo")
    assert 'needs                        = { buckets = ["uploads"], topics = [], database = "firestore" }' in rendered["files"]["main.tf"]
    base["applications"]["demo"]["infra"] = {"instance_alert_count": 4}
    with_settings = Config.model_validate(base)
    rendered = Controller(with_settings, gh, Store(with_settings.database)).render_infrastructure(p, "demo")
    assert "instance_alert_count         = 4" in rendered["files"]["main.tf"]
    base["applications"]["demo"]["infra"] = {"instance_alert_count": 0}
    with pytest.raises(ValueError, match="infra.instance_alert_count"):
        Config.model_validate(base)
    base["applications"]["demo"]["infra"] = {"unknown": 1}
    with pytest.raises(ValueError, match="not a known setting"):
        Config.model_validate(base)
