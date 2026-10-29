"""Release A: what a developer needs to use what was created. Access grants with connect blocks, operator software profiles,
credentials in secrets the request owns, stop and start, idle stop, maximum run time, expiry with warnings and retention,
and the GPU boot disk minimum. Every test runs against the fake Google Cloud; nothing here touches a cloud."""
import base64
import datetime
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pydantic
import pytest
from fastapi.testclient import TestClient
from gcp_fakes import PROJECT, TOKEN, VM_REQUEST, config_dict, make_orchestrator, make_provider, principal

from cloudinfra.config import Config
from cloudinfra.mcp.server import build_app
from cloudinfra.models import InfrastructureRequest, RequestStatus, ResourceStatus
from cloudinfra.models.errors import ErrorCode, InfrastructureError, problems_from
from cloudinfra.orchestration.provisioner import InlineExecutor
from cloudinfra.orchestration.sweeper import SWEEPER, Sweeper
from cloudinfra.providers import ProviderRegistry
from cloudinfra.providers.gcp.iam import IAP_TUNNEL

START = datetime.datetime(2026, 10, 28, 12, 0, tzinfo=datetime.UTC)
PROFILES = {"gpu-signed": {"image": "projects/platform-images/global/images/family/gpu-signed-cu129", "min_boot_disk_gb": 100, "accelerator": "required",
                           "description": "Ubuntu 24.04 with signed NVIDIA drivers and CUDA"},
            "hardened": {"image": "projects/platform-images/global/images/family/hardened-ubuntu", "accelerator": "none"}}
GPU_GROUP = {"cpu": 8, "memory_gb": 32, "accelerator": {"type": "l4"}}
DEV = "user:dev@example.com"
DATA = "group:data@example.com"


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += datetime.timedelta(**delta)


def configured(tmp_path, identity=DEV, domains=("example.com",), limits=None, gcp=None, **overrides):
    data = config_dict(**overrides)
    tmp_path.mkdir(parents=True, exist_ok=True)
    data["database"] = str(tmp_path / "cloudinfra.db")
    data["principals"][0]["identity"] = identity
    data["access"] = {"allowed_domains": list(domains), **data.get("access", {})}
    data["limits"] = {"max_monthly_cost_usd": 5000.0, **(limits or {})}
    data["providers"]["gcp"].update(gcp or {})
    return Config.model_validate(data)


def orchestrated(tmp_path, **settings):
    orchestrator, fake = make_orchestrator(tmp_path, config=configured(tmp_path, **settings))
    clock = Clock()
    orchestrator.wallclock = clock
    return orchestrator, fake, clock, principal(orchestrator.config)


def provision(orchestrator, who, request):
    response = orchestrator.provision(InfrastructureRequest.model_validate({"region": "us-central1", **request}), who, wait_seconds=45)
    assert response.error is None, response.error
    return response


def refused(code, fn, *args, **kwargs):
    with pytest.raises(InfrastructureError) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code, caught.value.message
    return caught.value


# ---- the contract
def test_gpu_boot_disk_defaults_to_fifty_and_refuses_less_than_forty():
    def compute(**fields):
        return InfrastructureRequest.model_validate({"region": "us-central1", "compute": {**GPU_GROUP, **fields}}).compute
    assert compute().boot_disk_gb == 50 and compute(boot_disk_gb=40).boot_disk_gb == 40
    with pytest.raises(pydantic.ValidationError) as caught:
        compute(boot_disk_gb=30)
    [problem] = problems_from(caught.value)
    assert problem.code == ErrorCode.INVALID_COMPUTE_CONFIGURATION and "at least 40 GB" in problem.message
    assert InfrastructureRequest.model_validate({"region": "us-central1", "compute": {"cpu": 2, "memory_gb": 4}}).compute.boot_disk_gb == 20


def test_contract_rules_for_profiles_access_secrets_and_time():
    def problems(request):
        with pytest.raises(pydantic.ValidationError) as caught:
            InfrastructureRequest.model_validate({"region": "us-central1", **request})
        return [(item.code, item.message) for item in problems_from(caught.value)]
    [(code, message)] = problems({"compute": {"cpu": 2, "memory_gb": 4, "os": "debian", "profile": "hardened"}})
    assert code == ErrorCode.INVALID_COMPUTE_CONFIGURATION and "profile or os" in message
    [(code, message)] = problems({"compute": {"cpu": 2, "memory_gb": 4}, "access": [{"member": "dev@example.com"}]})
    assert code == ErrorCode.INVALID_ACCESS and "user:, group: or serviceAccount:" in message
    [(code, _)] = problems({"compute": {"cpu": 2, "memory_gb": 4}, "access": [{"member": DEV}, {"member": DEV, "level": "admin"}]})
    assert code == ErrorCode.INVALID_ACCESS
    [(code, message)] = problems({"database": {"public_access": True}, "secret": {"name": "db-password"}})
    assert code == ErrorCode.INVALID_REQUEST and "the secret the service makes" in message
    assert problems({"compute": {"cpu": 2, "memory_gb": 4}, "lifetime": {"hours": 0}})[0][0] == ErrorCode.INVALID_REQUEST
    assert problems({"compute": {"cpu": 2, "memory_gb": 4, "autostop": {"idle_minutes": 5}}})[0][0] == ErrorCode.INVALID_REQUEST
    assert problems({"compute": {"cpu": 2, "memory_gb": 4, "max_run_hours": 3000}})[0][0] == ErrorCode.INVALID_REQUEST
    fine = InfrastructureRequest.model_validate({"region": "us-central1", "compute": {"cpu": 2, "memory_gb": 4, "profile": "hardened", "autostop": {"idle_minutes": 30},
                                                                                      "max_run_hours": 12},
                                                 "lifetime": {"hours": 72, "on_expiry": "destroy"}, "access": [{"member": DATA, "level": "admin"}]})
    assert fine.compute.profile == "hardened" and fine.lifetime.on_expiry == "destroy" and fine.access[0].level == "admin"


def test_operator_settings_are_checked():
    data = config_dict()
    data["providers"]["gcp"].update({"profiles": PROFILES, "default_accelerator_profile": "hardened"})
    with pytest.raises(pydantic.ValidationError, match="refuses accelerators"):
        Config.model_validate(data)
    data["providers"]["gcp"].update({"profiles": {"x": {"image": "ubuntu-latest"}}, "default_accelerator_profile": None})
    with pytest.raises(pydantic.ValidationError):
        Config.model_validate(data)
    with pytest.raises(pydantic.ValidationError, match="cannot exceed"):
        Config.model_validate({**config_dict(), "limits": {"default_lifetime_hours": 100, "max_lifetime_hours": 50}})
    with pytest.raises(pydantic.ValidationError):
        Config.model_validate({**config_dict(), "principals": [{**config_dict()["principals"][0], "identity": "dev@example.com"}]})


# ---- software profiles
def test_gpu_machines_get_the_operators_signed_driver_profile_with_secure_boot(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path, gcp={"profiles": PROFILES, "default_accelerator_profile": "gpu-signed"})
    plan = orchestrator.plan(InfrastructureRequest.model_validate({"region": "us-central1", "compute": [{"group": "gpu", **GPU_GROUP}, {"group": "web", "cpu": 2, "memory_gb": 4}]}), who)
    gpu, web = (next(item.spec for item in plan.planned if item.spec.get("group") == name) for name in ("gpu", "web"))
    assert gpu["profile"] == "gpu-signed" and gpu["secure_boot"] is True and gpu["boot_disk_gb"] == 100 and gpu["image"].endswith("family/gpu-signed-cu129")
    assert web["profile"] is None and web["secure_boot"] is True and web["image"].startswith("projects/ubuntu-os-cloud/")
    text = " ".join(plan.warnings)
    assert "drivers come with profile gpu-signed" in text and "the boot disk is 100 GB, the minimum of profile gpu-signed, not the 50 GB asked" in text
    response = provision(orchestrator, who, {"compute": GPU_GROUP})
    body = next(call[2] for call in fake.calls if call[0] == "POST" and call[1].endswith("/instances"))
    assert body["shieldedInstanceConfig"]["enableSecureBoot"] is True and body["disks"][0]["initializeParams"]["diskSizeGb"] == "100"
    assert body["disks"][0]["initializeParams"]["sourceImage"] == "projects/platform-images/global/images/family/gpu-signed-cu129"
    [machine] = response.resources.compute
    assert machine.profile == "gpu-signed" and machine.secure_boot is True and machine.boot_disk_gb == 100
    kinds = orchestrator.resource_kinds()
    assert [(item.name, item.accelerator) for item in kinds.profiles["gcp"]] == [("gpu-signed", "required"), ("hardened", "none")]
    assert orchestrator.service_status("x")["profiles"]["gcp"][0]["name"] == "gpu-signed"


def test_a_plain_image_on_a_gpu_machine_turns_secure_boot_off_and_says_so(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path, gcp={"profiles": PROFILES})
    response = provision(orchestrator, who, {"compute": {**GPU_GROUP, "os": "ubuntu"}})
    [machine] = response.resources.compute
    assert machine.profile is None and machine.secure_boot is False and machine.boot_disk_gb == 50
    assert any("the image has no GPU driver" in warning for warning in response.warnings)
    assert any("plain image without GPU drivers" in note for note in machine.connect.notes)


def test_profiles_are_checked_against_the_operators_list(tmp_path):
    orchestrator, _, _, who = orchestrated(tmp_path, gcp={"profiles": PROFILES})

    def problem(compute):
        found = orchestrator.validate(InfrastructureRequest.model_validate({"region": "us-central1", "compute": compute}), who)
        return found.problems[0]
    unknown = problem({"cpu": 2, "memory_gb": 4, "profile": "gpu"})
    assert unknown.code == ErrorCode.INVALID_COMPUTE_CONFIGURATION and "profiles: gpu-signed, hardened" in unknown.message
    assert "for machines with GPUs" in problem({"cpu": 2, "memory_gb": 4, "profile": "gpu-signed"}).message
    assert "not for machines with GPUs" in problem({**GPU_GROUP, "profile": "hardened"}).message


def test_maximum_run_time_is_googles_own_and_survives_a_restart(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"compute": {**GPU_GROUP, "max_run_hours": 12}})
    body = next(call[2] for call in fake.calls if call[0] == "POST" and call[1].endswith("/instances"))
    assert body["scheduling"] == {**body["scheduling"], "maxRunDuration": {"seconds": "43200"}, "instanceTerminationAction": "STOP", "onHostMaintenance": "TERMINATE"}
    assert response.resources.compute[0].max_run_hours == 12
    assert any("stops after 12 hours of running" in warning for warning in response.warnings)


# ---- credentials in secrets the request owns
def test_the_password_is_returned_only_when_the_operator_allows_it(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path, return_database_password=True)
    response = provision(orchestrator, who, {"database": {"public_access": True}})
    password = fake.sql_users["infra-" + response.request_id[:8] + "-db-" + response.request_id[:6]][0]["password"]
    assert response.credentials.database_password == password and response.credentials.database_password_secret
    assert orchestrator.status(response.request_id, who).credentials is None
    raw = (tmp_path / "cloudinfra.db").read_bytes()
    assert password.encode() not in raw


def test_connect_blocks_name_secrets_never_their_values(tmp_path):
    orchestrator, _, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"network": {}, "database": {}, "cache": {}, "compute": {"cpu": 2, "memory_gb": 4}})
    [database], [cache], [machine] = response.resources.database, response.resources.cache, response.resources.compute
    secrets = {secret.id: secret for secret in response.resources.secret}
    assert database.connect.commands["password"] == f"gcloud secrets versions access latest --secret={secrets[database.password_secret].name} --project {PROJECT}"
    assert cache.connect.commands["auth"].endswith(f"--secret={secrets[cache.auth_secret].name} --project {PROJECT}") and cache.server_ca_cert.startswith("-----BEGIN")
    assert machine.connect.commands["ssh"] == f"gcloud compute ssh {machine.name} --zone {machine.zone} --project {PROJECT} --tunnel-through-iap"
    assert any("compute.projects.get" in item for item in machine.connect.prerequisites)
    text = json.dumps(response.model_dump(mode="json"))
    assert "fake-redis-auth-string" not in text


# ---- access
def test_requester_and_members_are_granted_on_each_resource_never_on_the_project(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"compute": {"cpu": 2, "memory_gb": 4}, "storage": {"type": "object", "size_gb": 10}, "queue": {"name": "jobs"},
                                             "secret": {"name": "api-key"}, "access": [{"member": DATA}]})
    assert [(item.member, item.level, item.status) for item in response.access] == [(DEV, "admin", "GRANTED"), (DATA, "use", "GRANTED")]
    [machine], [bucket], [queue], [secret] = response.resources.compute, response.resources.storage, response.resources.queue, response.resources.secret
    assert sorted(response.access[0].resources) == sorted([machine.id, bucket.id, queue.id, secret.id])
    assert fake.members(("instance", machine.zone, machine.name), "roles/compute.osAdminLogin") == [DEV]
    assert fake.members(("instance", machine.zone, machine.name), "roles/compute.osLogin") == [DATA]
    assert fake.members(("tunnel", machine.zone, machine.instance_id), IAP_TUNNEL) == [DEV, DATA] and machine.instance_id.isdigit()
    assert fake.members(("secret", secret.name), "roles/secretmanager.secretVersionAdder") == [DEV]
    assert fake.members(("secret", secret.name), "roles/secretmanager.secretAccessor") == [DEV, DATA]
    assert fake.members(("bucket", bucket.name), "roles/storage.objectAdmin") == [DEV] and fake.members(("bucket", bucket.name), "roles/storage.objectUser") == [DATA]
    assert fake.members(("topics", queue.name), "roles/pubsub.publisher") == [DEV, DATA] and fake.members(("subscriptions", queue.name), "roles/pubsub.subscriber") == [DEV, DATA]
    assert {(grant.member, grant.role) for grant in machine.access} == {(DEV, "roles/compute.osAdminLogin"), (DEV, IAP_TUNNEL), (DATA, "roles/compute.osLogin"), (DATA, IAP_TUNNEL)}
    assert not any("cloudresourcemanager" in call[1] and "IamPolicy" in call[1] for call in fake.calls), "no project-level policy is read or written"
    assert not any("Nobody has been granted" in item for item in machine.connect.prerequisites)


def test_members_outside_the_allowed_domains_are_refused(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    error = refused(ErrorCode.INVALID_ACCESS, orchestrator.provision, InfrastructureRequest.model_validate({**VM_REQUEST, "access": [{"member": "user:someone@gmail.com"}]}), who)
    assert "outside the domains this service grants to: example.com" in error.message and fake.calls == []
    found = orchestrator.validate(InfrastructureRequest.model_validate({**VM_REQUEST, "access": [{"member": "user:someone@gmail.com"}]}), who)
    assert not found.valid and found.problems[0].code == ErrorCode.INVALID_ACCESS
    strict, _, _, who = orchestrated(tmp_path / "strict", domains=())
    error = refused(ErrorCode.INVALID_ACCESS, strict.provision, InfrastructureRequest.model_validate({**VM_REQUEST, "access": [{"member": DATA}]}), who)
    assert "not the identity of one of this service's principals" in error.message
    small, _, _, who = orchestrated(tmp_path / "small", access={"max_members": 1})
    error = refused(ErrorCode.INVALID_ACCESS, small.provision, InfrastructureRequest.model_validate({**VM_REQUEST, "access": [{"member": DATA}]}), who)
    assert "At most 1 members per request, the requester included; 2 asked" in error.message


def test_access_is_granted_changed_and_revoked_later(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"compute": {"cpu": 2, "memory_gb": 4}, "secret": {"name": "api-key"}})
    [machine], [secret] = response.resources.compute, response.resources.secret
    granted = orchestrator.manage_access(response.request_id, "grant", DATA, who, level="admin")
    assert next(item for item in granted.access if item.member == DATA).status == "GRANTED"
    assert DATA in fake.members(("instance", machine.zone, machine.name), "roles/compute.osAdminLogin")
    lowered = orchestrator.manage_access(response.request_id, "grant", DATA, who, level="use")
    assert next(item for item in lowered.access if item.member == DATA).level == "use"
    assert DATA not in fake.members(("instance", machine.zone, machine.name), "roles/compute.osAdminLogin"), "a lower level takes the admin role away"
    assert DATA not in fake.members(("secret", secret.name), "roles/secretmanager.secretVersionAdder")
    assert DATA in fake.members(("instance", machine.zone, machine.name), "roles/compute.osLogin")
    revoked = orchestrator.manage_access(response.request_id, "revoke", DATA, who)
    entry = next(item for item in revoked.access if item.member == DATA)
    assert entry.status == "REVOKED" and entry.resources == []
    assert DATA not in fake.members(("tunnel", machine.zone, machine.instance_id), IAP_TUNNEL) and DATA not in fake.members(("secret", secret.name), "roles/secretmanager.secretAccessor")
    assert DEV in fake.members(("tunnel", machine.zone, machine.instance_id), IAP_TUNNEL), "another member is never touched"
    assert [operation.kind for operation in revoked.operations] == ["provision", "grant_access", "grant_access", "revoke_access"]
    refused(ErrorCode.NOT_FOUND, orchestrator.manage_access, response.request_id, "revoke", DATA, who)
    refused(ErrorCode.INVALID_ACCESS, orchestrator.manage_access, response.request_id, "grant", "user:x@other.org", who)
    refused(ErrorCode.INVALID_ACCESS, orchestrator.manage_access, response.request_id, "grant", "x@example.com", who)
    refused(ErrorCode.PERMISSION_DENIED, orchestrator.manage_access, response.request_id, "grant", DATA, principal(orchestrator.config, "reader"))


def test_a_grant_that_fails_is_the_members_not_the_requests(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    fake.fail_on.append(("POST", ":setIamPolicy"))  # the tunnel's and the secret's policies refuse; the machine's own takes the grant
    response = orchestrator.provision(InfrastructureRequest.model_validate({**VM_REQUEST, "secret": {"name": "api-key"}}), who, wait_seconds=45)
    [entry] = response.access
    assert response.status == RequestStatus.READY and entry.status == "FAILED" and entry.error.code == ErrorCode.PROVIDER_ERROR and entry.resources == []
    [machine] = response.resources.compute
    assert fake.members(("instance", machine.zone, machine.name), "roles/compute.osAdminLogin") == [DEV], "the grant reached the machine's policy before failing"
    fake.fail_on.clear()
    revoked = orchestrator.manage_access(response.request_id, "revoke", DEV, who)
    assert revoked.access[0].status == "REVOKED" and fake.members(("instance", machine.zone, machine.name), "roles/compute.osAdminLogin") == [], "a removal finds it"
    again = orchestrator.manage_access(response.request_id, "grant", DEV, who, level="admin")
    assert again.access[0].status == "GRANTED" and again.access[0].error is None and len(again.access[0].resources) == 2


def test_destroy_takes_members_out_of_the_tunnel_policy_that_outlives_the_machine(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"compute": {"cpu": 2, "memory_gb": 4}, "access": [{"member": DATA}]})
    [machine] = response.resources.compute
    destroyed = orchestrator.destroy(response.request_id, who, wait_seconds=45)
    assert destroyed.status == RequestStatus.DESTROYED and fake.members(("tunnel", machine.zone, machine.instance_id), IAP_TUNNEL) == []
    assert {item.status for item in destroyed.access} == {"REVOKED"} and destroyed.resources.compute[0].access == []


def test_without_an_identity_nobody_is_granted_and_the_connect_block_says_so(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path, identity=None)
    response = provision(orchestrator, who, VM_REQUEST)
    assert response.access == [] and not fake.policies
    assert any("Nobody has been granted access yet" in item for item in response.resources.compute[0].connect.prerequisites)


# ---- stop and start
def test_stop_and_start_a_machine_and_its_database_in_order(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"network": {}, "compute": {"cpu": 2, "memory_gb": 4}, "database": {}, "queue": {"name": "jobs"}})
    running = orchestrator.cost(who, request_id=response.request_id)
    fake.calls.clear()
    stopped = orchestrator.control(response.request_id, "stop", who, wait_seconds=45)
    assert stopped.status == RequestStatus.STOPPED and stopped.lifecycle.stop_reason == "requested" and stopped.lifecycle.stopped_at
    order = [("compute" if "/instances/" in call[1] and call[1].endswith("/stop") else "database") for call in fake.calls if call[0] in ("POST", "PATCH")]
    assert order == ["compute", "database"], "machines stop before their database"
    [machine], [database] = stopped.resources.compute, stopped.resources.database
    assert machine.status == ResourceStatus.STOPPED and machine.stop_reason == "requested" and database.status == ResourceStatus.STOPPED
    assert fake.sql[database.name]["settings"]["activationPolicy"] == "NEVER" and fake.instances[(machine.zone, machine.name)]["status"] == "TERMINATED"
    billing = " ".join(stopped.lifecycle.still_billing)
    assert "20 GB boot disk" in billing and "10 GB of storage" in billing and "a queue" not in billing
    cost = orchestrator.cost(who, request_id=response.request_id)
    assert cost["current_monthly_usd"] == cost["estimate"]["stopped_monthly_usd"] < running["current_monthly_usd"] == running["estimate"]["monthly_usd"]
    assert any("stopped" in note for note in machine.connect.notes)
    refused(ErrorCode.INVALID_STATE, orchestrator.control, response.request_id, "stop", who)
    fake.calls.clear()
    started = orchestrator.control(response.request_id, "start", who, wait_seconds=45)
    order = [("compute" if "/instances/" in call[1] and call[1].endswith("/start") else "database") for call in fake.calls if call[0] in ("POST", "PATCH")]
    assert order == ["database", "compute"], "a database starts before the machines that use it"
    assert started.status == RequestStatus.READY and started.lifecycle is None and started.resources.compute[0].stop_reason is None
    assert fake.sql[database.name]["settings"]["activationPolicy"] == "ALWAYS"
    refused(ErrorCode.INVALID_STATE, orchestrator.control, response.request_id, "start", who)
    assert [operation.kind for operation in started.operations] == ["provision", "stop", "start"]
    stopped = orchestrator.control(response.request_id, "stop", who, wait_seconds=45)
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED, "a stopped request is destroyed as it is"


def test_what_cannot_stop_is_refused_and_actions_are_checked(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    bucket = provision(orchestrator, who, {"storage": {"type": "object", "size_gb": 10}})
    error = refused(ErrorCode.INVALID_STATE, orchestrator.control, bucket.request_id, "stop", who)
    assert "the gcp adapter stops compute and database" in error.message
    refused(ErrorCode.INVALID_REQUEST, orchestrator.control, bucket.request_id, "pause", who)
    refused(ErrorCode.INVALID_REQUEST, orchestrator.control, bucket.request_id, "extend", who)
    refused(ErrorCode.INVALID_REQUEST, orchestrator.control, bucket.request_id, "stop", who, hours=2)
    refused(ErrorCode.INVALID_STATE, orchestrator.control, bucket.request_id, "extend", who, hours=2)  # no expiry to extend
    refused(ErrorCode.PERMISSION_DENIED, orchestrator.control, bucket.request_id, "stop", principal(orchestrator.config, "reader"))


def test_a_machine_that_failed_to_stop_leaves_the_request_ready_with_the_error(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, VM_REQUEST)
    fake.fail_on.append(("POST", "/stop"))
    after = orchestrator.control(response.request_id, "stop", who, wait_seconds=45)
    assert after.status == RequestStatus.READY and after.operations[-1].status == "FAILED" and after.operations[-1].error.code == ErrorCode.PROVIDER_ERROR
    assert "1 of 1 resources did not stop" in after.operations[-1].error.message


# ---- lifetime, warnings, expiry
def test_lifetime_warns_then_stops_then_destroys_after_retention(tmp_path, caplog):
    orchestrator, fake, clock, who = orchestrated(tmp_path)
    sweeper = Sweeper(orchestrator, 0)
    response = provision(orchestrator, who, {"compute": {"cpu": 2, "memory_gb": 4}, "lifetime": {"hours": 72}})
    assert response.lifecycle.expires_at == "2026-10-31T12:00:00Z" and response.lifecycle.expires_in_hours == 72.0 and response.lifecycle.on_expiry == "stop"
    assert any("expires 72 hours after it is created; then machines and databases stop, and everything is destroyed 7 days later" in item for item in response.warnings)
    assert orchestrator.list_requests(who, expiring_within_hours=80).count == 1 and orchestrator.list_requests(who, expiring_within_hours=10).count == 0
    assert sweeper.tick()["warned"] == []
    clock.advance(hours=30)
    with caplog.at_level("INFO", logger="cloudinfra"):
        assert sweeper.tick()["warned"] == [response.request_id]
    assert '"warning": "48h"' in caplog.text and sweeper.tick()["warned"] == [], "each warning once"
    status = orchestrator.status(response.request_id, who)
    assert status.lifecycle.warnings_sent == ["48h"] and any(item.startswith("Expires in 42.0 hours") for item in status.warnings)
    clock.advance(hours=41)
    assert sweeper.tick()["warned"] == [response.request_id] and orchestrator.status(response.request_id, who).lifecycle.warnings_sent == ["48h", "2h"]
    clock.advance(hours=2)
    assert sweeper.tick()["stopped"] == [response.request_id]
    expired = orchestrator.status(response.request_id, who)
    assert expired.status == RequestStatus.STOPPED and expired.lifecycle.stop_reason == "expired" and expired.lifecycle.retain_until == "2026-11-07T13:00:00Z"
    assert any(item.startswith("Expired at 2026-10-31T12:00:00Z; destroyed at 2026-11-07T13:00:00Z") for item in expired.warnings)
    assert expired.operations[-1].kind == "stop" and expired.operations[-1].identity == SWEEPER.name
    refused(ErrorCode.INVALID_STATE, orchestrator.control, response.request_id, "start", who)
    assert sweeper.tick() == {"warned": [], "stopped": [], "destroyed": [], "idle_stopped": [], "errors": []}
    clock.advance(days=7)
    assert sweeper.tick()["destroyed"] == [response.request_id]
    assert orchestrator.status(response.request_id, who).status == RequestStatus.DESTROYED and not fake.instances


def test_extend_keeps_an_expired_request_and_respects_the_maximum(tmp_path):
    orchestrator, fake, clock, who = orchestrated(tmp_path, limits={"max_lifetime_hours": 100})
    sweeper = Sweeper(orchestrator, 0)
    refused(ErrorCode.LIMIT_EXCEEDED, orchestrator.provision, InfrastructureRequest.model_validate({**VM_REQUEST, "lifetime": {"hours": 200}}), who)
    response = provision(orchestrator, who, {**VM_REQUEST, "lifetime": {"hours": 24}})
    assert response.lifecycle.max_expires_at == "2026-11-01T16:00:00Z"
    clock.advance(hours=25)
    sweeper.tick()
    assert orchestrator.status(response.request_id, who).status == RequestStatus.STOPPED
    error = refused(ErrorCode.LIMIT_EXCEEDED, orchestrator.control, response.request_id, "extend", who, hours=90)
    assert error.details["hours_left"] == 75
    extended = orchestrator.control(response.request_id, "extend", who, hours=24)
    assert extended.lifecycle.expires_at == "2026-10-30T13:00:00Z" and extended.lifecycle.retain_until is None and extended.lifecycle.warnings_sent == []
    started = orchestrator.control(response.request_id, "start", who, wait_seconds=45)
    assert started.status == RequestStatus.READY and started.lifecycle.stop_reason is None
    clock.advance(hours=25)
    assert sweeper.tick()["stopped"] == [response.request_id], "the new expiry stops it again"


def test_on_expiry_destroy_and_the_operators_default_lifetime(tmp_path):
    orchestrator, fake, clock, who = orchestrated(tmp_path, limits={"default_lifetime_hours": 8})
    sweeper = Sweeper(orchestrator, 0)
    defaulted = provision(orchestrator, who, VM_REQUEST)
    assert defaulted.lifecycle.expires_in_hours == 8.0 and any("(the operator's default lifetime)" in item for item in defaulted.warnings)
    doomed = provision(orchestrator, who, {**VM_REQUEST, "lifetime": {"hours": 4, "on_expiry": "destroy"}})
    clock.advance(hours=5)
    found = sweeper.tick()
    assert found["destroyed"] == [doomed.request_id] and found["warned"] == [defaulted.request_id]
    assert orchestrator.status(doomed.request_id, who).status == RequestStatus.DESTROYED
    nothing, _, _, who = orchestrated(tmp_path / "none")
    assert provision(nothing, who, VM_REQUEST).lifecycle is None, "nothing expires unless asked"


def test_a_request_that_cannot_stop_is_kept_then_destroyed_at_expiry(tmp_path):
    orchestrator, fake, clock, who = orchestrated(tmp_path, limits={"expiry_retention_days": 1})
    sweeper = Sweeper(orchestrator, 0)
    response = provision(orchestrator, who, {"queue": {"name": "jobs"}, "lifetime": {"hours": 2}})
    clock.advance(hours=3)
    assert sweeper.tick()["stopped"] == [response.request_id]
    marked = orchestrator.status(response.request_id, who)
    assert marked.status == RequestStatus.READY and marked.lifecycle.stop_reason == "expired" and marked.lifecycle.retain_until
    clock.advance(days=1)
    assert sweeper.tick()["destroyed"] == [response.request_id] and not fake.topics


# ---- idle stop
def test_idle_machines_stop_and_keep_awake_holds_them(tmp_path):
    orchestrator, fake, clock, who = orchestrated(tmp_path, limits={"default_autostop_idle_minutes": 30})
    sweeper = Sweeper(orchestrator, 0)
    response = provision(orchestrator, who, {"compute": [{"group": "gpu", **GPU_GROUP}, {"group": "web", "cpu": 2, "memory_gb": 4}]})
    gpu, web = response.resources.compute
    assert gpu.autostop_idle_minutes == 30 and web.autostop_idle_minutes is None, "the operator's default covers machines with GPUs"
    assert any("the operator's default stops these machines after 30 idle minutes" in item for item in response.warnings)
    fake.monitoring_value = 0.5  # half of 8 vCPUs: busy
    assert sweeper.tick()["idle_stopped"] == []
    fake.monitoring_value = 0.02  # 0.16 cores at the busiest: idle
    held = orchestrator.control(response.request_id, "keep_awake", who, hours=4)
    assert held.lifecycle.awake_until == "2026-10-28T16:00:00Z" and sweeper.tick()["idle_stopped"] == []
    refused(ErrorCode.LIMIT_EXCEEDED, orchestrator.control, response.request_id, "keep_awake", who, hours=100)
    clock.advance(hours=5)
    assert sweeper.tick()["idle_stopped"] == [gpu.id]
    after = orchestrator.status(response.request_id, who)
    stopped = next(item for item in after.resources.compute if item.id == gpu.id)
    assert after.status == RequestStatus.READY and stopped.status == ResourceStatus.STOPPED and stopped.stop_reason == "idle"
    assert after.operations[-1].kind == "stop_compute" and after.operations[-1].identity == SWEEPER.name
    assert next(item for item in after.resources.compute if item.id == web.id).status == ResourceStatus.READY
    restarted = orchestrator.control(response.request_id, "start", who, wait_seconds=45)
    assert all(item.status == ResourceStatus.READY for item in restarted.resources.compute)


def test_a_machine_is_not_idle_right_after_it_starts_or_without_samples(tmp_path):
    orchestrator, fake, clock, who = orchestrated(tmp_path)
    sweeper = Sweeper(orchestrator, 0)
    fake.started_at = "2026-10-28T11:50:00Z"
    response = provision(orchestrator, who, {"compute": {"cpu": 4, "memory_gb": 16, "autostop": {"idle_minutes": 30}}})
    fake.monitoring_value = 0.0
    assert sweeper.tick()["idle_stopped"] == [], "ten minutes after a start is not thirty idle minutes"
    clock.advance(minutes=30)
    original = fake.request

    def no_samples(method, url, json=None, params=None):
        return {"timeSeries": []} if "timeSeries" in url else original(method, url, json, params)
    fake.request = no_samples
    assert sweeper.tick()["idle_stopped"] == [], "no samples is not proof of idleness"
    fake.request = original
    assert sweeper.tick()["idle_stopped"] == [response.resources.compute[0].id]


def test_the_sweeper_thread_starts_only_when_asked_and_stops_cleanly(tmp_path):
    orchestrator, _, _, _ = orchestrated(tmp_path)
    off = Sweeper(orchestrator, 0)
    off.start()
    assert off._thread is None
    on = Sweeper(orchestrator, 3600)
    on.start()
    assert on._thread is not None and on._thread.is_alive()
    on.stop()
    assert on._thread is None


# ---- state from before release A
def test_an_older_state_file_gains_the_new_columns(tmp_path):
    import sqlite3

    from cloudinfra.state.state_manager import StateManager
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE requests (request_id TEXT PRIMARY KEY, name TEXT, provider TEXT, region TEXT, zone TEXT, owner TEXT, status TEXT, request_json TEXT, "
                   "labels_json TEXT, estimated_cost REAL, error_json TEXT, created_at TEXT, updated_at TEXT)")
        db.execute("INSERT INTO requests VALUES ('a', 'old', 'gcp', 'us-central1', NULL, 'app-layer', 'READY', '{}', '{}', 1.0, NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
    state = StateManager(str(path))
    record = state.get_request("a")
    assert record["lifecycle"] == {} and record["access"] == [] and record["expires_at"] is None
    state.set_lifecycle("a", {"expires_at": "2026-02-01T00:00:00Z"})
    assert [item["request_id"] for item in state.list_requests(expires_before="2026-03-01T00:00:00Z")] == ["a"]


# ---- the MCP tools and the REST API
@pytest.fixture
def served(tmp_path):
    config = configured(tmp_path)
    provider, fake = make_provider(config)
    app = build_app(config, registry=ProviderRegistry(config, {"gcp": provider}), executor=InlineExecutor())
    with TestClient(app) as client:
        yield client, fake


HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}


def tool(client, name, arguments):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    response = client.post("/mcp", headers=HEADERS, json=body)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_control_and_access_over_mcp_and_rest(served):
    client, fake = served
    created = tool(client, "create_compute", {"region": "us-central1", "cpu": 2, "memory_gb": 4, "lifetime_hours": 48, "autostop_idle_minutes": 60, "wait_seconds": 45})
    assert not created.get("isError"), created
    result = created["structuredContent"]
    request_id = result["request_id"]
    assert result["status"] == "READY" and result["lifecycle"]["expires_in_hours"] is not None and result["resources"]["compute"][0]["autostop_idle_minutes"] == 60
    assert result["access"][0]["member"] == DEV and result["resources"]["compute"][0]["connect"]["commands"]["ssh"].startswith("gcloud compute ssh")
    stopped = tool(client, "control_infrastructure", {"request_id": request_id, "action": "stop", "wait_seconds": 45})["structuredContent"]
    assert stopped["status"] == "STOPPED" and stopped["lifecycle"]["stop_reason"] == "requested"
    wrong = tool(client, "control_infrastructure", {"request_id": request_id, "action": "pause"})
    assert wrong["isError"] and wrong["structuredContent"]["error"]["code"] == "INVALID_REQUEST"
    listed = client.get("/infrastructure", params={"expiring_within_hours": 72}, headers=HEADERS).json()
    assert listed["count"] == 1
    started = client.post(f"/infrastructure/{request_id}/actions", json={"action": "start", "wait_seconds": 45}, headers=HEADERS)
    assert started.status_code == 200 and started.json()["status"] == "READY"
    conflict = client.post(f"/infrastructure/{request_id}/actions", json={"action": "start"}, headers=HEADERS)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "INVALID_STATE"
    bad = client.post(f"/infrastructure/{request_id}/actions", json={"action": "start", "extra": 1}, headers=HEADERS)
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_REQUEST"
    granted = client.post(f"/infrastructure/{request_id}/access", json={"action": "grant", "member": DATA}, headers=HEADERS).json()
    assert [item["status"] for item in granted["access"]] == ["GRANTED", "GRANTED"]
    outside = tool(client, "manage_infrastructure_access", {"request_id": request_id, "action": "grant", "member": "user:x@other.org"})
    assert outside["isError"] and outside["structuredContent"]["error"]["code"] == "INVALID_ACCESS"
    revoked = tool(client, "manage_infrastructure_access", {"request_id": request_id, "action": "revoke", "member": DATA})["structuredContent"]
    assert revoked["access"][1]["status"] == "REVOKED"
    awake = client.post(f"/infrastructure/{request_id}/actions", json={"action": "keep_awake", "hours": 2}, headers=HEADERS).json()
    assert awake["lifecycle"]["awake_until"]
    events = client.app.state.state.recent_events(limit=50, request_id=request_id)
    assert {"control", "access"} <= {item["name"] for item in events}
    assert base64.b64encode(b"x") and "fake-redis-auth-string" not in json.dumps(events)


def test_a_profile_removed_later_is_a_coded_error_not_a_crash(tmp_path):
    orchestrator, _, _, who = orchestrated(tmp_path, gcp={"profiles": PROFILES})
    response = provision(orchestrator, who, {"compute": {"cpu": 2, "memory_gb": 4, "profile": "hardened"}})
    orchestrator.config.providers.gcp.profiles.pop("hardened")
    error = refused(ErrorCode.INVALID_COMPUTE_CONFIGURATION, orchestrator.cost, who, request_id=response.request_id)
    assert "Unknown profile 'hardened'" in error.message



# ---- what the independent review of release A found
def stored_as(orchestrator, resource_id, status):
    resource = orchestrator.state.get_resource(resource_id)
    orchestrator.state.save_resource(resource.model_copy(update={"status": status}))


def test_revoke_and_a_lower_level_reach_resources_whose_state_is_unknown_or_changing(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {**VM_REQUEST, "access": [{"member": DATA, "level": "admin"}]})
    [machine] = response.resources.compute
    instance, tunnel = ("instance", machine.zone, machine.name), ("tunnel", machine.zone, machine.instance_id)
    stored_as(orchestrator, machine.id, ResourceStatus.UNKNOWN)  # what one failed refresh leaves behind
    orchestrator.manage_access(response.request_id, "grant", DATA, who, level="use")
    assert DATA not in fake.members(instance, "roles/compute.osAdminLogin") and DATA in fake.members(instance, "roles/compute.osLogin")
    stored_as(orchestrator, machine.id, ResourceStatus.UPDATING)  # a resize in flight
    revoked = orchestrator.manage_access(response.request_id, "revoke", DATA, who)
    assert next(item for item in revoked.access if item.member == DATA).status == "REVOKED"
    assert DATA not in fake.members(instance, "roles/compute.osLogin") and DATA not in fake.members(tunnel, IAP_TUNNEL)


def test_simultaneous_grants_keep_every_member(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, VM_REQUEST)
    original = fake.request

    def slow(method, url, json=None, params=None):  # widen the window between reading and writing the access list
        if url.endswith("setIamPolicy"):
            time.sleep(0.02)
        return original(method, url, json, params)
    fake.request = slow
    members = [f"user:dev{index}@example.com" for index in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda member: orchestrator.manage_access(response.request_id, "grant", member, who), members))
    entries = {item.member: item.status for item in orchestrator.status(response.request_id, who, refresh=False).access}
    assert entries == {DEV: "GRANTED", **{member: "GRANTED" for member in members}}
    [machine] = response.resources.compute
    assert sorted(fake.members(("tunnel", machine.zone, machine.instance_id), IAP_TUNNEL)) == sorted([DEV, *members])


def test_members_match_without_regard_to_case(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path, identity="user:Dev@Example.com")
    response = provision(orchestrator, who, {**VM_REQUEST, "access": [{"member": "user:John.Smith@Example.com"}]})
    assert [item.member for item in response.access] == [DEV, "user:john.smith@example.com"]
    [machine] = response.resources.compute
    instance = ("instance", machine.zone, machine.name)
    for binding in fake.policies[instance]["bindings"]:  # the same member, written in another case by someone else
        binding["members"] = [item.replace("john.smith", "John.Smith") for item in binding["members"]]
    revoked = orchestrator.manage_access(response.request_id, "revoke", "user:JOHN.SMITH@example.com", who)
    assert next(item for item in revoked.access if item.member == "user:john.smith@example.com").status == "REVOKED"
    assert not any("john.smith" in item.lower() for binding in fake.policies[instance]["bindings"] for item in binding["members"])
    with pytest.raises(pydantic.ValidationError):
        InfrastructureRequest.model_validate({**VM_REQUEST, "access": [{"member": "user:A@example.com"}, {"member": "user:a@example.com"}]})


def test_extend_waits_for_a_stop_and_retention_follows_only_the_expiry_it_was_for(tmp_path):
    orchestrator, _, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {**VM_REQUEST, "lifetime": {"hours": 24}})
    orchestrator.state.set_request_status(response.request_id, RequestStatus.STOPPING)
    error = refused(ErrorCode.INVALID_STATE, orchestrator.control, response.request_id, "extend", who, hours=24)
    assert "extend a READY or STOPPED request" in error.message
    orchestrator.state.set_request_status(response.request_id, RequestStatus.READY)
    stopped = orchestrator.stop(response.request_id, SWEEPER, wait_seconds=45, reason="expired", expired_at="2026-10-01T00:00:00Z")
    assert stopped.status == RequestStatus.STOPPED and stopped.lifecycle.retain_until is None, "an expiry that moved on starts no retention"


def test_the_sweeper_decides_on_the_request_as_it_is_now(tmp_path):
    orchestrator, _, clock, who = orchestrated(tmp_path)
    sweeper = Sweeper(orchestrator, 0)
    response = provision(orchestrator, who, {**VM_REQUEST, "lifetime": {"hours": 2, "on_expiry": "destroy"}})
    listed = orchestrator.state.sweepable_requests()
    orchestrator.control(response.request_id, "extend", who, hours=24)
    clock.advance(hours=3)
    orchestrator.state.sweepable_requests = lambda: listed  # the list the sweep began with predates the extension
    found = sweeper.tick()
    assert found["destroyed"] == [] and found["stopped"] == [] and found["warned"] == [response.request_id]
    after = orchestrator.status(response.request_id, who)
    assert after.status == RequestStatus.READY and after.lifecycle.expires_at == "2026-10-29T14:00:00Z" and after.lifecycle.warnings_sent == ["48h"]


def test_nothing_is_marked_stopped_while_a_machine_may_still_run(tmp_path):
    orchestrator, fake, clock, who = orchestrated(tmp_path)
    sweeper = Sweeper(orchestrator, 0)
    response = provision(orchestrator, who, {**VM_REQUEST, "lifetime": {"hours": 1}})
    [machine] = response.resources.compute
    instance = fake.instances[(machine.zone, machine.name)]
    instance["status"] = "STOPPING"  # Google is in the middle of something
    assert refused(ErrorCode.INVALID_STATE, orchestrator.control, response.request_id, "stop", who).details["reason"] == "changing"
    clock.advance(hours=2)
    found = sweeper.tick()
    assert found["stopped"] == [] and found["errors"][0]["error"]["details"]["reason"] == "changing"
    assert orchestrator.status(response.request_id, who).status == RequestStatus.READY
    instance["status"] = "RUNNING"
    fake.fail_on.append(("GET", f"/instances/{machine.name}"))  # its state cannot be read: it is stopped anyway, and the outcome stays open
    found = sweeper.tick()
    after = orchestrator.status(response.request_id, who, refresh=False)
    assert instance["status"] == "TERMINATED" and after.status == RequestStatus.READY and after.operations[-1].status == "FAILED"
    fake.fail_on.clear()
    assert sweeper.tick()["stopped"] == [response.request_id] and orchestrator.status(response.request_id, who).status == RequestStatus.STOPPED


def test_a_stopped_machine_is_resized_and_stays_stopped(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, VM_REQUEST)
    orchestrator.control(response.request_id, "stop", who, wait_seconds=45)
    fake.calls.clear()
    resized = orchestrator.resize_compute(response.resources.compute[0].id, 8, 32, who, wait_seconds=45)
    assert resized.resource.status == ResourceStatus.STOPPED and resized.resource.machine_type == "e2-standard-8"
    assert not any(call[1].endswith(("/start", "/stop")) for call in fake.calls)


class Dropping:
    """Workers that never run what they are given: a service that stops right after taking work."""

    def submit(self, *args):
        return None


class Closed:
    """Workers that take no more work: a service shutting down."""

    def submit(self, *args):
        raise RuntimeError("cannot schedule new futures after shutdown")


def test_a_restart_leaves_no_request_half_way(tmp_path):
    orchestrator, _, _, who = orchestrated(tmp_path)
    running, stopped, leaving = (provision(orchestrator, who, VM_REQUEST) for _ in range(3))
    orchestrator.control(stopped.request_id, "stop", who, wait_seconds=45)
    inline, orchestrator.executor = orchestrator.executor, Dropping()
    orchestrator.control(running.request_id, "stop", who)
    orchestrator.control(stopped.request_id, "start", who)
    orchestrator.destroy(leaving.request_id, who)
    born = orchestrator.provision(InfrastructureRequest.model_validate(VM_REQUEST), who)
    assert [orchestrator.status(item.request_id, who, refresh=False).status for item in (running, stopped, leaving, born)] == [
        RequestStatus.STOPPING, RequestStatus.STARTING, RequestStatus.DESTROYING, RequestStatus.PROVISIONING]
    orchestrator.executor = inline
    found = {item["request_id"]: (item["was"], item["now"]) for item in orchestrator.recover()}
    assert found == {running.request_id: ("STOPPING", "READY"), stopped.request_id: ("STARTING", "STOPPED"), leaving.request_id: ("DESTROYING", "DESTROY_FAILED"),
                     born.request_id: ("PROVISIONING", "FAILED")}
    assert orchestrator.state.running_operations() == []
    assert orchestrator.status(born.request_id, who).error.details["cause"] == "INTERRUPTED"
    assert orchestrator.destroy(leaving.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED, "an interrupted destroy can be repeated"
    assert orchestrator.control(running.request_id, "stop", who, wait_seconds=45).status == RequestStatus.STOPPED


def test_work_refused_at_shutdown_leaves_the_request_as_it_was(tmp_path):
    orchestrator, _, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, VM_REQUEST)
    orchestrator.executor = Closed()
    error = refused(ErrorCode.INTERNAL_ERROR, orchestrator.control, response.request_id, "stop", who)
    after = orchestrator.status(response.request_id, who)
    assert "shutting down" in error.message and after.status == RequestStatus.READY and after.operations[-1].status == "FAILED"
    refused(ErrorCode.INTERNAL_ERROR, orchestrator.destroy, response.request_id, who)
    assert orchestrator.status(response.request_id, who).status == RequestStatus.READY
    refused(ErrorCode.INTERNAL_ERROR, orchestrator.provision, InfrastructureRequest.model_validate(VM_REQUEST), who)
    # Requests made within the same second are listed in id order, not by age, so the new one is found by its id.
    [newest] = [entry for entry in orchestrator.list_requests(who).requests if entry.request_id != response.request_id]
    assert newest.status == RequestStatus.FAILED and newest.error.details["cause"] == "SHUTTING_DOWN"


def test_idle_stop_looks_again_at_a_machine_after_a_failed_refresh(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"compute": {"cpu": 4, "memory_gb": 16, "autostop": {"idle_minutes": 30}}})
    [machine] = response.resources.compute
    stored_as(orchestrator, machine.id, ResourceStatus.UNKNOWN)
    fake.monitoring_value = 0.01
    assert Sweeper(orchestrator, 0).tick()["idle_stopped"] == [machine.id]


def test_the_maximum_lifetime_applies_to_a_request_that_sets_none(tmp_path):
    orchestrator, _, _, who = orchestrated(tmp_path, limits={"max_lifetime_hours": 100})
    response = provision(orchestrator, who, VM_REQUEST)
    assert response.lifecycle.expires_in_hours == 100.0 and response.lifecycle.max_expires_at == response.lifecycle.expires_at
    assert any("(the operator's maximum lifetime)" in item for item in response.warnings)


def test_warnings_say_what_the_sweeper_will_do_and_expiring_lists_only_live_requests(tmp_path):
    orchestrator, _, clock, who = orchestrated(tmp_path, limits={"expiry_retention_days": 0})
    response = provision(orchestrator, who, {**VM_REQUEST, "lifetime": {"hours": 24}})
    assert any(item.startswith("Expires in 24.0 hours") and "then it is destroyed" in item for item in response.warnings)
    clock.advance(hours=25)
    warnings = orchestrator.status(response.request_id, who).warnings
    assert any(item.startswith("Expired at 2026-10-29T12:00:00Z; at the service's next check it is destroyed") for item in warnings), warnings
    assert orchestrator.list_requests(who, expiring_within_hours=1).count == 1
    assert Sweeper(orchestrator, 0).tick()["destroyed"] == [response.request_id]
    assert orchestrator.status(response.request_id, who).status == RequestStatus.DESTROYED
    assert orchestrator.list_requests(who, expiring_within_hours=1).count == 0, "a destroyed request no longer expires"
