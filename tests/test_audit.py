"""Phase 1: the audit trail records tool calls, REST calls, sign-ins, throttling and token reuse."""
import hashlib
import logging

from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient
from test_oauth import make_config, register, sign_in, tokens_for

from labdeploy.config import Config
from labdeploy.server import LOGGERS, attach_log_file, build_app

MANAGER = {"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"}
VIEWER = {"Authorization": "Bearer view-token", "Accept": "application/json, text/event-stream"}


def two_identities(tmp_path, **extra):
    return Config.model_validate({
        "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}}, "database": str(tmp_path / "db"),
        "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True},
                       {"name": "viewer", "token_sha256": hashlib.sha256(b"view-token").hexdigest(), "applications": ["demo"]}], **extra})


def call(client, name, headers=MANAGER, **arguments):
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}).json()["result"]


def test_tool_and_rest_calls_are_recorded_with_identity_subject_and_outcome(tmp_path):
    with TestClient(build_app(two_identities(tmp_path), FakeGitHub())) as client:
        assert not call(client, "list_applications").get("isError")
        refused = call(client, "create_deployment_plan", application="demo", cloud="aws")
        assert refused["isError"]
        assert client.get("/plans", headers=MANAGER).status_code == 200
        assert client.get("/applications", headers=VIEWER).status_code == 200
        events = call(client, "list_events")["structuredContent"]["events"]
        seen = {(e["kind"], e["name"], e["identity"], e["outcome"]) for e in events}
        assert ("tool", "list_applications", "manager", "ok") in seen
        assert ("rest", "plans", "manager", "ok") in seen and ("rest", "applications", "viewer", "ok") in seen
        policy = next(e for e in events if e["name"] == "create_deployment_plan")
        assert policy["outcome"] == "error" and "GCP staging" in policy["detail"] and policy["application"] == "demo"
        assert all(e["client"] == "testclient" and isinstance(e["duration_ms"], int) for e in events)
        assert not any(e["name"] == "list_events" for e in events)  # listing the trail does not pollute it
        mine = call(client, "list_events", headers=VIEWER)["structuredContent"]["events"]
        assert mine and all(e["identity"] == "viewer" for e in mine)
        only_demo = call(client, "list_events", application="demo")["structuredContent"]["events"]
        assert only_demo and all(e["application"] == "demo" for e in only_demo)
        assert len(client.get("/events?limit=2", headers=MANAGER).json()["events"]) == 2
        assert len(client.get("/events?limit=0", headers=MANAGER).json()["events"]) == 1  # bounded below by one


def test_failed_login_bursts_are_recorded(tmp_path):
    with TestClient(build_app(two_identities(tmp_path, auth_failures_per_minute=2), FakeGitHub())) as client:
        codes = [client.get("/applications", headers={"Authorization": "Bearer wrong"}).status_code for _ in range(3)]
        assert codes == [401, 401, 429]
        events = client.get("/events", headers=MANAGER).json()["events"]
        burst = next(e for e in events if e["kind"] == "auth_burst")
        assert burst["outcome"] == "throttled" and burst["client"] == "testclient" and burst["detail"] == "/applications"


def test_sign_in_registration_and_token_reuse_are_recorded(tmp_path):
    with TestClient(build_app(make_config(tmp_path), FakeGitHub())) as client:
        registration = register(client).json()
        rejected, _, _ = sign_in(client, registration, token="wrong")
        assert rejected.status_code == 401
        registration, issued = tokens_for(client)
        rotated = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": issued["refresh_token"], "client_id": registration["client_id"]})
        assert rotated.status_code == 200
        reuse = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": issued["refresh_token"], "client_id": registration["client_id"]})
        assert reuse.status_code == 400
        events = client.get("/events", headers={"Authorization": "Bearer test-token"}).json()["events"]
        seen = [(e["kind"], e["identity"], e["outcome"]) for e in events]
        assert ("client_registered", None, "ok") in seen
        assert ("sign_in", None, "rejected") in seen and ("sign_in", "manager", "ok") in seen
        assert ("token_reuse", "manager", "session_revoked") in seen
        assert next(e for e in events if e["kind"] == "sign_in" and e["outcome"] == "ok")["name"] == "Claude"


def test_log_file_is_attached_once_and_receives_lines(tmp_path):
    path = tmp_path / "controller.log"
    attach_log_file(str(path))
    attach_log_file(str(path))
    for name in LOGGERS:
        assert sum(1 for h in logging.getLogger(name).handlers if getattr(h, "baseFilename", None) == str(path)) == 1
    logging.getLogger("labdeploy").warning("credential expires soon")
    for handler in logging.getLogger("labdeploy").handlers:
        handler.flush()
    assert "credential expires soon" in path.read_text()
    assert Config.model_validate({**two_identities(tmp_path).model_dump(), "log_file": str(path)}).log_file == str(path)
