"""Phase 19: what a data-bearing application needs (docs/ROADMAP.md 19.1, 19.3, 19.4, D47): read-only bucket mounts, Claude
through Vertex AI, and requests as long as Cloud Run allows. `state: sqlite` was dropped before it was built (the roadmap's
Phase 19 says why)."""
from pathlib import Path

import pytest
from test_needs import ROOT

from labdeploy.cost import NEEDS_PRICING, needs_costs
from labdeploy.infra import app_root, check_plan
from labdeploy.models import AppSpec, containment, draft_dump, needs_env, spec_hash
from labdeploy.release import CloudRun

REPO = Path(__file__).resolve().parent.parent
SOPHIA = {"buckets": ["corpus"], "mounts": {"corpus": "/app/data/index"}, "models": ["claude"]}


# ---- 19.1 bucket mounts
def test_a_mount_names_a_declared_bucket_at_a_path_that_hides_nothing():
    spec = AppSpec(name="sophia", needs=SOPHIA)
    assert spec.needs.mounts == {"corpus": "/app/data/index"}
    assert AppSpec(name="sophia", needs={"buckets": ["models"], "mounts": {"models": "/models"}}).needs.mounts == {"models": "/models"}
    refused = {
        "not under needs.buckets": {"buckets": [], "mounts": {"corpus": "/data"}},
        "absolute path": {"buckets": ["corpus"], "mounts": {"corpus": "data/index"}},
        "would hide": {"buckets": ["corpus"], "mounts": {"corpus": "/app"}},
    }
    for message, needs in refused.items():
        with pytest.raises(ValueError, match=message):
            AppSpec(name="sophia", needs=needs)
    for path in ("/", "/tmp", "/var", "/usr/share/corpus", "/etc/corpus", "/dev/x", "/proc", "/app/../etc", "/app/./x", "/a,b", "/a=b", "/a b", "/a/"):
        with pytest.raises(ValueError):
            AppSpec(name="sophia", needs={"buckets": ["corpus"], "mounts": {"corpus": path}})
    with pytest.raises(ValueError, match="overlap"):
        AppSpec(name="sophia", needs={"buckets": ["corpus", "model"], "mounts": {"corpus": "/data", "model": "/data/model"}})
    with pytest.raises(ValueError, match="same path"):
        AppSpec(name="sophia", needs={"buckets": ["corpus", "model"], "mounts": {"corpus": "/data", "model": "/data"}})
    # A sibling whose name only starts the same is not inside the other.
    assert AppSpec(name="sophia", needs={"buckets": ["corpus", "model"], "mounts": {"corpus": "/data", "model": "/data2"}})


def test_a_mount_needs_the_second_generation_environment():
    reviewed_with_mount = AppSpec(name="sophia", needs=SOPHIA)
    assert containment(reviewed_with_mount)["execution_environment"] == "gen2"
    assert containment(AppSpec(name="demo"))["execution_environment"] == "default"  # exactly as before
    own = CloudRun("p", "us-central1", "sophia", "sophia-probe@p.iam.gserviceaccount.com", runner=lambda c: "")
    assert own.egress_flags(reviewed_with_mount) == ["--clear-network", "--execution-environment=gen2"]
    assert own.egress_flags(AppSpec(name="demo")) == ["--clear-network"]


def test_every_release_mounts_what_the_contract_names_and_clears_the_rest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # configuration() writes revision-env.yaml beside the run
    calls = []
    run = CloudRun("p", "us-central1", "sophia", "sophia-probe@p.iam.gserviceaccount.com", runner=lambda c: calls.append(c) or "")
    spec = AppSpec(name="sophia", needs=SOPHIA)
    assert run.volume_flags(spec) == [
        "--clear-volume-mounts", "--clear-volumes",
        "--add-volume=name=bucket-corpus,type=cloud-storage,bucket=p-sophia-corpus,readonly=true",
        "--add-volume-mount=volume=bucket-corpus,mount-path=/app/data/index",
    ]
    # Nothing mounted: still cleared, so a mount the contract dropped is gone with the next release.
    assert run.volume_flags(AppSpec(name="sophia")) == ["--clear-volume-mounts", "--clear-volumes"]
    run.deploy("us-docker.pkg.dev/p/r/sophia@sha256:" + "a" * 64, spec, "abc1234")
    deploy = calls[-1]
    assert deploy[:3] == ["gcloud", "run", "deploy"] and "--execution-environment=gen2" in deploy
    assert deploy.index("--clear-volumes") < deploy.index("--add-volume=name=bucket-corpus,type=cloud-storage,bucket=p-sophia-corpus,readonly=true")


# ---- 19.3 Claude through Vertex AI
def test_claude_sets_the_vertex_variables_and_nothing_else_is_a_model():
    env = needs_env(AppSpec(name="sophia", needs={"models": ["claude"]}), "lab-project")
    assert env == {"ANTHROPIC_VERTEX_PROJECT_ID": "lab-project", "CLOUD_ML_REGION": "global"}
    assert needs_env(AppSpec(name="demo"), "lab-project") == {}
    for needs in ({"models": ["gpt"]}, {"models": ["claude", "claude"]}):
        with pytest.raises(ValueError):
            AppSpec(name="sophia", needs=needs)
    with pytest.raises(ValueError, match="CLOUD_ML_REGION"):  # the platform sets it; the contract may not set it too
        AppSpec(name="sophia", needs={"models": ["claude"]}, env={"CLOUD_ML_REGION": "us"})


def test_the_root_grants_the_model_role_only_when_a_model_is_declared():
    plain = app_root("demo", **ROOT)["main.tf"]
    assert "models" not in plain and "model_user_role" not in plain  # roots without a model stay as they were
    sophia = app_root("sophia", needs={"buckets": ["corpus"], "mounts": {"corpus": "/app/data/index"}, "models": ["claude"]}, **ROOT)["main.tf"]
    assert 'buckets = ["corpus"], topics = [], database = "none", migrate = [], models = ["claude"] }' in sophia
    assert 'model_user_role              = try(data.terraform_remote_state.project.outputs.model_user_role, "")' in sophia
    assert "/app/data/index" not in sophia  # a mount is the release's, not Terraform's
    with pytest.raises(ValueError, match="models"):
        app_root("sophia", needs={"models": ["gpt"]}, **ROOT)
    with pytest.raises(ValueError, match="mounts"):
        app_root("sophia", needs={"mounts": {"corpus": "/data"}}, **ROOT)


def test_the_policy_takes_the_one_permission_role_and_refuses_vertex_ai_user():
    def grant(role):
        return {"resource_changes": [{"address": "module.service.google_project_iam_member.need_models_runtime[0]", "type": "google_project_iam_member",
                                      "change": {"actions": ["create"], "after": {"project": "test-project", "role": role,
                                                                                  "member": "serviceAccount:sophia-runtime@test-project.iam.gserviceaccount.com"}}}]}
    assert check_plan(grant("projects/test-project/roles/labDeployModelUser")) == []
    assert [v["rule"] for v in check_plan(grant("roles/aiplatform.user"))] == ["role_not_allowed"]


def test_the_foundation_holds_the_api_and_the_one_permission_role():
    project = (REPO / "infra/gcp/modules/project/main.tf").read_text()
    assert '"aiplatform.googleapis.com"' in project
    assert 'role_id     = "labDeployModelUser"' in project and 'permissions = ["aiplatform.endpoints.predict"]' in project
    assert 'output "model_user_role"' in (REPO / "infra/gcp/modules/project/outputs.tf").read_text()
    assert 'output "model_user_role"' in (REPO / "infra/gcp/project/outputs.tf").read_text()
    needs = (REPO / "infra/gcp/modules/service/needs.tf").read_text()
    assert 'resource "google_project_iam_member" "need_models_runtime"' in needs and "precondition" in needs
    variables = (REPO / "infra/gcp/modules/service/variables.tf").read_text()
    assert 'variable "model_user_role"' in variables and 'models = optional(list(string), [])' in variables


def test_the_cost_view_shows_the_model_by_the_token():
    items = needs_costs(AppSpec(name="sophia", needs={"models": ["claude"]}))
    assert items == [{"need": "model", "name": "claude", "floor_usd_month": 0.0, "pricing": NEEDS_PRICING["claude"]}]
    opus = NEEDS_PRICING["claude"]["usd_per_million_tokens"]["claude-opus-5-5"]
    assert opus == {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write_5m": 5.0}


# ---- 19.4 and what stays the same
def test_a_request_may_run_as_long_as_cloud_run_allows():
    assert AppSpec(name="sophia", timeout_seconds=3600).timeout_seconds == 3600
    with pytest.raises(ValueError):
        AppSpec(name="sophia", timeout_seconds=3601)


def test_existing_contracts_hash_and_draft_as_before():
    assert spec_hash(AppSpec(name="demo")) == spec_hash(AppSpec(name="demo", needs={"mounts": {}, "models": []}))
    plain = draft_dump(AppSpec(name="demo"))["needs"]
    assert "mounts" not in plain and "models" not in plain
    declared = draft_dump(AppSpec(name="sophia", needs=SOPHIA))["needs"]
    assert declared["mounts"] == {"corpus": "/app/data/index"} and declared["models"] == ["claude"]
