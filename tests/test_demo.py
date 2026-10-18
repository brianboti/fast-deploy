import hashlib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from labdeploy.controller import Controller, Store
from labdeploy.demo import LocalRepository, demo_config
from labdeploy.server import build_app

DEMO_APP = Path("examples/demo-app")


def test_local_head_is_a_stable_content_hash(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    repo = LocalRepository(tmp_path)
    first = repo.head(None)
    assert len(first) == 40 and int(first, 16) >= 0
    assert repo.head(None) == first
    (tmp_path / "requirements.txt").write_text("fastapi\nhttpx\n")
    assert repo.head(None) != first


def test_missing_file_behaves_like_github_404(tmp_path):
    repo = LocalRepository(tmp_path)
    with pytest.raises(httpx.HTTPStatusError) as info:
        repo.file(None, repo.head(None), "platform.yaml")
    assert info.value.response.status_code == 404


def test_stale_sha_and_symlink_are_rejected(tmp_path):
    (tmp_path / "requirements.txt").symlink_to("/etc/hosts")
    repo = LocalRepository(tmp_path)
    with pytest.raises(httpx.HTTPStatusError):
        repo.file(None, repo.head(None), "requirements.txt")
    with pytest.raises(ValueError, match="changed on disk"):
        repo.file(None, "0" * 40, "requirements.txt")


def test_demo_flow_never_dispatches_or_writes(tmp_path):
    config = demo_config(str(tmp_path / "demo.db"), hashlib.sha256(b"demo").hexdigest())
    assert config.live_dispatch is False
    repo = LocalRepository(DEMO_APP)
    c = Controller(config, repo, Store(config.database))
    p = config.principals[0]
    report = c.inspect(p, "demo")
    assert report["supported_candidate"] and report["draft_spec"]["reviewed"] is False
    plan = c.plan(p, "demo")
    assert plan["sha"] == repo.head(None) and plan["target"] == config.applications["demo"].target.model_dump()
    assert c.deploy(p, plan["id"])["dispatch"] == "disabled"
    status = c.status(p, plan["id"])
    assert status["state"] == "planned"
    assert [e["event"] for e in status["events"]] == ["planned"]
    assert [x["id"] for x in c.plans(p)["plans"]] == [plan["id"]]
    drafted = c.propose_config(p, "demo")  # the files the demo lacks are listed, and nothing is written in demo mode
    assert drafted["pull_request"] == "disabled" and "platform.yaml" not in drafted["files"] and "platform.yaml" in drafted["existing"]
    with pytest.raises(ValueError, match="never"):
        repo.dispatch(None, plan)


def test_demo_server_serves_all_tools_over_mcp(tmp_path):
    config = demo_config(str(tmp_path / "demo.db"), hashlib.sha256(b"demo").hexdigest())
    headers = {"Authorization": "Bearer demo", "Accept": "application/json, text/event-stream"}
    with TestClient(build_app(config, LocalRepository(DEMO_APP))) as client:
        def rpc(method, params=None):
            body = {"jsonrpc": "2.0", "id": 1, "method": method}
            if params is not None:
                body["params"] = params
            return client.post("/mcp", headers=headers, json=body).json()["result"]

        init = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        assert "dispatched" in init["instructions"]
        names = [t["name"] for t in rpc("tools/list")["tools"]]
        assert names == ["list_applications", "analyze_repository", "create_deployment_plan", "propose_configuration", "update_app_workflows",
                         "propose_infrastructure", "add_production", "promote_to_production", "rollback_production", "onboard_application", "import_contract",
                         "check_repository", "get_onboarding_status", "deploy_staging",
                         "deploy_latest", "rollback_staging", "cancel_deployment", "compare_with_live", "get_service_health", "get_recent_logs", "get_deployment_status",
                         "list_plans", "estimate_cost", "propose_rightsizing", "get_cost_report", "list_events", "get_controller_status"]
        planned = rpc("tools/call", {"name": "create_deployment_plan", "arguments": {"application": "demo"}})
        assert not planned.get("isError"), planned
        refused = rpc("tools/call", {"name": "create_deployment_plan", "arguments": {"application": "demo", "cloud": "aws"}})
        assert refused.get("isError") and "GCP staging" in refused["content"][0]["text"]
