"""The server end to end over HTTP: authentication, the MCP handshake and tools with structured results, the REST API,
audit events, and the catalog that both are held to."""
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from delivery_fakes import IMAGE, TEAM_TOKEN, release_dict
from fastapi.testclient import TestClient
from fleet_fakes import V1, V2, World

from appdelivery import __version__, cli
from appdelivery.catalog import CATALOG, MEANINGS, render_markdown
from appdelivery.errors import ErrorCode
from appdelivery.server import build_app

ROOT = Path(__file__).resolve().parents[2]
ONCALL_TOKEN = "oncall-token"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def served(tmp_path):
    world = World(database=str(tmp_path / "delivery.db"))
    world.config.auth_failures_per_minute = 3
    world.config.allowed_hosts = ["testserver"]
    world.config.principals[1].token_sha256 = hashlib.sha256(ONCALL_TOKEN.encode()).hexdigest()
    with TestClient(build_app(world.config, delivery=world.delivery)) as client:
        yield client, world


def auth(token=TEAM_TOKEN):
    return {"Authorization": f"Bearer {token}"}


def call(client, name, arguments, token=TEAM_TOKEN):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    response = client.post("/mcp", headers={**MCP_HEADERS, **auth(token)}, json=body)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def dev(image=V1, **overrides):
    return release_dict(environment="dev", image=image, **overrides)


def test_authentication_is_required_and_failed_attempts_are_throttled(served):
    client, _ = served
    assert client.get("/healthz").json() == {"status": "ok", "version": __version__}
    assert client.get("/releases").status_code == 401 and client.post("/mcp", json={}).status_code == 401
    assert client.get("/releases", headers=auth("wrong")).json() == {"status": "FAILED", "error": {"code": "PERMISSION_DENIED", "message": "Authentication required"}}
    client.get("/releases", headers=auth("wrong"))
    assert client.get("/releases", headers=auth("wrong")).status_code == 429


def test_the_mcp_endpoint_refuses_a_host_it_was_not_told_about(served):
    client, _ = served
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert client.post("/mcp", headers={**MCP_HEADERS, **auth(), "Host": "attacker.example"}, json=body).status_code == 421


def test_the_mcp_handshake_lists_the_catalog(served):
    client, _ = served
    params = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
    init = client.post("/mcp", headers={**MCP_HEADERS, **auth()}, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}).json()["result"]
    assert init["serverInfo"] == {"name": "Application Delivery", "version": __version__} and "plan_release" in init["instructions"]
    listed = client.post("/mcp", headers={**MCP_HEADERS, **auth()}, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    by_name = {tool["name"]: tool for tool in listed}
    assert sorted(by_name) == sorted(spec.name for spec in CATALOG)
    assert by_name["remove_release"]["annotations"]["destructiveHint"] is True and by_name["get_release"]["annotations"]["readOnlyHint"] is True
    assert by_name["deploy_release"]["inputSchema"]["additionalProperties"] is False and "ReleaseRequest" in json.dumps(by_name["deploy_release"]["inputSchema"])
    for spec in CATALOG:
        assert set(by_name[spec.name]["inputSchema"].get("properties", {})) == {item.name for item in spec.inputs}, spec.name


def test_a_release_through_mcp_from_plan_to_remove(served):
    client, world = served
    planned = call(client, "plan_release", {"release": dev()})["structuredContent"]
    assert planned["status"] == "PLANNED" and [item["name"] for item in planned["machines"]] == ["shop-web-1", "shop-web-2"]
    first = call(client, "deploy_release", {"release": dev(), "wait_seconds": 45})["structuredContent"]
    assert first["status"] == "RUNNING" and [item["status"] for item in first["targets"]] == ["HEALTHY", "HEALTHY"]
    second = call(client, "deploy_release", {"release": dev(V2)})["structuredContent"]
    assert second["previous"] == first["release_id"] and second["can_roll_back"]
    assert call(client, "get_release", {"release_id": second["release_id"]})["structuredContent"]["status"] == "RUNNING"
    assert call(client, "list_releases", {"environment": "dev"})["structuredContent"]["count"] == 2
    running = call(client, "get_running", {"request_id": first["request_id"], "live": True})["structuredContent"]
    assert running["groups"][0]["image"] == V2 and all(item["matches"] for item in running["groups"][0]["machines"])
    rolled = call(client, "rollback_release", {"release_id": second["release_id"]})["structuredContent"]
    assert rolled["status"] == "ROLLED_BACK" and world.fleet.app["shop-web-1"] == V1
    removed = call(client, "remove_release", {"release_id": first["release_id"]})["structuredContent"]
    assert removed["status"] == "REMOVED" and world.fleet.app["shop-web-1"] is None
    status = call(client, "get_service_status", {})["structuredContent"]
    assert status["version"] == __version__ and status["environments"]["prod"]["require_approval"] is True


def test_errors_through_mcp_are_structured(served):
    client, world = served
    tagged = call(client, "deploy_release", {"release": dev(image="us-central1-docker.pkg.dev/acme-apps/shop/web:1.0")})
    assert tagged["isError"] is True and tagged["structuredContent"]["error"]["code"] == "INVALID_RELEASE"
    assert json.loads(tagged["content"][0]["text"]) == tagged["structuredContent"], "the same error as text, for clients that read only text"
    command = call(client, "deploy_release", {"release": {**dev(), "command": ["sh", "-c", "id"]}})
    assert command["isError"] is True and "not a field of a release" in command["structuredContent"]["error"]["message"]
    first = call(client, "deploy_release", {"release": dev()})["structuredContent"]
    refused = call(client, "rollback_release", {"release_id": first["release_id"]})
    assert refused["isError"] and refused["structuredContent"]["error"]["code"] == "INVALID_STATE"
    missing = call(client, "get_release", {"release_id": "rel-000000000000"})["structuredContent"]
    assert missing["error"]["code"] == "NOT_FOUND"
    unknown = call(client, "get_release", {"releaseid": first["release_id"]})
    assert unknown["isError"] and "did you mean release_id" in unknown["content"][0]["text"]
    assert [call["action"] for call in world.fleet.calls].count("deploy") == 2, "nothing refused reached a machine"


def test_approval_needs_another_principal(served):
    client, _ = served
    waiting = call(client, "deploy_release", {"release": release_dict(environment="prod", image=IMAGE)})["structuredContent"]
    assert waiting["status"] == "AWAITING_APPROVAL"
    own = call(client, "approve_release", {"release_id": waiting["release_id"]})
    assert own["isError"] and own["structuredContent"]["error"]["code"] == "PERMISSION_DENIED"
    approved = call(client, "approve_release", {"release_id": waiting["release_id"], "wait_seconds": 45}, token=ONCALL_TOKEN)["structuredContent"]
    assert approved["status"] == "RUNNING" and approved["approved_by"] == "shop-oncall"


def test_the_rest_api_mirrors_the_tools(served):
    client, world = served
    assert client.post("/releases/plan", headers=auth(), json=dev()).json()["status"] == "PLANNED"
    created = client.post("/releases?wait_seconds=45", headers=auth(), json=dev())
    assert created.status_code == 200 and created.json()["status"] == "RUNNING"
    release_id = created.json()["release_id"]
    assert client.get(f"/releases/{release_id}", headers=auth()).json()["release_id"] == release_id
    assert client.get("/releases?environment=dev", headers=auth()).json()["count"] == 1
    assert client.get("/releases/rel-000000000000", headers=auth()).status_code == 404
    assert client.post(f"/releases/{release_id}/rollback", headers=auth()).status_code == 409
    assert client.post("/releases", headers=auth(), json={**dev(), "image": "shop:latest"}).status_code == 400
    assert client.post("/releases", headers=auth(), content=b"not json").json()["error"]["code"] == "INVALID_RELEASE"
    assert client.post("/releases", headers=auth(ONCALL_TOKEN), json=dev()).status_code == 403
    running = client.get(f"/running?request_id={created.json()['request_id']}", headers=auth()).json()
    assert running["groups"][0]["release_id"] == release_id
    waiting = client.post("/releases", headers=auth(), json=release_dict(environment="prod", image=IMAGE)).json()
    assert client.post(f"/releases/{waiting['release_id']}/approve?wait_seconds=45", headers=auth(ONCALL_TOKEN)).json()["status"] == "RUNNING"
    # the same request and group are the same machines, whatever the environment: the production release replaced it
    assert client.get(f"/releases/{release_id}", headers=auth()).json()["status"] == "SUPERSEDED"
    assert client.delete(f"/releases/{release_id}", headers=auth()).status_code == 409
    assert client.delete(f"/releases/{waiting['release_id']}", headers=auth()).json()["status"] == "REMOVED"
    catalog = client.get("/catalog", headers=auth()).json()
    assert len(catalog["tools"]) == len(CATALOG) and catalog["markdown"].startswith("# Delivery service catalog")
    assert client.get("/status", headers=auth()).json()["version"] == __version__


def test_every_call_is_an_audit_event(served, tmp_path):
    client, _ = served
    call(client, "get_service_status", {})
    call(client, "get_release", {"release_id": "rel-000000000000"})
    client.get("/releases/rel-000000000000", headers=auth())
    client.get("/releases", headers=auth("wrong"))
    rows = sqlite3.connect(tmp_path / "delivery.db").execute("SELECT kind, name, identity, outcome FROM events ORDER BY id").fetchall()
    assert ("tool", "get_service_status", "shop-team", "ok") in rows and ("tool", "get_release", "shop-team", "error") in rows
    assert ("rest", "get", "shop-team", "error") in rows


def test_the_catalog_document_is_current_and_explains_every_code():
    expected = render_markdown()
    assert (ROOT / "docs" / "DELIVERY-CATALOG.md").read_text() == expected, "run: app-delivery catalog --write docs/DELIVERY-CATALOG.md"
    assert set(MEANINGS) == set(ErrorCode) and all(f"| `{code.value}` |" in expected for code in ErrorCode)
    assert all(code in ErrorCode.__members__ for spec in CATALOG for code in spec.errors)


def test_the_catalog_command(capsys, tmp_path):
    assert cli.main(["catalog"]) == 0 and capsys.readouterr().out.startswith("# Delivery service catalog")
    assert cli.main(["catalog", "--write", str(tmp_path / "c.md")]) == 0 and (tmp_path / "c.md").read_text() == render_markdown()
