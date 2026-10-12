import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
import yaml
from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient

from labdeploy.config import Application, Config
from labdeploy.github import GitHub
from labdeploy.server import build_app


def test_cost_tool_and_ceiling_policy(setup):
    c, p, gh = setup
    result = c.cost(p, "demo")
    assert result["compute_ceiling_usd_month"] == 132.714 and result["ceiling_policy"]["status"] == "not_configured"
    assert result["reviewed"] is True and result["findings"][-1]["id"] == "authenticated_only"
    assert c.cost(p, "demo", 100_000, 0.2)["scenario"]["usd_month"]["worst_case"] == 0.545
    c.config.applications["demo"].cost_ceiling_usd = 100
    with pytest.raises(ValueError, match="Cost policy"):
        c.plan(p, "demo")
    assert c.cost(p, "demo")["ceiling_policy"]["status"] == "exceeds_limit"
    c.config.applications["demo"].cost_ceiling_usd = 200
    plan = c.plan(p, "demo")
    assert plan["cost"]["ceiling_policy"] == {"limit_usd_month": 200, "status": "within_limit"}
    c.config.applications["demo"].cost_ceiling_usd = 100  # tightened after planning: dispatch must stop
    with pytest.raises(ValueError, match="Cost policy"):
        c.deploy(p, plan["id"])
    assert gh.calls == 0
    assert c.cost_report(p, "demo")["report"] is None


def test_cost_ceiling_configuration_validation():
    base = {"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET, "cost_ceiling_usd": 150}},
            "principals": [{"name": "m", "token_sha256": hashlib.sha256(b"t").hexdigest(), "applications": ["demo"]}]}
    assert Config.model_validate(base).applications["demo"].cost_ceiling_usd == 150
    assert Config.model_validate(yaml.safe_load(yaml.safe_dump(base))).applications["demo"].cost_ceiling_usd == 150
    with pytest.raises(ValueError, match="positive"):
        Application(repository="lab/demo", service="demo", target=TARGET, cost_ceiling_usd=0)
    with pytest.raises(ValueError):
        Application(repository="lab/demo", service="demo", target=TARGET, cost_ceiling_usd="150")


def test_github_cost_report_downloads_newest_artifact_without_forwarding_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("cost-report.json", json.dumps({"service": "demo", "rightsizing": {"recommendations": []}}))
    blob = buffer.getvalue()
    seen = []

    def handler(request):
        seen.append((request.url.host, "authorization" in request.headers))
        if request.url.path.endswith("/actions/workflows/rightsize-gcp.yml/runs"):
            assert request.url.params["status"] == "success" and request.url.params["branch"] == "main"
            return httpx.Response(200, json={"workflow_runs": [{"id": 8, "html_url": "u8", "updated_at": "t8"}, {"id": 9, "html_url": "u9", "updated_at": "t9"}]})
        if request.url.path.endswith("/actions/runs/8/artifacts"):
            return httpx.Response(200, json={"artifacts": [{"id": 70, "name": "cost-report", "expired": True, "size_in_bytes": 10}]})
        if request.url.path.endswith("/actions/runs/9/artifacts"):
            return httpx.Response(200, json={"artifacts": [{"id": 77, "name": "cost-report", "expired": False, "size_in_bytes": len(blob)}]})
        if request.url.path.endswith("/actions/artifacts/77/zip"):
            return httpx.Response(302, headers={"location": "https://blob.example/signed"})
        if request.url.host == "blob.example":
            return httpx.Response(200, content=blob)
        raise AssertionError(str(request.url))

    gh = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    app = Application(repository="lab/demo", service="demo", target=TARGET)
    result = gh.cost_report(app)
    assert result["report"]["service"] == "demo" and result["workflow_run_id"] == 9 and result["run_url"] == "u9"
    assert [auth for host, auth in seen if host == "blob.example"] == [False]
    assert all(auth for host, auth in seen if host == "api.github.com")
    none = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"workflow_runs": []}))))
    assert none.cost_report(app)["report"] is None


def test_oversized_cost_report_rejected(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")

    def handler(request):
        if request.url.path.endswith("/runs"):
            return httpx.Response(200, json={"workflow_runs": [{"id": 1}]})
        return httpx.Response(200, json={"artifacts": [{"id": 2, "name": "cost-report", "size_in_bytes": 10_000_000}]})

    gh = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError, match="1 MiB"):
        gh.cost_report(Application(repository="lab/demo", service="demo", target=TARGET))


def test_rest_and_mcp_cost_endpoints(tmp_path):
    config = Config.model_validate({"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
                                    "database": str(tmp_path / "db"),
                                    "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True}]})
    headers = {"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"}
    with TestClient(build_app(config, FakeGitHub())) as client:
        rest = client.get("/applications/demo/cost", headers=headers, params={"requests_per_month": 100000, "average_request_seconds": 0.2})
        assert rest.status_code == 200 and rest.json()["scenario"]["usd_month"]["worst_case"] == 0.545
        assert client.get("/applications/demo/cost", headers=headers, params={"requests_per_month": 5}).status_code == 400
        assert client.get("/applications/demo/cost-report", headers=headers).json()["report"] is None
        assert client.get("/applications/demo/cost").status_code == 401
        called = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                            "params": {"name": "estimate_cost", "arguments": {"application": "demo"}}}).json()["result"]
        assert not called.get("isError") and "compute_ceiling_usd_month" in called["content"][0]["text"]


def test_rightsize_workflow_is_read_only_and_pinned():
    workflow = yaml.safe_load(Path("templates/rightsize-gcp.yml").read_text())
    job = workflow["jobs"]["rightsize"]
    assert workflow["permissions"] == {"contents": "read"} and job["permissions"] == {"contents": "read", "id-token": "write"}
    assert workflow["concurrency"]["group"] != "gcp-staging" and "schedule" in workflow[True]
    checkouts = [step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")]
    assert checkouts and all(step["with"]["repository"] == "PLATFORM_REPOSITORY" and step["with"]["ref"] == "PLATFORM_COMMIT" for step in checkouts)
    assert all("@" in step["uses"] and len(step["uses"].split("@")[1]) == 40 for step in job["steps"] if "uses" in step)
    assert any(step.get("run") == "python -m labdeploy.cost" for step in job["steps"])
