"""The machine session: the gcloud command, the text it sends, what it reads back, and IAP's passing refusals."""
import base64

import pytest
from delivery_fakes import IMAGE, PROD_SA, Runner, release_dict

from appdelivery import machine
from appdelivery.errors import DeliveryError, ErrorCode
from appdelivery.machine import Machine, MachineSession
from appdelivery.models import ReleaseRequest
from appdelivery.remote import Completed

WEB1 = Machine("shop-web-1", "us-central1-a")
HEALTHY = Completed(0, f"Pulling...\nAPPDELIVERY result=healthy image={IMAGE} release=rel-000000000002 previous=\n", "WARNING: impersonating\n")
NOT_AUTHORIZED = Completed(255, "", "ERROR: [0] Error during local connection to [stdin]: Error while connecting [4033: 'not authorized'].\n")
NO_BACKEND = Completed(255, "", "Error while connecting [4003: 'failed to connect to backend']. (Failed to connect to port 22)\n")


class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def session(*answers):
    runner, clock = Runner(*answers), Clock()
    return MachineSession("infra-project", PROD_SA, runner=runner, sleep=clock.sleep, clock=clock), runner, clock


def release():
    return ReleaseRequest.model_validate(release_dict(health={"path": "/healthz", "timeout_seconds": 90}))


def decoded_inputs(stdin):
    head, _, _ = stdin.partition("\n# appdelivery's host script")
    return {line.split("=", 1)[0]: base64.b64decode(line.split("=", 1)[1]).decode() for line in head.splitlines()}


def test_a_deploy_runs_the_script_through_iap_as_the_environment_and_sends_everything_on_stdin():
    machines, runner, _ = session(HEALTHY)
    report = machines.deploy(WEB1, "rel-000000000002", release(), "DB_PASSWORD=s3cr3t\n", "ya29.token")
    assert report.result == "healthy" and report.fields["image"] == IMAGE and report.attempts == 1
    call = runner.calls[0]
    assert call["argv"] == ["gcloud", "compute", "ssh", "shop-web-1", "--zone", "us-central1-a", "--project", "infra-project", "--tunnel-through-iap",
                            "--impersonate-service-account", PROD_SA, "--quiet", "--strict-host-key-checking=no", "--ssh-key-expire-after=15m", "--command", "sudo bash -s"]
    assert call["timeout"] == machine.BASE_TIMEOUT + 90
    assert "s3cr3t" not in " ".join(call["argv"]) and "ya29" not in " ".join(call["argv"])
    assert decoded_inputs(call["stdin"]) == {"IN_ACTION": "deploy", "IN_RELEASE": "rel-000000000002", "IN_IMAGE": IMAGE, "IN_PORT": "8080", "IN_HEALTH_PATH": "/healthz",
                                             "IN_TIMEOUT": "90", "IN_REGISTRY": "us-central1-docker.pkg.dev", "IN_ENV": "DB_PASSWORD=s3cr3t\n", "IN_TOKEN": "ya29.token"}
    assert call["stdin"].endswith(machine.script())
    assert "s3cr3t" not in call["stdin"] and "ya29" not in call["stdin"], "values travel base64-encoded"


def test_without_a_token_no_login_input_is_sent():
    machines, runner, _ = session(HEALTHY)
    machines.deploy(WEB1, "rel-000000000002", release(), "", None)
    assert "IN_TOKEN" not in decoded_inputs(runner.calls[0]["stdin"])


def test_a_changed_script_is_refused_before_anything_is_sent(monkeypatch):
    monkeypatch.setattr(machine, "SCRIPT_SHA256", "0" * 64)
    machines, runner, _ = session(HEALTHY)
    with pytest.raises(DeliveryError) as caught:
        machines.status(WEB1)
    assert caught.value.code == ErrorCode.INTERNAL_ERROR and runner.calls == []


def test_the_recorded_hash_is_the_scripts():
    assert machine.script()  # raises when the file and machine.SCRIPT_SHA256 disagree


def test_unknown_actions_inputs_and_release_ids_never_reach_a_machine():
    with pytest.raises(ValueError):
        machine.compose("format", {})
    with pytest.raises(ValueError):
        machine.compose("deploy", {"COMMAND": "id"})
    machines, runner, _ = session(HEALTHY)
    with pytest.raises(ValueError):
        machines.deploy(WEB1, "rel-$(id)", release(), "", None)
    assert runner.calls == []


def test_the_report_is_the_last_marked_line_and_its_log_is_decoded():
    log = base64.b64encode(b"panic: no DATABASE_URL\n").decode()
    report = machine.parse(f"noise\nAPPDELIVERY result=status image=x\nAPPDELIVERY result=unhealthy restored=yes image={IMAGE} log={log}\n")
    assert report.result == "unhealthy" and report.fields["restored"] == "yes" and report.log() == "panic: no DATABASE_URL\n"
    assert machine.parse("no report here") is None


def test_a_grant_that_has_not_reached_iap_yet_is_waited_for():
    machines, runner, clock = session(NOT_AUTHORIZED, NOT_AUTHORIZED, HEALTHY)
    report = machines.status(WEB1)
    assert report.attempts == 3 and clock.slept == [30, 30] and len(runner.calls) == 3


def test_iap_is_given_ten_minutes_after_a_grant_and_then_the_reason_is_said():
    machines, runner, clock = session(NOT_AUTHORIZED)
    with pytest.raises(DeliveryError) as caught:
        machines.status(WEB1)
    assert caught.value.code == ErrorCode.MACHINE_UNREACHABLE and "grant takes minutes" in caught.value.message
    assert sum(clock.slept) == 600 and caught.value.details["attempts"] == 21
    assert "4033" in caught.value.details["said"] and "WARNING" not in caught.value.details["said"]


def test_a_machine_that_is_still_starting_gets_two_minutes():
    machines, _, clock = session(NO_BACKEND)
    with pytest.raises(DeliveryError) as caught:
        machines.status(WEB1)
    assert "starting or stopped" in caught.value.message and sum(clock.slept) == 120 and set(clock.slept) == {10}


def test_any_other_ssh_failure_is_reported_at_once():
    machines, runner, clock = session(Completed(255, "", "ERROR: (gcloud.compute.ssh) Could not fetch resource: The resource was not found\n"))
    with pytest.raises(DeliveryError) as caught:
        machines.status(WEB1)
    assert caught.value.code == ErrorCode.MACHINE_UNREACHABLE and clock.slept == [] and len(runner.calls) == 1
    assert "not found" in caught.value.details["said"]


def test_a_script_that_stops_is_a_machine_error_with_what_it_said():
    machines, _, _ = session(Completed(1, "", "docker: Error response from daemon: no space left on device\n"))
    with pytest.raises(DeliveryError) as caught:
        machines.status(WEB1)
    assert caught.value.code == ErrorCode.MACHINE_ERROR and "no space left" in caught.value.details["said"]


def test_a_session_that_ends_without_a_report_is_a_machine_error():
    machines, _, _ = session(Completed(0, "", ""))
    with pytest.raises(DeliveryError) as caught:
        machines.remove(WEB1)
    assert caught.value.code == ErrorCode.MACHINE_ERROR and "without a report" in caught.value.message


def test_rollback_sends_only_the_health_check():
    machines, runner, _ = session(Completed(0, "APPDELIVERY result=rolled_back image=x release=rel-000000000001\n", ""))
    assert machines.rollback(WEB1, release()).result == "rolled_back"
    assert decoded_inputs(runner.calls[0]["stdin"]) == {"IN_ACTION": "rollback", "IN_PORT": "8080", "IN_HEALTH_PATH": "/healthz", "IN_TIMEOUT": "90"}


def test_the_bounds_travel_to_the_host_as_inputs_like_everything_else():
    from appdelivery.config import WorkloadLimits
    machines, runner, _ = session(HEALTHY)
    machines.deploy(WEB1, "rel-000000000002", release(), "", None, WorkloadLimits(cpu_millicores=2500, memory_mb=4096, pids=1024))
    inputs = decoded_inputs(runner.calls[0]["stdin"])
    assert (inputs["IN_CPUS"], inputs["IN_MEMORY_MB"], inputs["IN_PIDS"]) == ("2.5", "4096", "1024")
