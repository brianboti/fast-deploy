"""Phase 10: one repository, several services (docs/ROADMAP.md D23, D24). A service's folder, the names a further service
of the same repository gets, the build context, Postgres by password and Compose drafts."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from conftest import TARGET, FakeGitHub
from test_onboarding import DOCKERFILE, PLATFORM, OnboardingGitHub, make

from labdeploy.analyzer import analyze, compose_drafts
from labdeploy.cli import decommission_steps
from labdeploy.config import Application, Config
from labdeploy.controller import Controller, Store
from labdeploy.infra import app_root, publish
from labdeploy.models import AppSpec, needs_env, needs_secrets, parse_spec, spec_hash
from labdeploy.onboarding import normalize_path
from labdeploy.provenance import ProvenanceError, workflow_path
from labdeploy.release import CloudRun
from labdeploy.workflows import TEMPLATES, lay_out, render

PLATFORM_SHA = "e" * 40
ADMIN = {**TARGET, "service": "admin", "registry": "admin", "deploy_service_account": "admin-deploy@test-project.iam.gserviceaccount.com",
         "probe_service_account": "admin-probe@test-project.iam.gserviceaccount.com",
         "wif_provider": "projects/123456789/locations/global/workloadIdentityPools/admin-gh/providers/github",
         "recovery_bucket": "test-project-admin-recovery"}
ROOT = dict(project_id="test-project", region="us-central1", state_bucket="test-project-tfstate", github_repository="lab/mono",
            github_repository_id="123", github_owner_id="45", bootstrap_image="us-docker.pkg.dev/cloudrun/container/hello@sha256:" + "a" * 64)


def admin(**extra):
    return Application.model_validate({"repository": "lab/mono", "service": "admin", "path": "services/admin", "workflow": "deploy-gcp-admin.yml",
                                       "environment": "staging-admin", "target": ADMIN, **extra})


# ---- 10.1 and 10.2: the registry's layout
def test_a_service_names_its_folder_and_a_further_service_of_the_repository_gets_suffixed_names():
    first = Application.model_validate({"repository": "lab/mono", "service": "demo", "path": "services/api", "target": TARGET})
    assert first.suffix == "" and first.workflows == {"deploy": "deploy-gcp.yml", "recover": "recover-gcp.yml", "rightsize": "rightsize-gcp.yml"}
    assert first.repo_path("platform.yaml") == "services/api/platform.yaml"
    second = admin()
    assert second.suffix == "-admin" and second.workflows["recover"] == "recover-gcp-admin.yml" and second.environment == "staging-admin"
    root = Application.model_validate({"repository": "lab/demo", "service": "demo", "target": TARGET})
    assert root.path == "" and root.repo_path("platform.yaml") == "platform.yaml"
    bad = ({"path": "../x"}, {"path": "/abs"}, {"path": ".hidden"}, {"path": "a/../b"},
           {"environment": "staging"}, {"workflow": "deploy-gcp.yml"}, {"workflow": "deploy-gcp-other.yml", "environment": "staging-other"})
    for change in bad:
        with pytest.raises(ValueError):
            admin(**change)


def test_a_folder_given_in_a_sentence_is_normalised_and_never_leaves_the_repository():
    assert normalize_path(None) == "" and normalize_path("./") == "" and normalize_path("/services/api/") == "services/api"
    assert normalize_path(r"services\admin") == "services/admin" and normalize_path("./backend") == "backend"
    for bad in ("../secrets", "services/../../x", ".github", "a b"):
        with pytest.raises(ValueError):
            normalize_path(bad)


def test_the_templates_render_one_set_of_workflows_per_service():
    texts = {name: render(Path("templates", name).read_text(), "lab/platform", PLATFORM_SHA, admin()) for name in TEMPLATES}
    assert not any("__LAB_" in text or "PLATFORM_COMMIT" in text for text in texts.values())
    deploy, recover, rightsize = (yaml.safe_load(texts[name]) for name in TEMPLATES)
    assert deploy["name"] == "Deploy GCP staging (admin)" and deploy["concurrency"]["group"] == "gcp-staging-admin"
    assert deploy["jobs"]["deploy"]["environment"]["name"] == "staging-admin"
    assert deploy["env"]["APP_DIR"] == "app/services/admin" and deploy["env"]["APP_PATH"] == "services/admin"
    assert deploy["env"]["DEPLOY_WORKFLOW"] == "deploy-gcp-admin.yml"
    assert {step.get("working-directory") for step in deploy["jobs"]["build"]["steps"]} >= {"app/services/admin"}
    # The recovery follows this service's releases only, in the same queue, in the same environment.
    assert recover[True]["workflow_run"]["workflows"] == ["Deploy GCP staging (admin)"]
    assert recover["concurrency"]["group"] == "gcp-staging-admin" and recover["jobs"]["recover"]["environment"] == "staging-admin"
    assert rightsize["concurrency"]["group"] == "gcp-staging-rightsize-admin" and rightsize["jobs"]["rightsize"]["environment"] == "staging-admin"
    assert rightsize["jobs"]["rightsize"]["env"]["APP_PATH"] == "services/admin"
    # The first service of a repository at its root gets exactly what every application had before 1.10.0.
    plain = yaml.safe_load(lay_out(Path("templates/deploy-gcp.yml").read_text()))
    assert plain["name"] == "Deploy GCP staging" and plain["concurrency"]["group"] == "gcp-staging"
    assert plain["env"]["APP_DIR"] == "app" and plain["env"]["APP_PATH"] == "." and plain["env"]["DEPLOY_WORKFLOW"] == "deploy-gcp.yml"


def test_the_build_reads_the_service_folder_or_the_whole_repository():
    steps = yaml.safe_load(lay_out(Path("templates/deploy-gcp.yml").read_text()))["jobs"]["build"]["steps"]
    run = next(step["run"] for step in steps if step.get("name", "").startswith("Build"))
    tests = next(step for step in steps if step.get("name") == "Run unit tests")
    # Nothing the tests leave behind reaches the image: the virtualenv lives outside the checkout, and the checkout is
    # cleaned back to the commit before the build.
    assert ".test-venv" not in tests["run"] and '"$RUNNER_TEMP/test-venv/bin/python" -m pytest' in tests["run"]
    assert run.index("git -C app clean -ffdxq") < run.index("docker build")
    assert tests["env"]["CONTEXT"] == "${{ steps.policy.outputs.context }}" and 'export PYTHONPATH="$GITHUB_WORKSPACE/app"' in tests["run"]
    assert 'if [ "$CONTEXT" = "repository" ]' in run
    assert '-f "$APP_DIR/Dockerfile" -t "lab-app:$COMMIT_SHA" app' in run and '-t "lab-app:$COMMIT_SHA" "$APP_DIR"' in run
    spec = AppSpec.model_validate({"name": "admin", "build": {"context": "repository"}})
    assert spec.build.context == "repository"
    with pytest.raises(ValueError):
        AppSpec.model_validate({"name": "admin", "build": {"context": "parent"}})
    # A spec that says nothing about the build or the database sign-in hashes as it did before 1.10.0: no plan is invalidated.
    text = "api_version: lab/v1\nname: demo\nreviewed: true\n"
    assert spec_hash(parse_spec(text)) == spec_hash(parse_spec(text + "build: {context: service}\n"))


def test_provenance_names_the_service_s_own_workflow():
    assert workflow_path(None) == ".github/workflows/deploy-gcp.yml"
    assert workflow_path("deploy-gcp-admin.yml") == ".github/workflows/deploy-gcp-admin.yml"
    for bad in ("recover-gcp.yml", "deploy-gcp-../x.yml", "deploy-gcp-A.yml"):
        with pytest.raises(ProvenanceError):
            workflow_path(bad)


def test_a_plan_binds_the_layout_and_a_changed_layout_refuses_its_dispatch(tmp_path):
    raw = {"live_dispatch": True, "database": str(tmp_path / "plans.db"),
           "applications": {"admin": admin().model_dump()},
           "principals": [{"name": "m", "token_sha256": hashlib.sha256(b"t").hexdigest(), "applications": ["admin"], "can_deploy": True}]}
    config = Config.model_validate(raw)
    github = FakeGitHub()
    github.spec = {"name": "admin", "reviewed": True}
    controller = Controller(config, github, Store(config.database))
    plan = controller.plan(config.principals[0], "admin")
    assert plan["layout"] == {"path": "services/admin", "workflow": "deploy-gcp-admin.yml", "github_environment": "staging-admin"}
    moved = {**raw, "applications": {"admin": admin(path="apps/admin").model_dump()}}
    controller = Controller(Config.model_validate(moved), github, Store(config.database))
    with pytest.raises(ValueError, match="folder or workflow names changed"):
        controller.deploy(config.principals[0], plan["id"])


# ---- auto-deploy in a shared repository
def test_auto_deploy_skips_a_service_whose_folder_did_not_change(tmp_path):
    raw = {"live_dispatch": True, "database": str(tmp_path / "plans.db"),
           "applications": {"admin": admin(auto_deploy=True).model_dump()},
           "principals": [{"name": "m", "token_sha256": hashlib.sha256(b"t").hexdigest(), "applications": ["admin"], "can_deploy": True}]}
    config = Config.model_validate(raw)
    github = FakeGitHub()
    github.spec = {"name": "admin", "reviewed": True}
    seen = []

    def changed(repository, base, head, files=("services/api/app.py", "README.md"), complete=True):
        seen.append((repository, base, head))
        return list(files), complete
    github.changed_files = changed
    controller = Controller(config, github, Store(config.database))
    controller.store.latest_success = lambda app_id: {"sha": "b" * 40}
    result = controller.auto_deploy_tick()
    assert result["admin"].startswith("up to date: nothing under services/admin changed") and github.calls == 0
    assert seen == [("lab/mono", "b" * 40, "a" * 40)]
    app = config.applications["admin"]
    github.changed_files = lambda *a: (["services/admin/main.py"], True)
    assert controller.touched_since(app, "b" * 40, "a" * 40)
    github.changed_files = lambda *a: ([".github/workflows/deploy-gcp-admin.yml"], True)
    assert controller.touched_since(app, "b" * 40, "a" * 40)
    github.changed_files = lambda *a: (["services/api/main.py"], False)  # an incomplete list: assume it did
    assert controller.touched_since(app, "b" * 40, "a" * 40)
    github.changed_files = lambda *a: pytest.fail("a whole-repository build reads every file")
    github.spec = {"name": "admin", "reviewed": True, "build": {"context": "repository"}}
    assert controller.touched_since(app, "b" * 40, "a" * 40)


# ---- 10.6: Postgres by password
def test_database_auth_password_is_postgres_only_and_reaches_the_app_as_a_secret(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="postgres only"):
        AppSpec.model_validate({"name": "admin", "needs": {"database": "firestore", "database_auth": "password"}})
    spec = AppSpec.model_validate({"name": "admin", "needs": {"database": "postgres", "database_auth": "password"}})
    assert needs_secrets(spec) == {"DATABASE_URL": "admin-DATABASE_URL"} and needs_env(spec, "p")["DATABASE_USER"] == "app"
    assert needs_secrets(AppSpec.model_validate({"name": "admin", "needs": {"database": "postgres"}})) == {}
    with pytest.raises(ValueError):  # the platform owns the name
        AppSpec.model_validate({"name": "admin", "secrets": ["DATABASE_URL"], "needs": {"database": "postgres", "database_auth": "password"}})
    monkeypatch.chdir(tmp_path)
    commands = []

    def runner(command):
        commands.append(command)
        return "" if "versions" in command else "found"
    adapter = CloudRun("test-project", "us-central1", "admin", "admin-probe@test-project.iam.gserviceaccount.com", runner=runner)
    flags = adapter.configuration(spec.model_copy(update={"secrets": ["API_KEY"]}))
    assert "--set-secrets=API_KEY=admin-API_KEY:latest,DATABASE_URL=admin-DATABASE_URL:latest" in flags
    assert json.loads((tmp_path / "revision-env.yaml").read_text())["DATABASE_USER"] == "app"
    with pytest.raises(ValueError, match=r"secret admin-DATABASE_URL \(no enabled version\)"):
        adapter.require_needs(spec)  # the pipeline writes the only version; without it the candidate could not start


def test_a_password_root_generates_the_password_in_every_run_and_never_stores_it():
    files = app_root("admin", needs={"database": "postgres", "database_auth": "password"}, github_environment="staging-admin",
                     workflow_files=("deploy-gcp-admin.yml", "recover-gcp-admin.yml", "rightsize-gcp-admin.yml"), **ROOT)
    main = files["main.tf"]
    assert 'ephemeral "random_password" "database"' in main and "= ephemeral.random_password.database.result" in main
    assert 'database = "postgres", migrate = [], database_auth = "password" }' in main
    assert 'github_environment           = "staging-admin"' in main
    assert '"deploy-gcp-admin.yml", "recover-gcp-admin.yml", "rightsize-gcp-admin.yml"' in main
    assert 'source  = "hashicorp/random"' in files["backend.tf"]
    assert 'output "github_environment" { value = "staging-admin" }' in files["outputs.tf"]
    plain = app_root("demo", **{**ROOT, "github_repository": "lab/demo"})
    assert "random" not in plain["backend.tf"] and "ephemeral" not in plain["main.tf"]
    assert "github_environment " not in plain["main.tf"] and 'output "github_environment" ' not in plain["outputs.tf"]
    with pytest.raises(ValueError, match="staging-<service>"):
        app_root("admin", github_environment="staging-other", workflow_files=("deploy-gcp-other.yml", "recover-gcp-other.yml", "rightsize-gcp-other.yml"), **ROOT)
    with pytest.raises(ValueError, match="postgres only"):
        app_root("admin", needs={"database": "none", "database_auth": "password"}, **ROOT)


def test_the_module_trusts_only_the_service_s_own_workflows_in_its_own_environment():
    module = Path("infra/gcp/modules/service/main.tf").read_text()
    assert "assertion.environment == '${var.github_environment}'" in module
    assert "assertion.workflow_ref in [" in module and "@refs/heads/main'" in module
    needs = Path("infra/gcp/modules/service/needs.tf").read_text()
    assert "password_wo         = var.database_password" in needs and "secret_data_wo         =" in needs
    assert "replace_triggered_by = [google_sql_user.password]" in needs  # a user created again and its URL always match
    variables = Path("infra/gcp/modules/service/variables.tf").read_text()
    assert 'variable "database_password"' in variables and "ephemeral   = true" in variables


def test_the_pipeline_publishes_a_further_service_s_variables_to_its_own_environment():
    outputs = {"github_repository": {"value": "lab/mono"}, "github_environment": {"value": "staging-admin"},
               "github_environment_variables": {"value": {"GCP_PROJECT_ID": "p"}}}
    calls = []
    text, ok = publish(outputs, token="t", request=lambda method, url, token, body: (calls.append(url), (204, None))[1])
    assert ok and "environment `staging-admin`" in text and calls == ["https://api.github.com/repos/lab/mono/environments/staging-admin/variables/GCP_PROJECT_ID"]
    with pytest.raises(ValueError):
        publish({**outputs, "github_environment": {"value": "production"}}, token="t", request=lambda *a: (204, None))


# ---- 10.4 and 10.5: the analyzer's component rule and Compose drafts
COMPOSE = """
services:
  api:
    build: ./services/api
    ports: ["8000:8000"]
    depends_on: [db, cache]
  admin:
    build:
      context: .
      dockerfile: services/admin/Dockerfile
    expose: ["8080"]
  worker:
    build: ./services/api
    command: celery -A tasks worker
  db:
    image: postgres:17
    volumes: [data:/var/lib/postgresql/data]
  cache:
    image: redis:7
volumes:
  data: {}
"""


def test_a_compose_file_becomes_drafts_and_advice_never_a_deployment():
    result = compose_drafts(COMPOSE)
    parts = {part["service"]: part for part in result["parts"]}
    assert parts["api"]["kind"] == "http" and parts["api"]["path"] == "services/api" and parts["api"]["draft_spec"]["port"] == 8000
    assert parts["api"]["draft_spec"]["reviewed"] is False and parts["api"]["draft_spec"]["build"] == {"context": "service"}
    assert parts["admin"]["path"] == "services/admin" and parts["admin"]["draft_spec"]["build"] == {"context": "repository"}
    assert parts["worker"]["kind"] == "worker" and "Pub/Sub" in parts["worker"]["advice"]
    assert parts["db"]["kind"] == "database" and parts["cache"]["kind"] == "cache"
    assert "never deployed" in result["note"]
    assert "error" in compose_drafts("services: [") and "error" in compose_drafts("version: '3'")


def test_the_analyzer_names_caches_workers_front_ends_and_url_drivers():
    result = analyze({"requirements.txt": "fastapi==0.115\nredis==6.0\ncelery==5.5\nasyncpg==0.30\n", "package.json": "{}",
                      "compose": COMPOSE}, "api", ["package.json", "requirements.txt"])
    text = json.dumps(result)
    assert "asyncpg without the Cloud SQL connector" in text and "database_auth: password" in text
    assert "celery appears in dependencies: a background worker" in result["evidence"]
    assert result["compose"]["parts"][0]["service"] == "api"


# ---- the onboarding of a monorepo, one sentence per service
class MonorepoGitHub(OnboardingGitHub):
    def __init__(self):
        super().__init__()
        spec = "api_version: lab/v1\nname: {name}\nport: 8080\nhealth_path: /health\nreviewed: true\n{extra}"
        files = {"lib/shared.py": "X = 1\n", "compose.yaml": COMPOSE}
        for name, extra in (("api", ""), ("admin", "build: {context: repository}\n")):
            files.update({f"services/{name}/Dockerfile": DOCKERFILE, f"services/{name}/platform.yaml": spec.format(name=name, extra=extra),
                          f"services/{name}/requirements.txt": "fastapi==0.1\n", f"services/{name}/requirements-dev.txt": "pytest\n",
                          f"services/{name}/tests/test_app.py": "def test(): pass\n"})
        self.repos["lab/mono"] = {"files": files, "default_branch": "main"}
        self.repos[PLATFORM]["files"].update({f"templates/{name}": Path("templates", name).read_text() for name in TEMPLATES})

    def repository(self, repository):
        canonical = next(name for name in self.repos if name.lower() == repository.lower())  # GitHub ignores case
        return {**super().repository(canonical), "full_name": canonical}

    def tree(self, repository, ref):
        paths = set(self.repos[repository]["files"])
        return sorted(paths | {"/".join(p.split("/")[:i]) for p in paths for i in range(1, p.count("/") + 1)})


def test_each_service_of_a_monorepo_is_onboarded_by_its_own_sentence(tmp_path):
    controller, brian, _ = make(tmp_path)
    gh = controller.github = MonorepoGitHub()
    first = controller.onboard(brian, "https://github.com/lab/mono", path="services/api")
    assert first["application"] == "api" and first["path"] == "services/api" and first["state"] == "planning"
    api = controller.config.applications["api"]
    assert (api.path, api.workflow, api.environment) == ("services/api", "deploy-gcp.yml", "staging")
    second = controller.onboard(brian, "lab/Mono", path="/services/admin/")  # GitHub's names ignore case
    assert second["application"] == "admin" and second["state"] == "planning"
    app = controller.config.applications["admin"]
    assert (app.repository, app.path, app.workflow, app.environment) == ("lab/mono", "services/admin", "deploy-gcp-admin.yml", "staging-admin")
    number = second["steps"]["infrastructure"]["pull_request"]
    root = gh.prs[(PLATFORM, number)]["files"]
    assert 'github_environment           = "staging-admin"' in root["infra/gcp/apps/admin/main.tf"]
    assert 'output "github_environment" { value = "staging-admin" }' in root["infra/gcp/apps/admin/outputs.tf"]
    # Through the plan, the apply, the environment and the workflows, as for any application.
    gh.runs[(PLATFORM, "infra-plan.yml", "d" * 40)] = [{"id": 1, "status": "completed", "conclusion": "success", "url": "u1", "head_sha": "d" * 40}]
    gh.comments[(PLATFORM, number)] = ["<!-- lab-deploy-infra:admin -->\n**Policy: passed.** Merging applies exactly this plan."]
    assert controller.advance_onboarding("admin")["state"] == "applying"
    gh.runs[(PLATFORM, "infra-apply.yml", "9" * 40)] = [{"id": 2, "status": "completed", "conclusion": "success", "url": "u2", "head_sha": "9" * 40}]
    status = controller.advance_onboarding("admin")
    assert status["state"] == "merging_workflows" and ("lab/mono", "staging-admin") in gh.variables and ("lab/mono", "staging") not in gh.variables
    pr = max(number for repository, number in gh.prs if repository == "lab/mono")
    written = gh.prs[("lab/mono", pr)]["files"]
    assert set(written) == {".github/workflows/deploy-gcp-admin.yml", ".github/workflows/recover-gcp-admin.yml", ".github/workflows/rightsize-gcp-admin.yml"}
    assert "APP_DIR: app/services/admin" in written[".github/workflows/deploy-gcp-admin.yml"]
    gh.prs[("lab/mono", pr)]["mergeable_state"] = "clean"
    done = controller.advance_onboarding("admin")
    assert done["state"] == "complete" and done["plan_id"]
    # The same folder cannot be onboarded twice under another id, and a missing folder blocks with a plain reason.
    with pytest.raises(ValueError, match="being onboarded from lab/mono at services/admin"):
        controller.onboard(brian, "lab/mono", "admin", path="services/other")
    missing = controller.onboard(brian, "lab/mono", "ghost", path="services/ghost")
    assert missing["state"] == "blocked" and "no folder services/ghost" in missing["error"]


def test_retiring_one_service_keeps_the_repository_s_access_for_the_others():
    steps = decommission_steps("admin", admin(), siblings=["api"])
    assert "deploy-gcp-admin.yml, recover-gcp-admin.yml, rightsize-gcp-admin.yml and the staging-admin environment" in steps[-1]
    assert "api still deploy from it" in steps[-1] and "revoke" not in steps[-1]
    alone = decommission_steps("demo", Application.model_validate({"repository": "lab/demo", "service": "demo", "target": TARGET}))
    assert "revoke the platform credential's access" in alone[-1] and "the staging environment" in alone[-1]


def test_the_command_line_renders_the_plain_layout(tmp_path, monkeypatch):
    from labdeploy.cli import main
    output = tmp_path / "deploy.yml"
    monkeypatch.setattr("sys.argv", ["lab-deploy", "render-workflow", "--platform-repo", "lab/platform", "--platform-sha", PLATFORM_SHA, "--output", str(output)])
    main()
    text = output.read_text()
    assert "__LAB_" not in text and f"ref: {PLATFORM_SHA}" in text and "APP_DIR: app\n" in text


def test_the_onboarding_environment_setting_is_the_platform_s_name():
    base = {"project_id": "test-project", "project_number": "123456789"}
    from labdeploy.config import Onboarding
    assert Onboarding.model_validate(base).environment == "staging"
    with pytest.raises(ValueError):
        Onboarding.model_validate({**base, "environment": "production"})


def test_spec_files_are_read_at_the_service_folder(tmp_path):
    raw = {"live_dispatch": True, "database": str(tmp_path / "plans.db"), "applications": {"admin": admin().model_dump()},
           "principals": [{"name": "m", "token_sha256": hashlib.sha256(b"t").hexdigest(), "applications": ["admin"], "can_deploy": True}]}
    config = Config.model_validate(raw)
    github = FakeGitHub()
    asked = []
    github.file = lambda app, sha, filename: (asked.append(filename), yaml.safe_dump(copy.deepcopy({"name": "admin", "reviewed": True})))[1]
    controller = Controller(config, github, Store(config.database))
    controller.spec_at(config.applications["admin"], "a" * 40)
    assert asked == ["services/admin/platform.yaml"]
