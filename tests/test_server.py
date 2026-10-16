import hashlib

import pytest
from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient

from labdeploy.config import Config
from labdeploy.server import build_app


@pytest.fixture
def client(tmp_path):
    config = Config.model_validate({"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
        "database": str(tmp_path / "db"), "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True}]})
    with TestClient(build_app(config, FakeGitHub())) as client:
        yield client


def test_unauthenticated_requests_blocked(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/applications").status_code == 401
    assert client.post("/mcp", json={}).status_code == 401


def test_http_plan_and_dry_dispatch(client):
    headers = {"Authorization": "Bearer test-token"}
    result = client.post("/applications/demo/plans", headers=headers)
    assert result.status_code == 200
    plan_id = result.json()["id"]
    result = client.post(f"/plans/{plan_id}/deploy", headers=headers)
    assert result.status_code == 200
    assert result.json()["dispatch"] == "disabled"


def test_real_mcp_initialize_list_and_tool_call(client):
    headers = {"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"}
    params = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
    init = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
    assert init.status_code == 200, init.text
    assert init.json()["result"]["serverInfo"]["name"] == "Fast Deploy"
    listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert "deploy_staging" in [t["name"] for t in listed.json()["result"]["tools"]]
    called = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "create_deployment_plan", "arguments": {"application": "demo"}}})
    assert called.status_code == 200, called.text
    assert not called.json()["result"].get("isError", False), called.text
    assert "planned" in called.text

