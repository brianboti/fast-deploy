"""Phase 1: regenerating app workflows against a platform commit, from Claude or from the operator's shell."""
import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient

from labdeploy.cli import update_workflows
from labdeploy.config import Config
from labdeploy.controller import Controller, Store
from labdeploy.github import GitHub
from labdeploy.server import build_app
from labdeploy.workflows import TEMPLATES, pinned_commit, render

OLD, NEW = "1" * 40, "2" * 40
PLATFORM = "lab/lab-deploy"


def test_render_and_pinned_commit_round_trip():
    template = Path("templates/deploy-gcp.yml").read_text()
    rendered = render(template, PLATFORM, NEW)
    assert "PLATFORM_COMMIT" not in rendered and "PLATFORM_REPOSITORY" not in rendered
    assert pinned_commit(rendered, PLATFORM) == NEW and pinned_commit(rendered, "other/repo") is None and pinned_commit("", PLATFORM) is None
    with pytest.raises(ValueError):
        render(template, PLATFORM, "short")


class PlatformAware(FakeGitHub):
    def __init__(self):
        self.pinned = OLD
        self.updates = []

    def contents(self, repository, ref, path):
        if repository == PLATFORM and path.startswith("templates/"):
            return {"sha": "t", "text": Path(path).read_text()}
        if path == ".github/workflows/deploy-gcp.yml":
            return None if self.pinned is None else {"sha": "w", "text": render(Path("templates/deploy-gcp.yml").read_text(), PLATFORM, self.pinned)}
        return None

    def branch_head(self, repository, branch="main"):
        return NEW

    def update_workflows(self, app, platform_sha, files):
        self.updates.append((app.repository, platform_sha, sorted(files)))
        return {"number": 7, "url": "https://github.com/lab/demo/pull/7", "branch": f"lab-deploy/platform-{platform_sha[:12]}", "changed": sorted(files)}


def config(tmp_path, **extra):
    return Config.model_validate({
        "live_dispatch": True, "database": str(tmp_path / "db"), "platform": {"repository": PLATFORM},
        "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
        "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True},
                       {"name": "viewer", "token_sha256": hashlib.sha256(b"view-token").hexdigest(), "applications": ["demo"]}], **extra})


def test_controller_renders_all_three_workflows_and_opens_one_pull_request(tmp_path):
    github = PlatformAware()
    cfg = config(tmp_path)
    c = Controller(cfg, github, Store(cfg.database))
    manager, viewer = cfg.principals
    result = c.update_workflows(manager, "demo")
    assert result["platform_commit"] == NEW and result["current_pin"] == OLD and result["pull_request"]["number"] == 7
    assert github.updates == [("lab/demo", NEW, sorted(f".github/workflows/{n}" for n in TEMPLATES))]
    github.pinned = NEW
    assert c.update_workflows(manager, "demo")["pull_request"] is None  # already pinned: nothing opened
    github.pinned = None  # an app that never had the workflows still gets them
    assert c.update_workflows(manager, "demo", platform_sha=NEW)["current_pin"] is None
    with pytest.raises(ValueError, match="read-only"):
        c.update_workflows(viewer, "demo")
    with pytest.raises(ValueError, match="40-character"):
        c.update_workflows(manager, "demo", platform_sha="abc")
    quiet = Controller(config(tmp_path, live_dispatch=False), github, Store(cfg.database))
    with pytest.raises(ValueError, match="Writes are disabled"):
        quiet.update_workflows(manager, "demo")
    assert quiet.update_workflows(manager, "demo", allow_writes=True)["platform_commit"] == NEW  # the operator's shell may still do it
    unset = Config.model_validate({**cfg.model_dump(), "platform": None})
    with pytest.raises(ValueError, match="platform.repository"):
        Controller(unset, github, Store(cfg.database)).update_workflows(manager, "demo")


def test_tool_and_cli_paths(tmp_path):
    github = PlatformAware()
    with TestClient(build_app(config(tmp_path), github)) as client:
        headers = {"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"}
        result = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                            "params": {"name": "update_app_workflows", "arguments": {"application": "demo"}}}).json()["result"]
        assert not result.get("isError") and result["structuredContent"]["pull_request"]["url"].endswith("/pull/7")
    results = update_workflows(config(tmp_path), PlatformAware(), ["demo", "missing"])
    assert results[0]["pull_request"]["number"] == 7 and "not authorized" in results[1]["error"]


def test_github_adapter_opens_a_pull_request_once(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
    app = config(Path("/tmp")).applications["demo"]
    calls, prs = [], []

    def handler(request):
        calls.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path == "/repos/lab/demo/pulls" and request.method == "GET":
            return httpx.Response(200, json=prs)
        if request.url.path == "/repos/lab/demo/commits/main":
            return httpx.Response(200, json={"sha": "a" * 40, "commit": {"message": "m", "author": {}}})
        if request.url.path == "/repos/lab/demo/git/refs":
            return httpx.Response(422 if calls.count(("POST", "/repos/lab/demo/git/refs", {})) > 1 else 201, json={"message": "Reference already exists"})
        if request.url.path.startswith("/repos/lab/demo/contents/.github/workflows/") and request.method == "GET":
            if request.url.path.endswith("deploy-gcp.yml"):
                return httpx.Response(200, json={"type": "file", "encoding": "base64", "size": 3, "sha": "old", "content": base64.b64encode(b"old").decode()})
            return httpx.Response(404, json={"message": "Not Found"})
        if request.url.path.startswith("/repos/lab/demo/contents/.github/workflows/") and request.method == "PUT":
            body = json.loads(request.content)
            assert body["branch"] == "lab-deploy/platform-" + NEW[:12] and body["message"].startswith("Pin Fast Deploy")
            assert ("sha" in body) == request.url.path.endswith("deploy-gcp.yml")
            return httpx.Response(200 if "sha" in body else 201, json={})
        if request.url.path == "/repos/lab/demo/pulls" and request.method == "POST":
            body = json.loads(request.content)
            assert body["base"] == "main" and body["draft"] is False and "Nothing is merged" in body["body"]
            prs.append({"number": 9, "html_url": "https://github.com/lab/demo/pull/9"})
            return httpx.Response(201, json=prs[-1])
        raise AssertionError((request.method, request.url.path))
    github = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    files = {".github/workflows/deploy-gcp.yml": "new", ".github/workflows/recover-gcp.yml": "r", ".github/workflows/rightsize-gcp.yml": "s"}
    first = github.update_workflows(app, NEW, files)
    assert first["number"] == 9 and first["changed"] == list(files)
    second = github.update_workflows(app, NEW, files)
    assert second["number"] == 9 and second["changed"] == [] and "already open" in second["note"]
    assert not any(method == "PUT" for method, _, _ in calls[len(calls) - 1:])  # the second call wrote nothing
