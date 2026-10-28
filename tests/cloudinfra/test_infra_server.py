"""The server end to end: authentication, the MCP handshake, tool calls with structured results, and the REST API."""
import json

import pytest
from fastapi.testclient import TestClient
from gcp_fakes import TOKEN, VM_REQUEST, make_config, make_provider

from cloudinfra import __version__
from cloudinfra.mcp.catalog import CATALOG
from cloudinfra.mcp.server import build_app
from cloudinfra.orchestration.provisioner import InlineExecutor
from cloudinfra.providers import ProviderRegistry

HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}


@pytest.fixture
def served(tmp_path):
    config = make_config(tmp_path, auth_failures_per_minute=3)
    provider, fake = make_provider(config)
    app = build_app(config, registry=ProviderRegistry(config, {"gcp": provider}), executor=InlineExecutor())
    with TestClient(app) as client:
        yield client, fake


def call(client, name, arguments, token=TOKEN):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    response = client.post("/mcp", headers={**HEADERS, "Authorization": f"Bearer {token}"}, json=body)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_authentication_is_required_and_bursts_are_throttled(served):
    client, _ = served
    assert client.get("/healthz").status_code == 200 and client.get("/healthz").json()["version"] == __version__
    assert client.get("/infrastructure").status_code == 401 and client.post("/mcp", json={}).status_code == 401
    assert client.get("/infrastructure", headers={"Authorization": "Bearer wrong"}).json() == {"status": "FAILED", "error": {"code": "PERMISSION_DENIED", "message": "Authentication required"}}
    assert client.get("/infrastructure", headers={"Authorization": "Bearer wrong"}).status_code == 429


def test_mcp_handshake_lists_the_catalog(served):
    client, _ = served
    params = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
    init = client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
    assert init.status_code == 200 and init.json()["result"]["serverInfo"] == {"name": "Cloud Infrastructure", "version": __version__}
    listed = client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    assert sorted(tool["name"] for tool in listed) == sorted(spec.name for spec in CATALOG)
    by_name = {tool["name"]: tool for tool in listed}
    assert by_name["destroy_infrastructure"]["annotations"]["destructiveHint"] is True and by_name["get_compute"]["annotations"]["readOnlyHint"] is True
    assert by_name["provision_infrastructure"]["inputSchema"]["additionalProperties"] is False and "InfrastructureRequest" in json.dumps(by_name["provision_infrastructure"]["inputSchema"])
    assert by_name["create_compute"]["description"].startswith("Create one or more identical virtual machines")


def test_provision_status_and_destroy_through_mcp(served):
    client, fake = served
    result = call(client, "provision_infrastructure", {"request": VM_REQUEST, "wait_seconds": 45})
    assert not result.get("isError"), result
    found = result["structuredContent"]
    assert found["status"] == "READY" and found["resources"]["compute"][0]["cpu"] == 4 and found["resources"]["compute"][0]["internal_ip"]
    status = call(client, "get_infrastructure_status", {"request_id": found["request_id"]})["structuredContent"]
    assert status["status"] == "READY" and status["operations"][0]["kind"] == "provision"
    health = call(client, "get_resource_health", {"resource_id": found["resources"]["compute"][0]["id"]})["structuredContent"]
    assert health["healthy"] is True
    listed = call(client, "list_infrastructure", {})["structuredContent"]
    assert listed["count"] == 1
    destroyed = call(client, "destroy_infrastructure", {"request_id": found["request_id"], "wait_seconds": 45})["structuredContent"]
    assert destroyed["status"] == "DESTROYED" and not fake.instances
    assert call(client, "get_service_status", {})["structuredContent"]["providers"]["gcp"]["project"] == "test-project"


def test_structured_errors_through_mcp(served):
    client, fake = served
    invalid = call(client, "provision_infrastructure", {"request": {"provider": "gcp", "region": "us-central1", "compute": {"cpu": "four", "memory_gb": 16}}})
    assert invalid["isError"] is True and invalid["structuredContent"]["status"] == "FAILED" and invalid["structuredContent"]["error"]["code"] == "INVALID_REQUEST"
    assert json.loads(invalid["content"][0]["text"]) == invalid["structuredContent"], "the same error as text, for clients that read only text"
    refused = call(client, "provision_infrastructure", {"request": {**VM_REQUEST, "provider": "aws"}})
    assert refused["isError"] is True and json.loads(refused["content"][0]["text"])["error"]["code"] == "UNSUPPORTED_PROVIDER"
    unsupported = refused["structuredContent"]
    assert unsupported == {"status": "FAILED", "error": {"code": "UNSUPPORTED_PROVIDER", "message": unsupported["error"]["message"],
                                                        "details": {"provider": "aws", "available": ["gcp"]}, "resource": None}}
    shape = call(client, "create_compute", {"region": "us-central1", "cpu": 3, "memory_gb": 16})["structuredContent"]
    assert shape["error"]["code"] == "INVALID_COMPUTE_CONFIGURATION" and "even number" in shape["error"]["message"]
    forbidden = call(client, "create_compute", {"region": "us-central1", "cpu": 2, "memory_gb": 8}, token="reader-token")["structuredContent"]
    assert forbidden["error"]["code"] == "PERMISSION_DENIED"
    checked = call(client, "validate_infrastructure_request", {"request": {"region": "europe-west1", "compute": {"cpu": 2, "memory_gb": 8}, "database": {}}})
    validated = checked["structuredContent"]
    assert validated["valid"] is False and validated["problems"][0]["code"] == "INVALID_DATABASE_CONFIGURATION"
    assert not checked.get("isError"), "an invalid request reported by the validation tool is a result, not an execution error"
    missing = call(client, "get_infrastructure_status", {"request_id": "f" * 32})["structuredContent"]
    assert missing["error"]["code"] == "NOT_FOUND"
    unknown = call(client, "get_infrastructure_status", {"requestid": "f" * 32})
    assert unknown["isError"] and "did you mean request_id" in unknown["content"][0]["text"]
    assert fake.instances == {}


def test_rest_api_mirrors_the_tools(served):
    client, fake = served
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post("/infrastructure/validate", headers=headers, json=VM_REQUEST).json()["valid"] is True
    planned = client.post("/infrastructure/plan", headers=headers, json=VM_REQUEST).json()
    assert planned["status"] == "PLANNED" and planned["planned"][0]["spec"]["machine_type"] == "e2-standard-4"
    created = client.post("/infrastructure?wait_seconds=45", headers=headers, json=VM_REQUEST)
    assert created.status_code == 200 and created.json()["status"] == "READY"
    request_id = created.json()["request_id"]
    assert client.get(f"/infrastructure/{request_id}", headers=headers).json()["status"] == "READY"
    assert client.get("/infrastructure", headers=headers).json()["count"] == 1
    assert client.get("/infrastructure/" + "0" * 32, headers=headers).status_code == 404
    assert client.post("/infrastructure", headers=headers, json={**VM_REQUEST, "provider": "aws"}).status_code == 400
    assert client.post("/infrastructure", headers={"Authorization": "Bearer reader-token"}, json=VM_REQUEST).status_code == 403
    assert client.delete(f"/infrastructure/{request_id}?wait_seconds=45", headers=headers).json()["status"] == "DESTROYED"
    assert client.delete(f"/infrastructure/{request_id}", headers=headers).status_code == 409
    catalog = client.get("/catalog", headers=headers).json()
    assert len(catalog["tools"]) == len(CATALOG) and catalog["markdown"].startswith("# MCP service catalog")
    assert client.get("/status", headers=headers).json()["version"] == __version__


def test_audit_events_record_every_call(served):
    client, _ = served
    call(client, "get_service_status", {})
    call(client, "get_infrastructure_status", {"request_id": "f" * 32})
    events = client.app.state.state.recent_events(limit=10)
    assert [(event["name"], event["outcome"]) for event in events[:2]] == [("get_infrastructure_status", "error"), ("get_service_status", "ok")]
    assert events[0]["identity"] == "app-layer" and events[0]["detail"].startswith("NOT_FOUND")
