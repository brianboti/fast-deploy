"""What the 1.12.0 live exercise found (docs/VALIDATION.md, 2026-10-15), fixed in 1.12.1."""
from conftest import FakeGitHub
from test_phase6 import config

from labdeploy.analyzer import compose_drafts
from labdeploy.controller import Controller, Store

# The live test repository's Compose file (brianboti/lab-monorepo): the admin service reads DATABASE_URL from a Postgres service.
LIVE_COMPOSE = """
services:
  api:
    build: ./services/api
    ports: ["8080:8080"]
  admin:
    build:
      context: .
      dockerfile: services/admin/Dockerfile
    ports: ["8081:8080"]
    environment:
      DATABASE_URL: postgresql://app:local@db:5432/app
    depends_on: [db]
  reports:
    build: ./services/reports
    ports: ["8082:8080"]
    environment: ["DATABASE_URL=postgresql://app:local@db:5432/app", "MODE=reports"]
    depends_on:
      db: {condition: service_healthy}
      cache: {condition: service_started}
  db:
    image: postgres:17
  cache:
    image: redis:7
"""


def test_a_compose_service_that_reads_database_url_from_postgres_is_drafted_with_postgres_by_password():
    parts = {part["service"]: part for part in compose_drafts(LIVE_COMPOSE)["parts"]}
    assert parts["api"]["draft_spec"]["needs"]["database"] == "none" and "needs_from" not in parts["api"]
    for name in ("admin", "reports"):  # the mapping and the list forms of environment and depends_on
        needs = parts[name]["draft_spec"]["needs"]
        assert needs["database"] == "postgres" and needs["database_auth"] == "password", name
        assert "DATABASE_URL" in parts[name]["needs_from"] and parts[name]["draft_spec"]["reviewed"] is False


def test_a_compose_service_that_depends_on_postgres_without_a_url_gets_iam_sign_in():
    text = "services:\n  api:\n    build: .\n    ports: ['8080:8080']\n    depends_on: [db]\n  db:\n    image: postgis/postgis:17-3.5\n"
    part = compose_drafts(text)["parts"][0]
    assert part["draft_spec"]["needs"]["database"] == "postgres" and part["draft_spec"]["needs"]["database_auth"] == "iam"


def test_the_infrastructure_pull_request_names_who_asked_and_through_what(tmp_path):
    cfg = config(tmp_path, platform={"repository": "lab/platform"})
    gh = FakeGitHub()
    gh.spec = {"name": "demo", "reviewed": True}
    c = Controller(cfg, gh, Store(cfg.database))
    p = cfg.principals[0]
    c.propose_infrastructure(p, "demo", through="the onboarding of `demo`")
    body = gh.platform_prs[-1]["body"]
    assert f"Requested by `{p.name}` through the onboarding of `demo`" in body and "not by Claude" in body
    c.propose_infrastructure(p, "demo")
    assert f"Requested by `{p.name}` (written by the controller" in gh.platform_prs[-1]["body"]


def test_an_open_draft_is_left_alone_and_the_answer_says_how_to_get_a_fresh_one(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
    import types

    import httpx

    from labdeploy.github import GitHub

    def handler(request):
        assert request.method == "GET" and request.url.path == "/repos/lab/bare/pulls"  # nothing is written to an open draft
        return httpx.Response(200, json=[{"number": 1, "html_url": "https://github.com/lab/bare/pull/1"}])
    github = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    app = types.SimpleNamespace(repository="lab/bare", branch="main")
    result = github.open_pull_request(app, "lab-deploy/contract-files", {"platform.yaml": "name: bare\n"}, "t", "b", "m", draft=True, create_only=True)
    assert result["number"] == 1 and result["changed"] == []
    assert "left as it is" in result["note"] and "close it to have the platform open a fresh one" in result["note"]


def test_the_pipeline_s_secret_writer_can_enable_and_replace_a_version_but_never_read_one():
    from pathlib import Path
    pipeline = Path("infra/gcp/modules/project/pipeline.tf").read_text()
    role = pipeline[pipeline.index('resource "google_project_iam_custom_role" "secret_writer"'):]
    role = role[:role.index("\n}\n")]
    for permission in ("versions.add", "versions.enable", "versions.disable", "versions.destroy"):
        assert f"secretmanager.{permission}" in role
    assert "versions.access" not in role.split("permissions")[1]  # the apply identity never reads a value back


def test_an_environment_whose_branch_policy_exists_is_accepted_as_github_answers_today(monkeypatch):
    # Sophia's copy onboarded again from backend/ (1.12.3): GitHub answered 303 See Other for the existing `main` policy.
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
    import httpx
    import pytest

    from labdeploy.github import GitHub

    for answer in (200, 303, 422):
        def handler(request, answer=answer):
            if request.method == "PUT":
                return httpx.Response(200, json={})
            assert request.url.path.endswith("/environments/staging/deployment-branch-policies")
            return httpx.Response(answer, json={}, headers={"location": "https://api.github.com/x"} if answer == 303 else {})
        GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))).ensure_environment("lab/app", "staging", "main")

    def refused(request):
        return httpx.Response(200 if request.method == "PUT" else 404, json={})
    with pytest.raises(httpx.HTTPStatusError):  # anything else still stops the step
        GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(refused))).ensure_environment("lab/app", "staging", "main")
