import base64
import json

import httpx
import pytest
from conftest import TARGET

from labdeploy.config import Application
from labdeploy.github import GitHub


def test_dispatch_uses_fixed_workflow_and_commit(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-credential")
    def handler(request):
        assert request.url.path == "/repos/lab/demo/actions/workflows/deploy-gcp.yml/dispatches"
        body = json.loads(request.content)
        assert body == {"ref": "main", "inputs": {"commit_sha": "a" * 40, "spec_hash": "b" * 64, "plan_id": "c" * 32, "target_json": json.dumps(TARGET, sort_keys=True)}}
        return httpx.Response(200, json={"workflow_run_id": 42})
    gh = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    assert gh.dispatch(Application(repository="lab/demo", service="demo", target=TARGET), {"sha": "a" * 40, "spec_hash": "b" * 64, "id": "c" * 32, "target": TARGET})["workflow_run_id"] == 42


def test_config_pr_is_draft_and_never_overwrites_a_file(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-credential")
    requests = []

    def handler(request):
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        path = request.url.path
        if request.method == "GET" and path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        if request.method == "GET" and "/commits/" in path:
            return httpx.Response(200, json={"sha": "a" * 40})
        if request.method == "POST" and path.endswith("/git/refs"):
            return httpx.Response(201, json={})
        if request.method == "GET" and "/contents/" in path:
            if path.endswith("/SECURITY.md"):  # appeared on the branch after the files were drafted
                return httpx.Response(200, json={"type": "file", "encoding": "base64", "sha": "s", "content": base64.b64encode(b"ours").decode()})
            return httpx.Response(404, json={})
        if request.method == "PUT":
            assert "sha" not in body  # create-only: an existing file is never overwritten
            return httpx.Response(201, json={})
        if request.method == "POST" and path.endswith("/pulls"):
            assert body["draft"] is True
            return httpx.Response(201, json={"number": 1, "html_url": "https://github.com/lab/demo/pull/1"})
        raise AssertionError(f"unexpected {request.method} {path}")
    gh = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    app = Application(repository="lab/demo", service="demo", target=TARGET)
    result = gh.open_pull_request(app, "lab-deploy/contract-files", {"platform.yaml": "name: demo\nreviewed: false\n", "SECURITY.md": "theirs"},
                                  "Add the files", "body", "message", draft=True, create_only=True)
    assert result["number"] == 1 and result["changed"] == ["platform.yaml"] and result["kept"] == ["SECURITY.md"]
    assert [method for method, *_ in requests].count("PUT") == 1


def test_symlink_content_rejected(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-credential")
    gh = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"type": "symlink"}))))
    with pytest.raises(ValueError, match="regular"):
        gh.file(Application(repository="lab/demo", service="demo", target=TARGET), "a" * 40, "platform.yaml")


def test_status_tolerates_a_run_that_is_still_being_created(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-credential")
    phase = {"status": "queued", "title": "Deploy GCP staging", "sha": ""}

    def handler(request):
        if request.url.path.endswith("/runs/42"):
            return httpx.Response(200, json={"id": 42, "status": phase["status"], "display_title": phase["title"], "head_sha": phase["sha"], "run_attempt": 1})
        return httpx.Response(200, json={"jobs": []})

    gh = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    app = Application(repository="lab/demo", service="demo", target=TARGET)
    plan = {"id": "c" * 32, "sha": "a" * 40, "dispatch_result": {"workflow_run_id": 42}}
    assert gh.status(app, plan)["status"] == "not_visible_yet"
    phase.update(status="in_progress")
    with pytest.raises(ValueError, match="identity"):
        gh.status(app, plan)
    phase.update(title="deploy-" + "c" * 32, sha="a" * 40)
    assert gh.status(app, plan)["status"] == "in_progress"
