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
    for bad in ({"buckets": ["Uploads"]}, {"topics": ["a"]}, {"buckets": ["x", "x"]}, {"database": "mysql"}, {"migrate": ["a,b"]}, {"migrate": ["x"] * 17}, {"buckets": [f"b{i}" for i in range(9)]}):
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
    assert 'needs                        = { buckets = ["uploads"], topics = ["events"], database = "firestore", migrate = [] }' in files["main.tf"]
    assert 'needs                        = { buckets = [], topics = [], database = "none", migrate = [] }' in app_root("demo", **ROOT)["main.tf"]
    sql = app_root("demo", needs={"database": "postgres", "migrate": ["alembic", "upgrade", "head"]}, **ROOT)["main.tf"]
    assert 'database = "postgres", migrate = ["alembic", "upgrade", "head"] }' in sql and "network                      = data.terraform_remote_state.project.outputs.network" in sql
    with pytest.raises(ValueError, match="migrate"):
        app_root("demo", needs={"migrate": ["a,b"]}, **ROOT)
    with pytest.raises(ValueError, match="Bucket name"):
        app_root("a-long-service-name-x", needs={"buckets": ["a-very-long-bucket-nm"]}, **{**ROOT, "project_id": "a-very-long-project-name-x2345"})
    with pytest.raises(ValueError):
        app_root("demo", needs={"database": "mysql"}, **ROOT)

    def change(address, kind, actions, after):
        return {"address": address, "type": kind, "change": {"actions": actions, "after": after}}
    plan = {"resource_changes": [
        change("module.service.google_storage_bucket.need[\"uploads\"]", "google_storage_bucket", ["create"],
               {"location": "US-CENTRAL1", "labels": {"lab-deploy-app": "demo"}, "uniform_bucket_level_access": False, "public_access_prevention": "inherited"}),
        change("module.service.google_firestore_database.need[0]", "google_firestore_database", ["create"], {"location_id": "us-central1", "delete_protection_state": "DELETE_PROTECTION_DISABLED"}),
        change("module.service.google_pubsub_topic.need[\"events\"]", "google_pubsub_topic", ["create"], {"labels": {}}),
    ]}
    assert sorted(v["rule"] for v in check_plan(plan)) == ["labels_present", "private_bucket", "protected_database"]
    unprotected = {"resource_changes": [change("module.service.google_sql_database_instance.need[0]", "google_sql_database_instance", ["create"],
                                               {"region": "us-central1", "deletion_protection": False, "labels": {"lab-deploy-app": "demo"},
                                                "settings": [{"ip_configuration": [{"ipv4_enabled": False}]}]})]}
    assert [v["rule"] for v in check_plan(unprotected)] == ["protected_database"]
    labelled = {"resource_changes": [change("module.service.google_sql_database_instance.need[0]", "google_sql_database_instance", ["create"],
                                            {"region": "us-central1", "deletion_protection": True,
                                             "settings": [{"user_labels": {"lab-deploy-app": "demo"}, "ip_configuration": [{"ipv4_enabled": False}]}]})]}
    assert check_plan(labelled) == []  # Cloud SQL keeps its labels under settings.user_labels
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
    assert 'needs                        = { buckets = ["uploads"], topics = [], database = "firestore", migrate = [] }' in rendered["files"]["main.tf"]
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


def test_observer_reports_each_need_and_the_health_view_names_missing_ones(tmp_path):
    import time

    import httpx

    from labdeploy.cloud import Observer

    def handler(request):
        path = request.url.path
        if path.endswith("/b/test-project-demo-uploads"):
            return httpx.Response(200, json={"name": "test-project-demo-uploads", "location": "US-CENTRAL1", "updated": "2026-10-12T19:09:29Z"})
        if path.endswith("/topics/demo-events"):
            return httpx.Response(200, json={"name": "projects/test-project/topics/demo-events"})
        if path.endswith("/subscriptions/demo-events"):
            return httpx.Response(200, json={"name": "projects/test-project/subscriptions/demo-events", "ackDeadlineSeconds": 20})
        if path.endswith("/databases/demo"):
            return httpx.Response(404, json={"error": {"message": "not found"}})
        raise AssertionError(str(request.url))
    cfg = Config.model_validate({"live_dispatch": False, "database": str(tmp_path / "db"), "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
                                 "principals": [{"name": "m", "token_sha256": "0" * 64, "applications": ["demo"], "can_deploy": True}],
                                 "observer": {"service_account": "lab-deploy-observer@test-project.iam.gserviceaccount.com"}})
    observer = Observer(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)), runner=lambda args: "t")
    observer.until, observer.cached = time.time() + 999, "t"
    items = observer.needs(cfg.applications["demo"].target, {"buckets": ["uploads"], "topics": ["events"], "database": "firestore"})
    assert [(i["need"], i["exists"]) for i in items] == [("bucket", True), ("topic", True), ("subscription", True), ("firestore", False)]
    assert items[0]["location"] == "US-CENTRAL1" and items[2]["ack_deadline_seconds"] == 20

    from test_phase5 import FakeObserver
    gh = FakeGitHub()
    gh.spec = {"name": "demo", "reviewed": True, "needs": {"buckets": ["uploads", "missing"]}}
    c = Controller(cfg, gh, Store(cfg.database), FakeObserver())
    health = c.service_health(cfg.principals[0], "demo")
    assert [n["exists"] for n in health["needs"]] == [True, False]
    assert any("Declared need bucket test-project-demo-missing does not exist" in note for note in health["notes"])


def test_decommission_lists_the_needs_with_their_data_warnings():
    from labdeploy.cli import decommission_steps
    app = Config.model_validate({"live_dispatch": False, "database": "x", "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
                                 "principals": []}).applications["demo"]
    steps = decommission_steps("demo", app, {"buckets": ["uploads"], "topics": ["events"], "database": "firestore"})
    text = "\n".join(steps)
    assert len(steps) == 12 and "gs://test-project-demo-uploads/**" in text and "pubsub topics delete demo-events" in text
    assert "--no-delete-protection" in text and "terraform state rm" in text
    assert len(decommission_steps("demo", app, None, "could not read")) == 10


def test_postgres_adds_connection_variables_private_egress_and_the_migration_phase(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    spec = AppSpec(name="demo", needs={"database": "postgres", "migrate": ["alembic", "upgrade", "head"]})
    assert needs_env(spec, "test-project", "us-central1") == {"DATABASE_INSTANCE": "test-project:us-central1:demo", "DATABASE_NAME": "app",
                                                              "DATABASE_USER": "demo-runtime@test-project.iam", "DATABASE_IP_TYPE": "private"}
    recorder = Recorder()
    adapter = CloudRun("test-project", "us-central1", "demo", "demo-probe@test-project.iam.gserviceaccount.com", runner=recorder)
    adapter.deploy("img@sha256:" + "a" * 64, spec, "r1")
    command = recorder.commands[-1]
    assert "--network=lab-deploy" in command and "--subnet=lab-deploy-run" in command and "--vpc-egress=private-ranges-only" in command
    adapter.deploy("img@sha256:" + "a" * 64, AppSpec(name="demo"), "r1")
    assert "--clear-network" in recorder.commands[-1]
    result = adapter.migrate("img@sha256:" + "b" * 64, spec)
    update, execute = recorder.commands[-2], recorder.commands[-1]
    assert update[:5] == ["gcloud", "run", "jobs", "update", "demo-migrate"] and "--command" in update and "alembic" in update and "--args=upgrade,head" in update
    assert "--max-retries=0" in update and "--network=lab-deploy" in update and "--env-vars-file=revision-env.yaml" in update
    assert execute[:6] == ["gcloud", "run", "jobs", "execute", "demo-migrate", "--wait"] and result == {"job": "demo-migrate", "execution": "ok"}
    adapter.require_needs(spec)
    described = [c for c in recorder.commands if "describe" in c]
    assert ["gcloud", "sql", "instances", "describe", "demo", "--project", "test-project", "--format=value(name)"] in described
    assert any(c[:5] == ["gcloud", "run", "jobs", "describe", "demo-migrate"] for c in described)
    recorder.missing = {"--format=value(name)"}  # every describe fails: the job and the instance are reported missing
    with pytest.raises(ValueError, match="migration job demo-migrate"):
        adapter.require_needs(spec)
    recorder.missing = {"--wait"}  # the update succeeds, the execution fails
    with pytest.raises(RuntimeError, match="Migration job demo-migrate failed"):
        adapter.migrate("img@sha256:" + "b" * 64, spec)


def test_rollout_runs_the_migration_before_the_candidate_and_stops_when_it_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from labdeploy.release import rollout

    class Cloud:
        service = "demo"

        def __init__(self, fail=False):
            self.calls, self.fail = [], fail

        def describe(self):
            return {"status": {"url": "https://demo.run.app", "traffic": [{"revisionName": "demo-rold", "percent": 100}, {"revisionName": "demo-r" + "1" * 16, "tag": "candidate", "url": "https://c.run.app"}]}}

        def traffic(self, allocations):
            self.calls.append(("traffic", dict(allocations)))

        def require_needs(self, spec):
            self.calls.append(("require_needs",))

        def migrate(self, image, spec):
            self.calls.append(("migrate", image))
            if self.fail:
                raise RuntimeError("Migration job demo-migrate failed; its log explains")
            return {"job": "demo-migrate", "execution": "e1"}

        def deploy(self, image, spec, suffix, labels=None):
            self.calls.append(("deploy", suffix))

        def health(self, url, host, path):
            return True

        def revision_logs(self, revision, limit=50):
            return []

        def run(self, args):
            return ""
    spec = AppSpec(name="demo", reviewed=True, needs={"database": "postgres", "migrate": ["alembic", "upgrade", "head"]})
    plan = "1" * 32
    cloud = Cloud()
    rollout(cloud, "us-central1-docker.pkg.dev/test-project/demo/demo@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", spec, plan, "receipt.json")
    names = [c[0] for c in cloud.calls]
    assert names.index("require_needs") < names.index("migrate") < names.index("deploy")
    receipt = json.loads(Path("receipt.json").read_text())
    assert [p["state"] for p in receipt["phases"]] == ["preparing", "migrating", "deploying", "promoting", "succeeded"] and receipt["migration"]["execution"] == "e1"
    failing = Cloud(fail=True)
    with pytest.raises(RuntimeError, match="Release failed"):
        rollout(failing, "us-central1-docker.pkg.dev/test-project/demo/demo@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", spec, plan, "receipt.json")
    receipt = json.loads(Path("receipt.json").read_text())
    assert "deploy" not in [c[0] for c in failing.calls] and receipt["state"] == "traffic_restored" and "Migration job" in receipt["error"]


def test_postgres_costs_show_the_always_on_floor_and_count_toward_the_ceiling(tmp_path):
    spec = AppSpec(name="demo", needs={"database": "postgres"})
    result = estimate(spec)
    assert result["needs"][0]["need"] == "postgres" and result["needs_floor_usd_month"] == 8.57
    assert any(f["id"] == "database_always_on" and f["severity"] == "warning" for f in findings(spec))
    base = {"live_dispatch": True, "database": str(tmp_path / "db"),
            "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET, "cost_ceiling_usd": 140}},
            "principals": [{"name": "m", "token_sha256": "0" * 64, "applications": ["demo"], "can_deploy": True}]}
    cfg = Config.model_validate(base)
    gh = FakeGitHub()
    gh.spec = {"name": "demo", "reviewed": True, "needs": {"database": "postgres"}}
    c = Controller(cfg, gh, Store(cfg.database))
    with pytest.raises(ValueError, match="plus the needs' floor"):
        c.plan(cfg.principals[0], "demo")
