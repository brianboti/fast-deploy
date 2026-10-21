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

from labdeploy.config import Application, Config, Principal
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
    assert plan["cost"]["ceiling_policy"] == {"limit_usd_month": 200, "compared_usd_month": 132.71, "status": "within_limit"}
    c.config.applications["demo"].cost_ceiling_usd = 100  # tightened after planning: dispatch must stop
    with pytest.raises(ValueError, match="Cost policy"):
        c.deploy(p, plan["id"])
    assert gh.calls == 0
    assert c.cost_report(p, "demo")["report"] is None


def test_a_limited_identity_gets_its_own_lines_of_the_weekly_report_and_the_kept_report_stays_whole(setup):
    """get_cost_report for an identity that sees only some applications (Controller.cost_report_for): what was measured on the
    application and the bill's lines labelled with its own; a `*` identity gets the report as the workflow wrote it. The
    whole report, which the settings page keeps for every viewer, is never changed by a limited view of it."""
    c, limited, gh = setup  # limited sees demo only
    c.config.monthly_budgets_usd["test-project"] = 20
    report = {"service": "demo", "project_id": "test-project", "window_days": 14, "estimate": {"idle_floor_usd_month": 0.0},
              "rightsizing": {"observed": None, "recommendations": []}, "cloud_run_cost_recommender": {"status": "ok", "recommendations": [{"description": "x"}]},
              "month": {"projects": [{"project": "test-project", "net_cost_usd": 8.4, "forecast_usd": 12.7}]},
              "release_tolls": {"month": "2026-10", "fresh_images": 1, "image_scans_usd": 0.26, "actions": {"billable": True, "minutes": 91}},
              "actual_spend": {"window_days": 30, "data_through": "2026-10-18T23:00", "total_net_cost_usd": 9.5, "table": "billing-proj.billing.export",
                               "by_service": [{"service": "Cloud Run", "net_cost_usd": 9.5}], "by_project": [{"project": "test-project", "net_cost_usd": 9.5}],
                               "by_app": [{"app": "demo", "net_cost_usd": 7.0}, {"app": "lab-x", "net_cost_usd": 2.5}],
                               "previous": {"window_days": 30, "total_net_cost_usd": 8.0, "by_app": [{"app": "lab-x", "net_cost_usd": 3.0}]},
                               "movers": {"by_service": [], "by_app": [{"app": "lab-x", "net_cost_usd": 2.5, "before_usd": 3.0}]},
                               "app": {"name": "demo", "net_cost_usd": 7.0}, "note": "Staging and production together"}}
    gh.cost_report = lambda app, workflow="rightsize-gcp.yml": {"workflow_run_id": 1, "report": json.loads(json.dumps(report))}
    own = c.cost_report(limited, "demo")
    assert own["report"]["actual_spend"] == {"window_days": 30, "data_through": "2026-10-18T23:00", "by_app": [{"app": "demo", "net_cost_usd": 7.0}],
                                             "previous": {"window_days": 30, "by_app": []}, "movers": {"by_app": []}, "app": {"name": "demo", "net_cost_usd": 7.0},
                                             "note": own["report"]["actual_spend"]["note"]}
    assert "carry no label" in own["report"]["actual_spend"]["note"] and own["report"]["release_tolls"] == {"month": "2026-10", "fresh_images": 1, "image_scans_usd": 0.26}
    assert set(own["report"]) == {"service", "project_id", "window_days", "estimate", "rightsizing", "release_tolls", "actual_spend"}
    assert "budgets" not in own and own["spending_guards"]["project_id"] == "test-project" and "every application" in own["hidden"]
    everyone = Principal(name="operator", token_sha256="0" * 64, applications=["*"], can_deploy=True)
    whole = c.read_cost_report(c.config.applications["demo"])
    assert c.cost_report_for(everyone, whole) is whole and whole["report"] == report and whole["budgets"] == {"test-project": 20}
    c.cost_report_for(limited, whole)
    assert whole["report"] == report and "hidden" not in whole  # the limited view is a copy
    assert c.cost_report(limited, "demo") == own and c.cost_report_for(limited, {"report": None, "note": "none yet", "budgets": {}}) == {
        "report": None, "note": "none yet", "hidden": own["hidden"]}


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
    # issues: the living report; actions: read, the month's runs and jobs for the billable-minutes line (1.9.0)
    assert workflow["permissions"] == {"contents": "read"} and job["permissions"] == {"contents": "read", "id-token": "write", "issues": "write", "actions": "read"}
    assert workflow["concurrency"]["group"] != "gcp-staging" and "schedule" in workflow[True]
    checkouts = [step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")]
    assert checkouts and all(step["with"]["repository"] == "PLATFORM_REPOSITORY" and step["with"]["ref"] == "PLATFORM_COMMIT" for step in checkouts)
    assert all("@" in step["uses"] and len(step["uses"].split("@")[1]) == 40 for step in job["steps"] if "uses" in step)
    assert any(step.get("run") == "python -m labdeploy.cost" for step in job["steps"])
