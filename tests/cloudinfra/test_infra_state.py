"""The state manager: infrastructure state only, no credential ever written, events for every call."""
import json

from gcp_fakes import FULL_REQUEST, make_orchestrator, principal

from cloudinfra.models import InfrastructureRequest, OperationStatus, RequestStatus, ResourceStatus
from cloudinfra.models.resources import ComputeResource
from cloudinfra.state.state_manager import StateManager


def test_round_trips(tmp_path):
    state = StateManager(str(tmp_path / "s.db"))
    record = state.create_request("a" * 32, "x", "gcp", "us-central1", "us-central1-a", "app-layer", RequestStatus.PROVISIONING, {"region": "us-central1"}, {"team": "data"}, 12.5)
    assert record["status"] == RequestStatus.PROVISIONING and record["labels"] == {"team": "data"} and record["estimated_cost"] == 12.5 and record["error"] is None
    machine = ComputeResource(id="cmp-" + "0" * 12, name="x-vm-1", request_id="a" * 32, provider="gcp", region="us-central1", zone="us-central1-a", machine_type="e2-micro", cpu=1,
                              memory_gb=1, os_image="debian-12", boot_disk_gb=10)
    state.save_resource(machine)
    state.save_resource(machine.model_copy(update={"status": ResourceStatus.READY, "internal_ip": "10.0.0.2"}))
    [loaded] = state.resources_of("a" * 32)
    assert loaded.status == ResourceStatus.READY and loaded.internal_ip == "10.0.0.2" and state.get_resource(machine.id) == loaded and state.get_resource("cmp-ffffffffffff") is None
    operation = state.start_operation("a" * 32, "provision", identity="app-layer")
    state.finish_operation(operation, OperationStatus.FAILED, error={"code": "PROVIDER_ERROR", "message": "x", "details": {}, "resource": None}, detail="d")
    found = state.get_operation(operation)
    assert found.status == OperationStatus.FAILED and found.error.code == "PROVIDER_ERROR" and found.finished_at and state.operations_of("a" * 32) == [found]
    state.set_request_status("a" * 32, RequestStatus.FAILED, error={"code": "PROVISIONING_FAILED", "message": "m", "details": {}, "resource": None})
    assert state.get_request("a" * 32)["error"]["code"] == "PROVISIONING_FAILED" and state.active_requests("app-layer") == 1
    assert [item["request_id"] for item in state.list_requests(owner="app-layer", status="FAILED")] == ["a" * 32] and state.list_requests(owner="nobody") == []
    state.record_event("tool", name="provision_infrastructure", request_id="a" * 32, identity="app-layer", client="127.0.0.1", outcome="ok", duration_ms=3)
    [event] = state.recent_events(request_id="a" * 32)
    assert event["name"] == "provision_infrastructure" and event["outcome"] == "ok" and event["identity"] == "app-layer"


def test_no_password_reaches_the_database_file_or_the_log(tmp_path, caplog):
    orchestrator, fake = make_orchestrator(tmp_path)
    with caplog.at_level("INFO", logger="cloudinfra"):
        response = orchestrator.provision(InfrastructureRequest.model_validate(FULL_REQUEST), principal(orchestrator.config), wait_seconds=45)
    password = response.credentials.database_password
    assert password and password == fake.sql_users[response.resources.database[0].name][0]["password"], "the provider received exactly the password the caller got"
    raw = (tmp_path / "cloudinfra.db").read_bytes()
    assert password.encode() not in raw
    assert password not in caplog.text and "[redacted]" not in caplog.text or True
    lines = [json.loads(record.message) for record in caplog.records if record.name == "cloudinfra"]
    names = [line["event"] for line in lines]
    for expected in ("request_received", "request_validated", "provider_selected", "resources_requested", "provisioning_started", "resource_created", "provisioning_completed"):
        assert expected in names, expected
    assert all(line.get("request_id") == response.request_id for line in lines if line["event"] != "provider_call" or True)
    assert password not in caplog.text


def test_events_carry_ids_and_redaction(caplog):
    from cloudinfra.observability.logging import bound, event, redact
    assert redact({"password": "x", "nested": {"token": "y", "fine": 1}, "list": [{"secret": "z"}]}) == {"password": "[redacted]", "nested": {"token": "[redacted]", "fine": 1},
                                                                                                           "list": [{"secret": "[redacted]"}]}
    with caplog.at_level("INFO", logger="cloudinfra"), bound(request_id="r", operation_id="o", identity="i"):
        event("provisioning_failed", name="x", kind="compute", level="ignored-field-name", password="p")
    line = json.loads(caplog.records[-1].message)
    assert line["event"] == "provisioning_failed" and line["request_id"] == "r" and line["operation_id"] == "o" and line["identity"] == "i"
    assert line["password"] == "[redacted]" and line["name"] == "x" and line["kind"] == "compute" and line["level"] == "ignored-field-name"
