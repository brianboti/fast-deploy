"""Phase 16, part five: production's data (docs/ROADMAP.md D39, roadmap item 16.5)."""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from conftest import TARGET
from test_promotion import ROOT, STAGING_IMAGE, ready_controller

from labdeploy.cost import PRODUCTION_POSTGRES, estimate, needs_costs
from labdeploy.infra import _floor_of, app_root
from labdeploy.models import AppSpec, spec_hash
from labdeploy.release import MIGRATION_LOCK_OPTIONS, CloudRun


def spec(**needs):
    return AppSpec.model_validate({"name": "demo", "reviewed": True, "needs": {"database": "postgres", **needs}})


# ---- the contract and the root
def test_the_tier_is_a_postgres_choice_and_only_a_chosen_one_reaches_the_root():
    assert spec().needs.database_tier == "standard" and spec(database_tier="sla").needs.database_tier == "sla"
    with pytest.raises(ValueError, match="database_tier applies to database: postgres only"):
        AppSpec.model_validate({"name": "demo", "reviewed": True, "needs": {"database": "firestore", "database_tier": "ha"}})
    needs = {"buckets": [], "topics": [], "migrate": [], "database_auth": "iam", "database": "postgres"}
    production = dict(project_id="lab-deploy-prod-abcd", environment="production", github_environment="production-demo", workflow_files=["promote.yml"],
                      pipeline="platform", **{**ROOT, "state_bucket": "lab-deploy-prod-abcd-tfstate"})
    assert 'database = "postgres", migrate = [], database_tier = "ha" }' in app_root("demo", needs={**needs, "database_tier": "ha"}, **production)["main.tf"]
    assert "database_tier" not in app_root("demo", needs={**needs, "database_tier": "standard"}, **production)["main.tf"]  # roots stay as they were
    with pytest.raises(ValueError, match="database_tier is standard, ha or sla"):
        app_root("demo", needs={**needs, "database": "none", "database_tier": "ha"}, **production)


def test_production_data_is_recoverable_to_any_moment_of_the_week_and_staging_stays_cheap():
    module = Path("infra/gcp/modules/service/needs.tf").read_text()
    assert 'recoverable   = local.environment_name == "production"' in module
    assert 'database_tier = local.environment_name == "production" ? var.needs.database_tier : "standard"' in module  # staging: always the smallest
    assert "point_in_time_recovery_enabled = local.recoverable" in module and "transaction_log_retention_days = local.recoverable ? 7 : null" in module
    assert 'availability_type           = local.database_tier == "standard" ? "ZONAL" : "REGIONAL"' in module
    assert 'local.recoverable ? "POINT_IN_TIME_RECOVERY_ENABLED" : "POINT_IN_TIME_RECOVERY_DISABLED"' in module  # Firestore too
    assert "deletion_protection_enabled = true" in module and "retained_backups = 7" in module


# ---- what each tier costs, where it is shown
def test_each_tier_has_its_floor_in_production_and_staging_always_prices_the_smallest():
    floors = {tier: next(item["floor_usd_month"] for item in needs_costs(spec(database_tier=tier), environment="production")) for tier in PRODUCTION_POSTGRES}
    assert floors == {"standard": 8.57, "ha": 17.13, "sla": 100.43}
    assert next(item["floor_usd_month"] for item in needs_costs(spec(database_tier="sla"))) == 8.57  # staging runs the smallest whatever the tier
    assert estimate(spec(database_tier="ha"), environment="production")["needs_floor_usd_month"] == 17.13
    regional = {"settings": {"tier": "db-f1-micro", "availability_type": "REGIONAL"}}
    assert _floor_of("google_sql_database_instance", regional) == 17.13  # the plan's approval block prices the standby too
    assert _floor_of("google_sql_database_instance", {"settings": {"tier": "db-custom-1-3840", "availability_type": "REGIONAL"}}) == 100.43
    assert _floor_of("google_sql_database_instance", {"settings": {"tier": "db-f1-micro"}}) == 8.57


def test_a_promotion_shows_production_s_database_floor(tmp_path):
    c, brian, gh = ready_controller(tmp_path)
    chosen = spec(database_tier="ha")
    c.store.add("brian", {"id": "e" * 32, "application": "demo", "repository": "lab/demo", "branch": "main", "sha": "a" * 40, "commit": {"sha": "a" * 40},
                          "spec_hash": spec_hash(chosen), "spec": chosen.model_dump(), "cloud": "gcp", "environment": "staging", "target": TARGET,
                          "expires_at": time.time(), "checks": {}, "layout": {"path": "", "workflow": "deploy-gcp.yml", "github_environment": "staging"},
                          "receipt": {"state": "succeeded", "image": STAGING_IMAGE, "provenance": "github", "revision": "demo-r1"},
                          "outcome": {"state": "succeeded", "recorded_at": time.time()}}, "succeeded")
    plan = c.promote(brian, "demo")
    assert plan["cost"]["needs_floor_usd_month"] == 17.13 and plan["approval"]["cost"]["production"]["needs_floor_usd_month"] == 17.13
    backup = {item["item"]: item for item in plan["readiness"]}["backup"]
    assert backup["ok"] and "write-ahead log" in backup["detail"] and "(ha tier)" in backup["detail"]


# ---- a migration never waits on a lock for long
def test_a_postgres_migration_stops_after_ten_seconds_waiting_for_a_lock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ran = []

    def runner(args):
        ran.append(args)
        return "projects/p/locations/us-central1/jobs/demo-migrate/executions/demo-migrate-abc" if "execute" in args else ""
    cloud = CloudRun("p", "us-central1", "demo", "demo-probe@p.iam.gserviceaccount.com", runner=runner)
    cloud.migrate("img", spec(migrate=["alembic", "upgrade", "head"]))
    assert json.loads(Path("revision-env.yaml").read_text())["PGOPTIONS"] == MIGRATION_LOCK_OPTIONS == "-c lock_timeout=10s"
    own = AppSpec.model_validate({"name": "demo", "reviewed": True, "env": {"PGOPTIONS": "-c lock_timeout=2s"},
                                  "needs": {"database": "postgres", "migrate": ["alembic", "upgrade", "head"]}})
    cloud.migrate("img", own)
    assert json.loads(Path("revision-env.yaml").read_text())["PGOPTIONS"] == "-c lock_timeout=2s"  # the contract's own setting wins
    cloud.configuration(spec())
    assert "PGOPTIONS" not in json.loads(Path("revision-env.yaml").read_text())  # the revision itself never gets it


# ---- the restore drill
def test_the_restore_drill_only_ever_deletes_its_own_clone():
    script = Path("scripts/data-drill.sh")
    text = script.read_text()
    assert script.stat().st_mode & 0o111 and "gcloud sql instances clone" in text and "--point-in-time" in text
    assert '[[ "$CLONE" == "$SERVICE"-drill-* && "$CLONE" != "$SERVICE" ]]' in text and "trap cleanup EXIT" in text
    assert "pointInTimeRecoveryEnabled" in text  # refuses a database without recovery before anything is cloned
    if shutil.which("zsh"):
        bad = subprocess.run(["zsh", str(script), "p", "Not A Service"], capture_output=True, text=True)
        assert bad.returncode == 1 and "service name" in bad.stdout
        far = subprocess.run(["zsh", str(script), "p", "demo", "99999"], capture_output=True, text=True)
        assert far.returncode == 1 and "seven days" in far.stdout
