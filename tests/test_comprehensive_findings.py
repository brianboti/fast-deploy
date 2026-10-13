"""Regressions for what the comprehensive test with the Lab Notebook app found (2026-10-13)."""

import hashlib
import re
from pathlib import Path

import pytest

from labdeploy.analyzer import analyze, analyze_local
from labdeploy.demo import LocalRepository, demo_config, service_name
from labdeploy.models import AppSpec, enforce

REQUIREMENTS = "fastapi==0.143.0\nsqlalchemy==2.1.4\npg8000==1.31.5\nalembic==1.20.0\ngoogle-cloud-storage==3.17.0\ngoogle-cloud-pubsub==2.42.0\n"
LOCKED_DOCKERFILE = "FROM python:3.12-slim\nCOPY requirements.lock .\nRUN pip install --require-hashes -r requirements.lock\nEXPOSE 8080\n"


def test_start_script_assigns_no_name_zsh_reserves():
    # zsh makes `status` (and a few others) read-only; assigning one aborts a `set -e` script at the worst moment.
    for script in Path("scripts").glob("*.sh"):
        assert not re.search(r"^\s*(status|pipestatus|argv|path)=", script.read_text(), re.M), script


def test_analyzer_sees_a_lock_file_installed_with_hashes():
    result = analyze({"requirements.txt": REQUIREMENTS, "Dockerfile": LOCKED_DOCKERFILE}, "notebook", ["requirements.txt", "requirements.lock", "Dockerfile"])
    assert "requirements.lock is installed with --require-hashes: builds are reproducible" in result["evidence"]
    assert not any("not reproducible" in line for line in result["evidence"])
    unused = analyze({"requirements.txt": REQUIREMENTS, "Dockerfile": "FROM python\n"}, "notebook", ["requirements.lock"])
    assert any("does not install it with --require-hashes" in line for line in unused["evidence"])
    unknown = analyze({"requirements.txt": REQUIREMENTS}, "notebook")
    assert any("not reproducible" in line for line in unknown["evidence"])


def test_analyzer_asks_for_tests_only_when_there_are_none():
    with_tests = analyze({"requirements.txt": REQUIREMENTS}, "notebook", ["tests/test_api.py", "tests/conftest.py", "app/main.py"])
    assert not any("Add pytest tests" in line for line in with_tests["review_required"])
    assert "2 test files found; they run before every build" in with_tests["evidence"]
    without = analyze({"requirements.txt": REQUIREMENTS}, "notebook", ["app/main.py"])
    assert any("Add pytest tests" in line for line in without["review_required"])


def test_analyzer_suggests_needs_without_adding_them_and_keeps_the_draft_releasable():
    result = analyze({"requirements.txt": REQUIREMENTS + "psycopg==3.3.0\n"}, "notebook", [])
    assert result["suggested_needs"][:1] == ["database: postgres"] and len(result["suggested_needs"]) == 4
    assert any("nothing was added" in line for line in result["review_required"])
    draft = AppSpec.model_validate({**result["draft_spec"], "reviewed": True})
    assert draft.needs.database == "none" and draft.dependencies == []
    enforce(draft)  # a database library alone no longer makes the draft unreleasable


def test_unprovided_services_are_named_plainly():
    result = analyze({"requirements.txt": "flask==3.1.0\nredis==6.0.0\n"}, "notebook", [])
    assert result["draft_spec"]["dependencies"] == ["redis"]
    assert "redis appears in dependencies: the platform does not provide it" in result["evidence"]
    with pytest.raises(ValueError, match="does not provide redis; declare databases, buckets and topics under needs"):
        enforce(AppSpec.model_validate({**result["draft_spec"], "reviewed": True}))
    with pytest.raises(ValueError, match="GPUs are not supported"):
        enforce(AppSpec.model_validate({"name": "notebook", "reviewed": True, "gpu": True}))


def test_analyze_local_lists_files_but_skips_virtualenvs(tmp_path):
    (tmp_path / "requirements.txt").write_text(REQUIREMENTS)
    (tmp_path / "Dockerfile").write_text(LOCKED_DOCKERFILE)
    (tmp_path / "requirements.lock").write_text("x==1 --hash=sha256:" + "0" * 64 + "\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test(): pass\n")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "test_site.py").write_text("")
    result = analyze_local(tmp_path, "notebook")
    assert "1 test file found; they run before every build" in result["evidence"]
    assert "requirements.lock is installed with --require-hashes: builds are reproducible" in result["evidence"]


def test_demo_mode_uses_the_application_s_own_name(tmp_path):
    (tmp_path / "platform.yaml").write_text("name: lab-notebook\nreviewed: true\n")
    assert service_name(tmp_path) == "lab-notebook"
    assert service_name(tmp_path / "missing") == "lab-demo"
    config = demo_config(str(tmp_path / "demo.db"), hashlib.sha256(b"demo").hexdigest(), "lab-notebook")
    app = config.applications["demo"]
    assert app.service == "lab-notebook" and app.target.service == "lab-notebook" and app.target.registry == "lab-notebook"
    assert "platform.yaml" in LocalRepository(tmp_path).tree("local/lab-notebook", "main")


def test_postgres_runtime_user_may_create_its_tables():
    needs = Path("infra/gcp/modules/service/needs.tf").read_text()
    user = needs[needs.index('resource "google_sql_user" "runtime"'):]
    user = user[:user.index("\n}")]
    assert 'type           = "CLOUD_IAM_SERVICE_ACCOUNT"' in user and 'database_roles = ["cloudsqlsuperuser"]' in user


def test_app_roots_plan_against_a_foundation_without_the_iap_output():
    from labdeploy.infra import app_root

    files = app_root("lab-notebook", project_id="p-12345", region="us-central1", state_bucket="p-12345-tfstate",
                     github_repository="o/lab-notebook", github_repository_id="1", github_owner_id="2",
                     bootstrap_image="us-docker.pkg.dev/cloudrun/container/hello",
                     needs={"buckets": [], "topics": [], "database": "postgres", "migrate": ["alembic", "upgrade", "head"]})
    main = files["main.tf"]
    assert re.search(r'iap_client_id\s+= try\(data\.terraform_remote_state\.project\.outputs\.iap_client_id, ""\)', main)
    assert re.search(r"network\s+= data\.terraform_remote_state\.project\.outputs\.network\n", main)


def test_every_role_the_service_module_grants_is_one_the_policy_allows():
    # The policy refused the module's own vulnerability-reader grant, so every new application was blocked;
    # existing ones never noticed because only changes are judged.
    from labdeploy.infra import ALLOWED_ROLES

    text = "".join(path.read_text() for path in Path("infra/gcp/modules/service").glob("*.tf"))
    roles = set(re.findall(r'"(roles/[A-Za-z.]+)"', text))
    assert roles and not roles - ALLOWED_ROLES, roles - ALLOWED_ROLES


def test_a_new_application_is_planned_against_an_empty_state():
    # The gcs backend creates a missing state (and its lock) even with -lock=false; the read-only plan identity may not.
    step = Path(".github/workflows/infra-plan.yml").read_text()
    step = step[step.index("- name: Plan without taking the state lock"):step.index("- name: Check the plan")]
    assert 'gcloud storage objects describe "gs://$bucket/$prefix/default.tfstate"' in step
    assert "backend_override.tf" in step and 'grep -qiE "not found|404|No URLs matched"' in step
    assert step.index("backend_override.tf") < step.index("terraform init") and "exit 1" in step
