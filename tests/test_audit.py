"""Phase 1: the audit trail records tool calls, REST calls, sign-ins, throttling and token reuse. Since 1.29.2 each identity
reads the events the settings page's Activity view gives it."""
import hashlib
import logging

from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient
from test_oauth import make_config, register, sign_in, tokens_for

from labdeploy import chat
from labdeploy.config import Config
from labdeploy.controller import HIDDEN_EVENTS, Store
from labdeploy.server import LOGGERS, attach_log_file, build_app

MANAGER = {"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"}
VIEWER = {"Authorization": "Bearer view-token", "Accept": "application/json, text/event-stream"}
STUDENT = {"Authorization": "Bearer student-token", "Accept": "application/json, text/event-stream"}


def identities(tmp_path, **extra):
    """Two applications; a deployer of every application (manager, who reads the whole trail), a read-only identity for demo
    (viewer) and a deployer of demo alone (student)."""
    return Config.model_validate({
        "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET},
                         "other": {"repository": "lab/other", "service": "other", "target": {**TARGET, "service": "other"}}},
        "database": str(tmp_path / "db"),
        "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["*"], "can_deploy": True},
                       {"name": "viewer", "token_sha256": hashlib.sha256(b"view-token").hexdigest(), "applications": ["demo"]},
                       {"name": "student", "token_sha256": hashlib.sha256(b"student-token").hexdigest(), "applications": ["demo"], "can_deploy": True}],
        **extra})


def call(client, name, headers=MANAGER, **arguments):
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}).json()["result"]


def test_tool_and_rest_calls_are_recorded_with_identity_subject_and_outcome(tmp_path):
    with TestClient(build_app(identities(tmp_path), FakeGitHub())) as client:
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
    with TestClient(build_app(identities(tmp_path, auth_failures_per_minute=2), FakeGitHub())) as client:
        codes = [client.get("/applications", headers={"Authorization": "Bearer wrong"}).status_code for _ in range(3)]
        assert codes == [401, 401, 429]
        events = client.get("/events", headers=MANAGER).json()["events"]
        burst = next(e for e in events if e["kind"] == "auth_burst")
        assert burst["outcome"] == "throttled" and burst["client"] == "testclient" and burst["detail"] == "/applications"


def test_sign_in_registration_and_token_reuse_are_recorded(tmp_path):
    # Its one identity is given every application: a deployer of all of them reads the whole trail, the events of no identity included.
    config = make_config(tmp_path)
    config = Config.model_validate({**config.model_dump(), "principals": [{**config.principals[0].model_dump(), "applications": ["*"]}]})
    with TestClient(build_app(config, FakeGitHub())) as client:
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


def busy(client, config):
    """A trail with some of everything: each identity's calls about demo, about other and about no application, the
    controller's own events about each application, and a failed-login burst, which is no identity's."""
    call(client, "list_applications")
    call(client, "create_deployment_plan", application="other", cloud="aws")
    call(client, "create_deployment_plan", application="demo", cloud="aws")
    client.get("/applications", headers=VIEWER)
    call(client, "create_deployment_plan", headers=VIEWER, application="demo")
    call(client, "list_applications", headers=STUDENT)
    client.get("/plans", headers=STUDENT)
    call(client, "create_deployment_plan", headers=STUDENT, application="demo", cloud="aws")
    store = Store(config.database)
    store.record_event("auto_deploy", application="demo", outcome="up_to_date")
    store.record_event("auto_deploy", application="other", outcome="released")
    assert [client.get("/plans", headers={"Authorization": "Bearer wrong"}).status_code for _ in range(2)] == [401, 429]


def trail(config, identity=None):
    """The audit trail read straight from the database, newest first, as list_events gave it before 1.29.2: every event to a
    deployer, its own to a read-only identity."""
    query, values = "SELECT at, identity, client, kind, name, application, outcome, duration_ms, detail FROM events", ()
    if identity is not None:
        query, values = query + " WHERE identity=?", (identity,)
    with Store(config.database).connect() as db:
        rows = db.execute(query + " ORDER BY rowid DESC LIMIT 50", values).fetchall()
    keys = ("at", "identity", "client", "kind", "name", "application", "outcome", "duration_ms", "detail")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def test_a_deployer_of_some_applications_gets_the_events_about_them_and_its_own(tmp_path):
    """list_events and GET /events give each identity the events the Activity view gives it (Controller.event_scope). A deployer
    of some applications gets the events about them, whoever made them, and its own; never an event about another
    application, nor another identity's or no identity's event about none. One line says what it does not see."""
    config = identities(tmp_path, auth_failures_per_minute=1)
    with TestClient(build_app(config, FakeGitHub())) as client:
        busy(client, config)
        answer = call(client, "list_events", headers=STUDENT)["structuredContent"]
        assert {(e["kind"], e["name"], e["identity"], e["application"]) for e in answer["events"]} == {
            ("tool", "create_deployment_plan", "manager", "demo"), ("tool", "create_deployment_plan", "viewer", "demo"), ("auto_deploy", None, None, "demo"),
            ("tool", "list_applications", "student", None), ("rest", "plans", "student", None), ("tool", "create_deployment_plan", "student", "demo")}
        assert answer["hidden"] == HIDDEN_EVENTS and f"- {HIDDEN_EVENTS.rstrip('.')}\n" in answer["summary"]
        assert answer["summary"].startswith("**6 events**, 2 errors, from 4 identities:")
        assert client.get("/events", headers=STUDENT).json() == {"events": answer["events"], "hidden": HIDDEN_EVENTS}
        other = call(client, "list_events", headers=STUDENT, application="other")["structuredContent"]
        assert other["events"] == [] and other["summary"].startswith("**No events recorded** for this question.")
        assert f"- {HIDDEN_EVENTS.rstrip('.')}\n" in other["summary"] and client.get("/events?application=other", headers=STUDENT).json()["events"] == []
        assert call(client, "create_deployment_plan", headers=STUDENT, application="other")["isError"]  # its own attempt is its own event
        mine = call(client, "list_events", headers=STUDENT, application="other")["structuredContent"]["events"]
        assert [(e["identity"], e["application"], e["outcome"]) for e in mine] == [("student", "other", "error")]


def test_a_deployer_of_every_application_and_a_read_only_identity_get_what_they_got(tmp_path):
    """A deployer of every application gets every event and a read-only identity its own, as before 1.29.2: the same events in
    the same order, the same summary and no line about what is hidden, through list_events and GET /events alike."""
    config = identities(tmp_path, auth_failures_per_minute=1)
    with TestClient(build_app(config, FakeGitHub())) as client:
        busy(client, config)
        every, own = trail(config), trail(config, "viewer")
        assert {e["application"] for e in every} == {"demo", "other", None} and any(e["kind"] == "auth_burst" for e in every)
        assert [(e["kind"], e["application"]) for e in own] == [("tool", "demo"), ("rest", None)]
        for headers, expected in ((MANAGER, every), (VIEWER, own)):
            assert call(client, "list_events", headers=headers)["structuredContent"] == {"summary": chat.events({"events": expected}), "events": expected}
            assert client.get("/events", headers=headers).json() == {"events": expected}
        about_other = [e for e in every if e["application"] == "other"]
        assert len(about_other) == 2 and call(client, "list_events", application="other")["structuredContent"]["events"] == about_other


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
    assert Config.model_validate({**identities(tmp_path).model_dump(), "log_file": str(path)}).log_file == str(path)
