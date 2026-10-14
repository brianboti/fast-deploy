"""Regression tests for the findings of the 2026-10-12 review (docs/REVIEW-2026-10-12.md)."""
import json
import subprocess
import time
from pathlib import Path

import pytest
from conftest import TARGET, FakeGitHub

from labdeploy.config import Config
from labdeploy.controller import Controller, Store
from labdeploy.cost import build_report
from labdeploy.infra import app_root, check_plan, comment
from labdeploy.models import AppSpec
from labdeploy.release import MIGRATION_TIMEOUT, CloudRun, recover, rollout
from labdeploy.status import TERMINAL, advice

ROOT = dict(project_id="test-project", region="us-central1", state_bucket="test-project-tfstate", github_repository="lab/demo",
            github_repository_id="123", github_owner_id="45", bootstrap_image="us-docker.pkg.dev/cloudrun/container/hello@sha256:" + "a" * 64)
IMAGE = "us-central1-docker.pkg.dev/test-project/demo/demo@sha256:" + "a" * 64


def settings(tmp_path, **extra):
    base = {"live_dispatch": True, "database": str(tmp_path / "db"),
            "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET, "auto_deploy": True}},
            "principals": [{"name": "m", "token_sha256": "0" * 64, "applications": ["demo"], "can_deploy": True}]}
    base.update(extra)
    return base


def change(address, kind, actions, after=None, unknown=None):
    return {"address": address, "type": kind, "change": {"actions": actions, "after": after, "after_unknown": unknown or {}}}


# ---- finding 4 and 10: the migration phase is recoverable and its timeout is coherent

def test_recovery_knows_the_migrating_checkpoint(tmp_path):
    class Journal:
        def __init__(self, saved):
            self.saved = saved

        def read(self):
            return self.saved

        def write(self, record):
            self.saved = record

    class Cloud:
        service = "demo"

        def __init__(self):
            self.calls = []

        def describe(self):
            return {"status": {"traffic": [{"revisionName": "demo-rold", "percent": 100}]}}

        def traffic(self, allocations):
            self.calls.append(("traffic", dict(allocations)))

        def remove_candidate(self):
            self.calls.append(("remove_candidate",))

        def ready(self):
            return True
    target = {"project_id": "test-project"}
    journal = Journal({"plan_id": "1" * 32, "state": "migrating", "previous_traffic": {"demo-rold": 100}, "target": target, "candidate": "demo-r" + "1" * 16 + "a2"})
    cloud = Cloud()
    result = recover(cloud, journal, target)
    assert result["state"] == "recovered" and ("traffic", {"demo-rold": 100}) in cloud.calls
    plan = {"state": "cancelled", "receipt": {"state": "migrating"}, "failed_step": {"step": "Deploy, probe, promote; restore traffic on failure"}, "kind": None}
    assert "migration job may have run" in advice(plan)


def test_single_word_migration_resets_args_and_a_timeout_names_the_running_job(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    def runner(command):
        calls.append(command)
        if "execute" in command:
            raise subprocess.TimeoutExpired(command, 600)
        return ""
    adapter = CloudRun("test-project", "us-central1", "demo", "demo-probe@test-project.iam.gserviceaccount.com", runner=runner)
    spec = AppSpec(name="demo", needs={"migrate": ["./migrate.sh"]})
    with pytest.raises(RuntimeError, match="may still be running"):
        adapter.migrate(IMAGE, spec)
    update = calls[0]
    assert "--args=" in update and "--clear-args" not in update and f"--task-timeout={MIGRATION_TIMEOUT}" in update and "--command=./migrate.sh" in update
    assert MIGRATION_TIMEOUT < 600


# ---- finding 19: a re-run gets its own revision name, recorded for recovery

def test_rerun_attempts_get_their_own_candidate_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class Cloud:
        service = "demo"
        suffixes = []

        def describe(self):
            return {"status": {"url": "https://demo.run.app", "traffic": [{"revisionName": "demo-rold", "percent": 100},
                                                                         {"revisionName": "demo-r" + "1" * 16 + "a2", "tag": "candidate", "url": "https://c.run.app"}]}}

        def traffic(self, allocations):
            pass

        def deploy(self, image, spec, suffix, labels=None):
            self.suffixes.append(suffix)

        def health(self, url, host, path):
            return True

        def remove_candidate(self):
            pass

        def ready(self):
            return True

        def run(self, args):
            return ""
    rollout(Cloud(), IMAGE, AppSpec(name="demo", reviewed=True), "1" * 32, "receipt.json", attempt=2)
    receipt = json.loads(Path("receipt.json").read_text())
    assert Cloud.suffixes == ["r" + "1" * 16 + "a2"] and receipt["candidate"] == "demo-r" + "1" * 16 + "a2" and receipt["state"] == "succeeded"


# ---- findings 7 and 8: lost dispatches are final and visible; auto-deploy retries them

def test_lost_dispatches_become_final_and_are_retried_later(tmp_path):
    cfg = Config.model_validate(settings(tmp_path))
    gh = FakeGitHub()
    gh.lost = True

    class Invisible(FakeGitHub):
        def status(self, app, plan):
            return None  # GitHub shows no run
    c = Controller(cfg, Invisible(), Store(cfg.database))
    p = cfg.principals[0]
    plan = c.plan(p, "demo")
    c.store.claim(p.name, plan["id"])  # a crash between claim and dispatch leaves it dispatching
    now = plan["expires_at"] - 900
    assert not Controller.dispatch_lost({"expires_at": plan["expires_at"], "dispatch_state": "dispatching"}, now=now + 30)
    assert Controller.dispatch_lost({"expires_at": plan["expires_at"], "dispatch_state": "dispatching"}, now=now + 121)
    assert not Controller.dispatch_lost({"expires_at": plan["expires_at"], "dispatch_state": "dispatched"}, now=now + 600)
    assert Controller.dispatch_lost({"expires_at": plan["expires_at"], "dispatch_state": "dispatched"}, now=plan["expires_at"] + 1)
    assert "dispatch_lost" in TERMINAL and "Create a new plan" in advice({"state": "dispatch_lost", "kind": None})
    assert Controller.retryable({"state": "planned", "expires_at": plan["expires_at"]}, now=now + 1799) is False
    assert Controller.retryable({"state": "planned", "expires_at": plan["expires_at"]}, now=now + 1801) is True
    assert Controller.retryable({"state": "dispatch_lost", "expires_at": plan["expires_at"]}, now=now + 1801) is True
    assert Controller.retryable({"state": "succeeded", "expires_at": plan["expires_at"]}, now=now + 9999) is False


def test_finish_records_an_outcome_once_under_concurrent_writers(tmp_path):
    cfg = Config.model_validate(settings(tmp_path))
    store = Store(cfg.database)
    store.add("m", {"id": "a" * 32, "application": "demo", "sha": "b" * 40, "expires_at": time.time() + 900}, "dispatched")
    plan = {"id": "a" * 32, "application": "demo", "sha": "b" * 40, "expires_at": time.time() + 900}
    assert store.finish("m", plan, "succeeded") is True
    assert store.finish("m", plan, "succeeded") is True  # same state again: allowed, no new audit row
    events = store.events("m", "a" * 32)
    assert [e["event"] for e in events] == ["dispatched", "succeeded"]
    assert store.finish("m", {**plan, "id": "c" * 32}, "failed") is False  # unknown plan


# ---- findings 2, 15: the policy fails closed; rendered values are literals

WITHOUT_IP_CONFIGURATION = {"user_labels": {"lab-deploy-app": "demo"}, "deletion_protection_enabled": True, "backup_configuration": [{"enabled": True}]}


def test_policy_refuses_public_members_powerful_roles_platform_identities_and_unknown_values():
    plan = {"resource_changes": [
        change("a", "google_storage_bucket_iam_member", ["create"], {"bucket": "x", "role": "roles/storage.objectViewer", "member": "allUsers"}),
        change("b", "google_project_iam_member", ["create"], {"role": "roles/resourcemanager.projectIamAdmin", "member": "serviceAccount:demo-runtime@p"}),
        change("c", "google_service_account_iam_member", ["create"],
               {"service_account_id": "projects/p/serviceAccounts/lab-deploy-infra-apply@p.iam.gserviceaccount.com", "role": "roles/iam.serviceAccountTokenCreator", "member": "user:x"}),
        change("d", "google_project_iam_policy", ["update"], {"policy_data": "{}"}),
        change("e", "google_storage_bucket_iam_member", ["create"], {"bucket": "test-project-tfstate", "role": "roles/storage.objectViewer", "member": "serviceAccount:a@p"}),
        change("f", "google_iam_workload_identity_pool_provider", ["create"], {"attribute_condition": "assertion.repository_owner_id == '45'"}),
        change("g", "google_firestore_database", ["create"], {"location_id": "europe-west1", "delete_protection_state": "DELETE_PROTECTION_ENABLED"}),
        change("h", "google_sql_database_instance", ["update"], {"deletion_protection": True, "settings": [WITHOUT_IP_CONFIGURATION]}),
        change("i", "google_storage_bucket", ["update"],
               {"location": "US-CENTRAL1", "labels": {"lab-deploy-app": "demo"}, "uniform_bucket_level_access": True, "public_access_prevention": "inherited"}),
        change("j", "google_storage_bucket", ["create"], {"location": "US-CENTRAL1", "labels": {"lab-deploy-app": "demo"}, "uniform_bucket_level_access": True},
               unknown={"public_access_prevention": True}),
    ]}
    rules = {v["address"]: sorted({x["rule"] for x in check_plan(plan) if x["address"] == v["address"]}) for v in plan["resource_changes"]}
    assert rules["a"] == ["no_public_access"]
    assert rules["b"] == ["role_not_allowed"]
    assert rules["c"] == ["platform_identities_untouched", "role_not_allowed"]
    assert rules["d"] == ["authoritative_policy_forbidden"]
    assert rules["e"] == ["state_bucket_untouched"]
    assert rules["f"] == ["federation_unconditioned"]
    assert rules["g"] == ["region_policy"]
    assert rules["h"] == ["encrypted_connections", "private_database"]  # an update that drops ip_configuration is refused on both counts
    assert rules["i"] == ["private_bucket"]
    assert rules["j"] == ["private_bucket"]
    good = {"resource_changes": [
        change("ok", "google_project_iam_member", ["create"], {"role": "projects/test-project/roles/labDeployTagManager", "member": "serviceAccount:demo-deploy@p"}),
        change("ok2", "google_iam_workload_identity_pool_provider", ["create"], {"attribute_condition": "assertion.repository_id == '123' && assertion.repository_owner_id == '45'"}),
        # the service module's own grant to the spending cap: the cap-operator role on this application's service only
        change("ok3", "google_cloud_run_v2_service_iam_member", ["create", "delete"],
               {"name": "demo", "role": "projects/test-project/roles/labDeployCapOperator", "member": "serviceAccount:lab-deploy-spending-cap@p.iam.gserviceaccount.com"}),
    ]}
    assert check_plan(good) == []
    cap, operator = "serviceAccount:lab-deploy-spending-cap@p.iam.gserviceaccount.com", "projects/test-project/roles/labDeployCapOperator"
    cap_elsewhere = {"resource_changes": [
        change("x", "google_cloud_run_v2_service_iam_member", ["create"], {"name": "demo", "role": "roles/run.developer", "member": cap}),
        change("y", "google_storage_bucket_iam_member", ["create"], {"bucket": "b", "role": operator, "member": cap}),
        change("z", "google_cloud_run_v2_service_iam_member", ["create"], {"name": "demo", "role": operator, "member": "serviceAccount:lab-deploy-infra-apply@p.iam.gserviceaccount.com"}),
    ]}
    assert {v["address"] for v in check_plan(cap_elsewhere)} == {"x", "y", "z"}  # any other reference to a platform identity stays refused


def test_rendered_values_cannot_become_terraform_expressions():
    with pytest.raises(ValueError, match="migrate"):
        app_root("demo", needs={"migrate": ["${data.terraform_remote_state.project.outputs.x}"]}, **ROOT)
    with pytest.raises(ValueError):
        AppSpec(name="demo", needs={"migrate": ["^a^b"]})
    files = app_root("demo", needs={"migrate": ["%{if", "x"]}, **ROOT)
    assert 'migrate = ["%%{if", "x"]' in files["main.tf"]
    text = comment("demo", {"resource_changes": [change("x", "google_pubsub_topic", ["create"], {})]}, [], plan_text="# plan\n  + resource x\n```hidden```")
    assert "<details>" in text and "'''hidden'''" in text


# ---- findings 9 and 11: the Firestore note and the live compute bound

def test_report_prices_a_drifted_service_at_its_real_bound():
    annotations = {"autoscaling.knative.dev/maxScale": "40", "autoscaling.knative.dev/minScale": "0"}
    container = {"resources": {"limits": {"cpu": "1", "memory": "512Mi"}}}
    description = {"spec": {"template": {"metadata": {"annotations": annotations}, "spec": {"containerConcurrency": 80, "containers": [container]}}}}
    report = build_report("demo", "test-project", 14, description, {}, None)
    assert report["live_settings"]["maximum"] == 40 and report["estimate"]["live_compute_ceiling_usd_month"] > report["estimate"]["compute_ceiling_usd_month"]
    assert any("40 instances" in w for w in report["estimate"]["warnings"])


def test_names_follow_the_platform_bounds():
    for bad in ("a", "ab", "abc-", "a" * 22):
        with pytest.raises(ValueError):
            AppSpec(name=bad)
    assert AppSpec(name="abc").name == "abc"


# ---- finding 13: public clients, resource required, resource checked

def test_sign_in_requires_the_resource_and_registers_public_clients(tmp_path):
    import asyncio

    from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyHttpUrl

    from labdeploy.oauth import Provider
    cfg = Config.model_validate(settings(tmp_path, oauth={"public_url": "https://controller.example.test"}, allowed_hosts=["controller.example.test"]))
    provider = Provider(cfg, Store(cfg.database))
    client = OAuthClientInformationFull(client_id="c1", client_name="Claude", redirect_uris=[AnyHttpUrl("https://claude.ai/api/mcp/auth_callback")],
                                        token_endpoint_auth_method="client_secret_post", client_secret="s3cret")
    asyncio.run(provider.register_client(client))
    stored = asyncio.run(provider.get_client("c1"))
    assert stored.client_secret is None and stored.token_endpoint_auth_method == "none"
    params = dict(state=None, scopes=None, code_challenge="x", redirect_uri=AnyHttpUrl("https://claude.ai/api/mcp/auth_callback"), redirect_uri_provided_explicitly=True)
    with pytest.raises(AuthorizeError):
        asyncio.run(provider.authorize(stored, AuthorizationParams(resource=None, **params)))
    with pytest.raises(AuthorizeError):
        asyncio.run(provider.authorize(stored, AuthorizationParams(resource="https://other.example.test/mcp", **params)))
    url = asyncio.run(provider.authorize(stored, AuthorizationParams(resource="https://controller.example.test/mcp", **params)))
    assert url.startswith("https://controller.example.test/login?request=")
    token = provider.issue("c1", "m", "0" * 64, [], "https://other.example.test/mcp", "fam")
    assert provider.authenticate(token.access_token) is None  # issued for another resource: refused by the gate


# ---- finding 17: the backup bucket receives the copy and the configuration file, from the CLI too

def test_backup_uploads_the_copy_and_the_configuration(tmp_path, monkeypatch):
    cfg = Config(**settings(tmp_path, backup_bucket="lab-backups"))
    c = Controller(cfg, FakeGitHub(), Store(cfg.database))
    config_file = tmp_path / "config.yaml"
    config_file.write_text("principals: []\n")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append(command))
    result = c.backup_tick(config_path=str(config_file))
    assert result["uploaded"] == [f"gs://lab-backups/controller/{Path(result['backup']).name}", "gs://lab-backups/controller/config.yaml"]
    assert [command[:3] for command in calls] == [["gcloud", "storage", "cp"]] * 2 and calls[1][3] == str(config_file)
