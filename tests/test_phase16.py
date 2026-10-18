"""Phase 16, part one: production as a project of its own (docs/ROADMAP.md D37)."""
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml
from conftest import TARGET, FakeGitHub

from labdeploy.config import Config, LiveConfig
from labdeploy.controller import Controller, Store
from labdeploy.github import GitHub
from labdeploy.infra import PRODUCTION_WORKFLOWS, PUBLISHED_ENVIRONMENT, ROOT_LABEL, app_root, roots

PRODUCTION = {"project_id": "lab-deploy-prod-abcd", "project_number": "987654321", "reviewers": ["brianboti"]}
ROOT = dict(region="us-central1", github_repository="lab/platform", github_repository_id="123", github_owner_id="45",
            bootstrap_image="us-docker.pkg.dev/cloudrun/container/hello@sha256:" + "a" * 64)


def settings(tmp_path, **extra):
    data = {"live_dispatch": True, "database": str(tmp_path / "db"), "platform": {"repository": "lab/platform"},
            "onboarding": {"project_id": "test-project", "project_number": "123456789"},
            "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
            "principals": [{"name": "brian", "token_sha256": hashlib.sha256(b"t").hexdigest(), "applications": ["*"], "can_deploy": True}]}
    data.update(extra)
    return data


class ProductionGitHub(FakeGitHub):
    def __init__(self):
        self.on_main, self.environments, self.variables, self.platform_prs = {}, {}, {}, []

    def contents(self, repository, ref, path):
        if path.endswith(".terraform.lock.hcl"):
            return {"sha": "lock", "text": "# lock\n"}
        return {"sha": "x", "text": self.on_main[path]} if path in self.on_main else None

    def ensure_protected_environment(self, repository, name, branch, reviewers):
        self.environments[(repository, name)] = {"branch": branch, "reviewers": list(reviewers)}

    def set_environment_variables(self, repository, name, variables):
        self.variables[(repository, name)] = dict(variables)


def controller(tmp_path, **extra):
    config = LiveConfig(Config.model_validate(settings(tmp_path, production=PRODUCTION, **extra)))
    github = ProductionGitHub()
    c = Controller(config, github, Store(config.current.database))
    return c, config.current.principals[0], github


# ---- 16.1 the configuration: production's project, its reviewers, each application's production
def test_production_settings_are_a_project_of_their_own_with_github_reviewers_and_a_rising_rollout(tmp_path):
    cfg = Config.model_validate(settings(tmp_path, production=PRODUCTION))
    assert cfg.production.rollout == [10, 50, 100] and cfg.production.bake_seconds == 300
    for bad, message in (({**PRODUCTION, "project_id": "test-project"}, "never the staging project"),
                         ({**PRODUCTION, "reviewers": ["not a login!"]}, "GitHub user names"),
                         ({**PRODUCTION, "reviewers": []}, "at least 1"),
                         ({**PRODUCTION, "rollout": [50, 10, 100]}, "rising list"),
                         ({**PRODUCTION, "rollout": [10, 50]}, "ends at 100")):
        with pytest.raises(ValueError, match=message):
            Config.model_validate(settings(tmp_path, production=bad))


def test_an_applications_production_lives_in_another_project_in_production_service(tmp_path):
    production = {"environment": "production-demo", "target": {**TARGET, "project_id": "lab-deploy-prod-abcd",
                  "deploy_service_account": "demo-deploy@lab-deploy-prod-abcd.iam.gserviceaccount.com",
                  "probe_service_account": "demo-probe@lab-deploy-prod-abcd.iam.gserviceaccount.com"}}
    app = {"repository": "lab/demo", "service": "demo", "target": TARGET, "production": production}
    assert Config.model_validate(settings(tmp_path, applications={"demo": app})).applications["demo"].production.environment == "production-demo"
    with pytest.raises(ValueError, match="production-demo"):
        Config.model_validate(settings(tmp_path, applications={"demo": {**app, "production": {**production, "environment": "production"}}}))
    with pytest.raises(ValueError, match="its own project"):
        Config.model_validate(settings(tmp_path, applications={"demo": {**app, "production": {**production, "target": TARGET}}}))


# ---- 16.1 the production root
def test_a_production_root_trusts_only_the_platform_repositorys_promote_workflow_in_its_environment():
    files = app_root("demo", project_id="lab-deploy-prod-abcd", state_bucket="lab-deploy-prod-abcd-tfstate", environment="production",
                     github_environment="production-demo", workflow_files=list(PRODUCTION_WORKFLOWS), pipeline="platform", **ROOT)
    assert 'prefix = "apps/demo/production"' in files["backend.tf"] and 'bucket = "lab-deploy-prod-abcd-tfstate"' in files["backend.tf"]
    main = files["main.tf"]
    assert 'prefix = "project/production"' in main and 'github_environment           = "production-demo"' in main
    assert 'workflow_files               = ["promote.yml"]' in main and 'project_id                   = "lab-deploy-prod-abcd"' in main
    assert 'output "github_environment" { value = "production-demo" }' in files["outputs.tf"]
    for wrong in ({"github_environment": "staging-demo"}, {"workflow_files": ["deploy.yml", "recover.yml", "rightsize.yml"]}, {"pipeline": "repository"}):
        arguments = dict(project_id="lab-deploy-prod-abcd", state_bucket="b-tfstate", environment="production", github_environment="production-demo",
                         workflow_files=list(PRODUCTION_WORKFLOWS), pipeline="platform", **ROOT)
        arguments.update(wrong)
        with pytest.raises(ValueError, match="promote.yml"):
            app_root("demo", **arguments)


def test_the_service_module_and_the_publisher_accept_production_and_nothing_looser():
    variables = Path("infra/gcp/modules/service/variables.tf").read_text()
    assert "production-[a-z][a-z0-9-]{1,19}[a-z0-9])$" in variables
    assert '(length(var.workflow_files) == 1 && join("", var.workflow_files) == "promote.yml")' in variables  # a list never equals a tuple literal
    assert 'environment_name = startswith(var.github_environment, "production-") ? "production" : "staging"' in Path("infra/gcp/modules/service/main.tf").read_text()
    assert PUBLISHED_ENVIRONMENT.fullmatch("production-demo") and PUBLISHED_ENVIRONMENT.fullmatch("staging-demo")
    assert not PUBLISHED_ENVIRONMENT.fullmatch("production") and not PUBLISHED_ENVIRONMENT.fullmatch("prod-demo")
    assert ROOT_LABEL.fullmatch("production-demo") and ROOT_LABEL.fullmatch("demo")
    assert not ROOT_LABEL.fullmatch("Production-demo") and not ROOT_LABEL.fullmatch("production-demo/../x")


def test_production_roots_have_their_own_folder_and_a_module_change_reaches_them_too(tmp_path):
    production = tmp_path / "infra" / "gcp" / "apps-production"
    for name in ("demo", "other"):
        (production / name).mkdir(parents=True)
    assert roots(["infra/gcp/apps-production/demo/main.tf", "infra/gcp/apps/demo/main.tf"], production) == ["demo"]
    assert roots(["infra/gcp/modules/service/main.tf"], production) == ["demo", "other"]
    assert roots(["infra/gcp/apps-production/demo/main.tf"], tmp_path / "infra" / "gcp" / "apps") == []


# ---- 16.1 add_production, one step at a time
def test_add_production_registers_proposes_the_root_then_makes_the_protected_environment(tmp_path):
    c, brian, gh = controller(tmp_path)
    first = c.add_production(brian, "demo")
    assert first["state"] == "infrastructure" and first["environment"] == "production-demo" and first["steps"]["registered"]["project_id"] == "lab-deploy-prod-abcd"
    app = c.config.applications["demo"]
    assert app.production.target.deploy_service_account == "demo-deploy@lab-deploy-prod-abcd.iam.gserviceaccount.com"
    assert app.production.target.wif_provider == "projects/987654321/locations/global/workloadIdentityPools/demo-gh/providers/github"
    pr = gh.platform_prs[-1]
    assert pr["branch"] == "lab-deploy/infra-demo-production" and pr["title"] == "Production infrastructure for demo"
    assert sorted(pr["files"]) == ["infra/gcp/apps-production/demo/.terraform.lock.hcl", "infra/gcp/apps-production/demo/backend.tf",
                                   "infra/gcp/apps-production/demo/main.tf", "infra/gcp/apps-production/demo/outputs.tf"]
    assert "never merged by the controller" in first["next_action"] and gh.environments == {}
    gh.on_main["infra/gcp/apps-production/demo/main.tf"] = pr["files"]["infra/gcp/apps-production/demo/main.tf"]  # a person merged it
    ready = c.add_production(brian, "demo")
    assert ready["state"] == "ready" and "registered" not in ready["steps"] and len(gh.platform_prs) == 1
    assert gh.environments[("lab/platform", "production-demo")] == {"branch": "main", "reviewers": ["brianboti"]}
    assert gh.variables[("lab/platform", "production-demo")]["GCP_PROJECT_ID"] == "lab-deploy-prod-abcd"
    assert {e["outcome"] for e in c.store.recent_events(10) if e["kind"] == "production"} == {"registered", "ready"}
    # the registration survives a restart, for an application of the file
    again = Controller(LiveConfig(Config.model_validate(settings(tmp_path, production=PRODUCTION))), gh, Store(c.config.database))
    assert again.config.applications["demo"].production.environment == "production-demo"


def test_add_production_needs_the_production_block_and_staging_roots_stay_where_they_are(tmp_path):
    config = LiveConfig(Config.model_validate(settings(tmp_path)))
    c = Controller(config, ProductionGitHub(), Store(config.current.database))
    with pytest.raises(ValueError, match="Set production in the controller configuration"):
        c.add_production(config.current.principals[0], "demo")
    rendered = c.render_infrastructure(config.current.principals[0], "demo")
    assert rendered["directory"] == "infra/gcp/apps/demo" and rendered["environment"] == "staging"
    with pytest.raises(ValueError, match="no production yet"):
        c.render_infrastructure(config.current.principals[0], "demo", environment="production")


def test_the_protected_environment_names_its_reviewers_by_id_and_refuses_self_review(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-credential")
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path == "/users/brianboti":
            return httpx.Response(200, json={"id": 207480197, "login": "brianboti"})
        if request.url.path.endswith("/deployment-branch-policies"):
            return httpx.Response(303, json={})  # the branch policy is there already
        return httpx.Response(200, json={})
    gh = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    gh.ensure_protected_environment("lab/platform", "production-demo", "main", ["brianboti"])
    put = next(body for method, path, body in seen if method == "PUT")
    assert put == {"wait_timer": 0, "prevent_self_review": True, "reviewers": [{"type": "User", "id": 207480197}],
                   "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}


# ---- 16.1 the foundation and the pipeline for production
def test_the_foundation_names_its_environment_and_lets_the_machine_view_production():
    module = Path("infra/gcp/modules/project/main.tf").read_text()
    assert 'display_name    = "${var.project_id} ${var.environment} monthly budget"' in module
    assert 'resource "google_project_iam_member" "outside_viewers"' in module and "setproduct(var.viewer_members, local.observer_roles)" in module
    root = Path("infra/gcp/project/variables.tf").read_text()
    assert 'contains(["staging", "production"], var.environment)' in root and "viewer_members" in root


def test_the_foundation_script_keeps_staging_values_out_of_production(tmp_path):
    script = Path("scripts/apply-foundation.sh").read_text()
    assert 'Rename infra/gcp/project/terraform.tfvars to staging.tfvars first' in script
    assert '-var-file="$VARS" -var="environment=$ENVIRONMENT"' in script and 'prefix = "INFRA_PRODUCTION_" if os.environ["ENVIRONMENT"] == "production"' in script
    if shutil.which("zsh"):
        work = tmp_path / "repo"
        (work / "scripts").mkdir(parents=True)
        (work / "infra" / "gcp" / "project").mkdir(parents=True)
        shutil.copy("scripts/apply-foundation.sh", work / "scripts" / "apply-foundation.sh")
        (work / "infra" / "gcp" / "project" / "terraform.tfvars").write_text('project_id = "test-project"\n')
        result = subprocess.run(["zsh", str(work / "scripts" / "apply-foundation.sh"), "lab-deploy-prod-abcd", "production"], capture_output=True, text=True)
        assert result.returncode == 1 and "Rename infra/gcp/project/terraform.tfvars to staging.tfvars first" in result.stdout
        (work / "infra" / "gcp" / "project" / "terraform.tfvars").rename(work / "infra" / "gcp" / "project" / "staging.tfvars")
        result = subprocess.run(["zsh", str(work / "scripts" / "apply-foundation.sh"), "lab-deploy-prod-abcd", "production"], capture_output=True, text=True)
        assert result.returncode == 1 and "production.tfvars is missing" in result.stdout


def test_the_pipeline_plans_applies_and_watches_production_roots_with_production_identities():
    plan = yaml.safe_load(Path(".github/workflows/infra-plan.yml").read_text())
    apply = yaml.safe_load(Path(".github/workflows/infra-apply.yml").read_text())
    drift = yaml.safe_load(Path(".github/workflows/infra-drift.yml").read_text())
    for workflow, job in ((plan, "plan-production"), (apply, "apply-production"), (drift, "plan-production")):
        text = json.dumps(workflow["jobs"][job])
        assert "INFRA_PRODUCTION_" in text and "infra/gcp/apps-production/$APP" in text and "needs.roots.outputs.production" in text
        assert "vars.INFRA_PLAN_WIF_PROVIDER" not in text and "vars.INFRA_APPLY_WIF_PROVIDER" not in text
    assert apply["jobs"]["apply-production"]["environment"] == "infra-production" and apply["jobs"]["apply"]["environment"] == "infra"
    assert "lab-deploy-infra:production-$APP" in json.dumps(plan["jobs"]["plan-production"])
    assert drift["jobs"]["report"]["needs"] == ["roots", "plan", "plan-production"]
    ci = Path(".github/workflows/ci.yml").read_text()
    assert ci.count("for root in project apps/*/ apps-production/*/; do") == 2 and ci.count("shopt -s nullglob") == 2
    assert re.search(r"infra/gcp/apps-production/\*\*", Path(".github/workflows/infra-plan.yml").read_text())


def test_the_chat_says_which_of_the_two_steps_production_is_at():
    from labdeploy import chat
    step = chat.production({"application": "demo", "environment": "production-demo", "project_id": "lab-deploy-prod-abcd", "state": "infrastructure",
                            "pull_request": {"number": 9, "url": "https://github.com/lab/platform/pull/9"}})
    assert step.startswith("**Production for `demo`, step 1 of 2") and "[#9](https://github.com/lab/platform/pull/9)" in step
    assert "never merged by the controller" in step and "infra-production" in step
    ready = chat.production({"application": "demo", "environment": "production-demo", "project_id": "lab-deploy-prod-abcd", "state": "ready",
                             "reviewers": ["brianboti"]})
    assert ready.startswith("**`demo` has its production**") and "`brianboti`" in ready and "16.2" in ready
