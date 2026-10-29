"""What the review of release A found (1.37.1): a test for each defect, against the fake Google Cloud. Every test of a defect
fails on 1.37.0; the audit-trail test replaces one that could not fail. Nothing here touches a cloud."""
import asyncio
import base64
import gc
import json
import sqlite3
import time

import httpx
import pydantic
import pytest
from fastapi.testclient import TestClient
from gcp_fakes import VM_REQUEST, config_dict, make_orchestrator, make_provider, principal
from test_infra_release_a import DATA, DEV, GPU_GROUP, HEADERS, PROFILES, configured, orchestrated, provision, refused, stored_as

from cloudinfra import cli
from cloudinfra.config import Config
from cloudinfra.mcp.catalog import CATALOG
from cloudinfra.mcp.server import build_app
from cloudinfra.models import InfrastructureRequest, RequestStatus, ResourceStatus
from cloudinfra.models.errors import ErrorCode
from cloudinfra.orchestration import validator
from cloudinfra.orchestration.provisioner import InlineExecutor
from cloudinfra.orchestration.sweeper import Sweeper
from cloudinfra.providers import ProviderRegistry
from cloudinfra.state.state_manager import ProcessLock

EMPTY = {"warned": [], "stopping": [], "destroying": [], "kept": [], "idle_stopping": [], "errors": []}


class Deferred:
    """Workers that run what they are given only when the test says so: work in flight, held still."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args, **kwargs):
        self.jobs.append((fn, args, kwargs))

    def run(self):
        jobs, self.jobs = self.jobs, []
        for fn, args, kwargs in jobs:
            fn(*args, **kwargs)


class Closed:
    """Workers that take no more work: a service shutting down."""

    def submit(self, *args, **kwargs):
        raise RuntimeError("cannot schedule new futures after shutdown")


class Died(BaseException):
    """The process ending in the middle of a call, as a restart does."""


def secret_of(response, purpose):
    return next(item for item in response.resources.secret if item.purpose == purpose)


def operations(orchestrator, request_id, kind):
    return [item for item in orchestrator.state.operations_of(request_id) if item.kind == kind]


# ---- permissions, credentials and single deletes
def test_a_lifetime_needs_can_destroy_because_it_ends_in_a_destroy(tmp_path):
    caller = {**config_dict()["principals"][0], "can_destroy": False}
    orchestrator, _, _, who = orchestrated(tmp_path, principals=[caller])
    error = refused(ErrorCode.PERMISSION_DENIED, orchestrator.provision, InfrastructureRequest.model_validate({**VM_REQUEST, "lifetime": {"hours": 1}}), who)
    assert "a lifetime ends in a destroy" in error.message and error.details["field"] == "lifetime"
    assert provision(orchestrator, who, VM_REQUEST).status == RequestStatus.READY, "without a lifetime nothing changes"


def test_credential_secrets_of_requests_with_the_same_name_do_not_collide(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    first = orchestrator.create_database(who, region="us-central1", name="orders", public_access=True, wait_seconds=45)
    second = orchestrator.create_database(who, region="europe-west1", name="orders", public_access=True, wait_seconds=45)
    assert first.status == second.status == RequestStatus.READY, second.error
    names = {secret_of(item, "database_password").name for item in (first, second)}
    assert names == {f"orders-db-password-{item.request_id[:6]}" for item in (first, second)} and set(fake.secrets) == names


def test_deleting_a_database_or_a_cache_takes_its_secret_and_ends_the_request(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    database = orchestrator.create_database(who, region="us-central1", public_access=True, wait_seconds=45)
    deleted = orchestrator.delete_resource(database.resources.database[0].id, who, database.resources.database[0].kind, wait_seconds=45)
    assert deleted.operation.status == "SUCCEEDED" and "with its secret" in deleted.operation.detail
    assert orchestrator.status(database.request_id, who).status == RequestStatus.DESTROYED and not fake.secrets and not fake.sql
    network = orchestrator.create_network(who, region="us-central1", wait_seconds=45)
    cache = orchestrator.create_cache(who, region="us-central1", network_id=network.resources.network[0].id, wait_seconds=45)
    orchestrator.delete_resource(cache.resources.cache[0].id, who, cache.resources.cache[0].kind, wait_seconds=45)
    assert orchestrator.status(cache.request_id, who).status == RequestStatus.DESTROYED and not fake.secrets and not fake.redis
    assert orchestrator.state.active_requests("app-layer") == 1, "only the network's request is left"


def test_a_secret_whose_value_could_not_be_written_is_cleaned_up_or_destroyed(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    fake.fail_on.append(("POST", ":addVersion"))
    failed = orchestrator.provision(InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "database": {"public_access": True}}), who)
    assert failed.status == RequestStatus.FAILED and not fake.secrets and not fake.sql, "cleanup found the secret it had made"
    kept, fake, _, who = orchestrated(tmp_path / "kept", cleanup_on_failure=False)
    fake.fail_on.append(("POST", ":addVersion"))
    failed = kept.provision(InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "database": {"public_access": True}}), who)
    [secret] = failed.resources.secret
    assert failed.status == RequestStatus.FAILED and secret.provider_id and set(fake.secrets) == {secret.name}
    fake.fail_on.clear()
    assert kept.destroy(failed.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED and not fake.secrets


def test_nobody_is_left_without_a_way_to_the_database_password(tmp_path):
    def database(**settings):
        config = Config.model_validate({**config_dict(database=str(tmp_path / f"{len(settings)}.db")), "access": {}, **settings})
        orchestrator, _ = make_orchestrator(tmp_path, config=config)
        return orchestrator, principal(config)
    orchestrator, who = database()
    error = refused(ErrorCode.INVALID_ACCESS, orchestrator.provision, InfrastructureRequest.model_validate({"region": "us-central1", "database": {"public_access": True}}), who)
    assert "Nobody could read this database's password" in error.message
    orchestrator, who = database(return_database_password=True)
    created = provision(orchestrator, who, {"database": {"public_access": True}})
    assert created.credentials.database_password and any("the creating response also carries it, once" in item for item in created.warnings)
    without, _, _, who = orchestrated(tmp_path / "domains", identity=None)
    created = provision(without, who, {"database": {"public_access": True}})
    assert any("nobody is granted the db-password secret yet" in item for item in created.warnings) and created.credentials.database_password is None


def test_a_single_delete_updates_the_access_list_and_the_last_one_revokes_it(tmp_path):
    orchestrator, _, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {**VM_REQUEST, "secret": {"name": "token"}, "access": [{"member": DATA}]})
    [machine], [secret] = response.resources.compute, response.resources.secret
    orchestrator.delete_resource(machine.id, who, machine.kind, wait_seconds=45)
    entries = orchestrator.status(response.request_id, who).access
    assert all(item.resources == [secret.id] and item.status == "GRANTED" for item in entries)
    orchestrator.delete_resource(secret.id, who, secret.kind, wait_seconds=45)
    after = orchestrator.status(response.request_id, who)
    assert after.status == RequestStatus.DESTROYED and all(item.status == "REVOKED" and item.resources == [] for item in after.access)


# ---- access records, grants and revokes
def test_grants_made_during_a_resize_or_a_refresh_are_kept(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, VM_REQUEST)
    [machine] = response.resources.compute
    orchestrator.executor = deferred = Deferred()
    orchestrator.resize_compute(machine.id, 8, 32, who)
    orchestrator.manage_access(response.request_id, "grant", DATA, who)
    deferred.run()
    resized = orchestrator.state.get_resource(machine.id)
    assert resized.machine_type == "e2-standard-8" and {grant.member for grant in resized.access} == {DEV, DATA}
    snapshot = orchestrator.state.get_resource(machine.id)
    orchestrator.manage_access(response.request_id, "revoke", DATA, who)
    fake.instances[(machine.zone, machine.name)]["status"] = "TERMINATED"  # the refresh has news to save
    record = orchestrator.state.get_request(response.request_id)
    refreshed = orchestrator._refresh(record, snapshot)
    assert refreshed.status == ResourceStatus.STOPPED and {grant.member for grant in orchestrator.state.get_resource(machine.id).access} == {DEV}, \
        "a member revoked while the provider was asked does not come back"


def test_a_member_is_recorded_not_granted_where_nothing_takes_grants(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    plan = orchestrator.plan(InfrastructureRequest.model_validate({"region": "us-central1", "storage": {"size_gb": 10}, "access": [{"member": DATA}]}), who)
    assert any("the members are recorded only" in item for item in plan.warnings)
    disks = provision(orchestrator, who, {"storage": {"size_gb": 10}, "access": [{"member": DATA}]})
    assert [item.status for item in disks.access] == ["RECORDED", "RECORDED"] and fake.policies == {}
    machine = provision(orchestrator, who, VM_REQUEST)
    stored_as(orchestrator, machine.resources.compute[0].id, ResourceStatus.CREATING)
    waiting = next(item for item in orchestrator.manage_access(machine.request_id, "grant", DATA, who).access if item.member == DATA)
    assert waiting.status == "FAILED" and waiting.error.code == ErrorCode.INVALID_STATE and "were changing" in waiting.error.message


def test_a_principals_identity_outside_the_domains_can_be_granted_again(tmp_path):
    app = "serviceAccount:app-layer@proj-123456.iam.gserviceaccount.com"
    orchestrator, fake, _, who = orchestrated(tmp_path, identity=app)
    response = provision(orchestrator, who, VM_REQUEST)
    assert response.access[0].member == app and response.access[0].status == "GRANTED"
    changed = orchestrator.manage_access(response.request_id, "grant", app, who, level="use")
    assert changed.access[0].level == "use" and changed.access[0].status == "GRANTED"


def test_a_level_change_keeps_access_on_a_failed_resource_that_still_exists(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"storage": {"type": "object", "size_gb": 10}, "access": [{"member": DATA}]})
    [bucket] = response.resources.storage
    stored_as(orchestrator, bucket.id, ResourceStatus.FAILED)  # a delete that failed: the bucket is still there
    orchestrator.manage_access(response.request_id, "grant", DATA, who, level="admin")
    assert DATA in fake.members(("bucket", bucket.name), "roles/storage.objectAdmin") and DATA not in fake.members(("bucket", bucket.name), "roles/storage.objectUser")


def test_a_revoke_reaches_what_a_failed_destroy_left_and_a_failed_revoke_fails(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"storage": {"type": "object", "size_gb": 10}, "access": [{"member": DATA}]})
    [bucket] = response.resources.storage
    fake.bucket_objects.add(bucket.name)
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == RequestStatus.DESTROY_FAILED
    refused(ErrorCode.INVALID_STATE, orchestrator.manage_access, response.request_id, "grant", "user:new@example.com", who)
    fake.fail_on.append(("PUT", f"/b/{bucket.name}/iam"))
    failing = orchestrator.manage_access(response.request_id, "revoke", DATA, who)
    entry = next(item for item in failing.access if item.member == DATA)
    assert entry.status == "GRANTED" and entry.error and failing.operations[-1].kind == "revoke_access" and failing.operations[-1].status == "FAILED"
    fake.fail_on.clear()
    revoked = orchestrator.manage_access(response.request_id, "revoke", DATA, who)
    assert next(item for item in revoked.access if item.member == DATA).status == "REVOKED" and revoked.operations[-1].status == "SUCCEEDED"
    assert DATA not in fake.members(("bucket", bucket.name), "roles/storage.objectUser")


# ---- stop, start, resize and delete
def test_a_failed_start_of_an_idle_stopped_machine_leaves_the_running_request_ready(tmp_path):
    orchestrator, fake, clock, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"compute": {"count": 2, "cpu": 4, "memory_gb": 16, "autostop": {"idle_minutes": 30}}})
    idle, busy = response.resources.compute
    fake.monitoring_value, fake.monitoring_by_instance = 0.0, {busy.instance_id: 0.9}
    clock.advance(hours=1)
    assert Sweeper(orchestrator, 0).tick()["idle_stopping"] == [idle.id]
    fake.fail_on.append(("POST", "/start"))
    started = orchestrator.control(response.request_id, "start", who, wait_seconds=45)
    assert started.status == RequestStatus.READY and started.operations[-1].status == "FAILED"
    fake.fail_on.clear()
    assert orchestrator.control(response.request_id, "stop", who, wait_seconds=45).status == RequestStatus.STOPPED, "and it can still be stopped"


def test_stop_resize_and_delete_wait_for_each_other(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, VM_REQUEST)
    [machine] = response.resources.compute
    orchestrator.executor = deferred = Deferred()
    orchestrator.resize_compute(machine.id, 8, 32, who)
    for action in ("stop", "start"):
        assert refused(ErrorCode.INVALID_STATE, orchestrator.control, response.request_id, action, who).details["reason"] == "changing"
    refused(ErrorCode.INVALID_STATE, orchestrator.destroy, response.request_id, who)
    deferred.run()
    orchestrator.control(response.request_id, "stop", who)
    refused(ErrorCode.INVALID_STATE, orchestrator.resize_compute, machine.id, 2, 4, who)
    refused(ErrorCode.INVALID_STATE, orchestrator.delete_resource, machine.id, who, machine.kind)
    orchestrator.state.set_request_status(response.request_id, RequestStatus.DESTROYED)  # destroyed meanwhile, elsewhere
    deferred.run()
    assert orchestrator.state.get_request(response.request_id)["status"] == RequestStatus.DESTROYED, "a late stop never undoes a destroy"


def test_a_delete_or_resize_refused_at_shutdown_leaves_the_machine_as_it_was(tmp_path):
    orchestrator, _, _, who = orchestrated(tmp_path)
    [machine] = provision(orchestrator, who, VM_REQUEST).resources.compute
    orchestrator.executor = Closed()
    for call in (lambda: orchestrator.delete_resource(machine.id, who, machine.kind), lambda: orchestrator.resize_compute(machine.id, 8, 32, who)):
        with pytest.raises(Exception) as caught:
            call()
        assert getattr(caught.value, "code", None) == ErrorCode.INTERNAL_ERROR and "shutting down" in caught.value.message
        assert orchestrator.state.get_resource(machine.id).status == ResourceStatus.READY
    assert all(item.status == "FAILED" for item in orchestrator.state.operations_of(machine.request_id) if item.kind in ("delete_compute", "resize_compute"))


def test_a_suspended_machine_is_resumed(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, VM_REQUEST)
    [machine] = response.resources.compute
    fake.instances[(machine.zone, machine.name)]["status"] = "SUSPENDED"
    assert orchestrator.status(response.request_id, who).resources.compute[0].status == ResourceStatus.STOPPED
    started = orchestrator.control(response.request_id, "start", who, wait_seconds=45)
    assert started.status == RequestStatus.READY and any(call[1].endswith(f"/{machine.name}/resume") for call in fake.calls)


# ---- the sweeper
def test_a_failed_expiry_destroy_is_tried_again_after_a_wait(tmp_path):
    orchestrator, fake, clock, who = orchestrated(tmp_path)
    sweeper = Sweeper(orchestrator, 0)
    response = provision(orchestrator, who, {**VM_REQUEST, "lifetime": {"hours": 2, "on_expiry": "destroy"}})
    fake.fail_on.append(("DELETE", "/instances/"))
    clock.advance(hours=3)
    assert sweeper.tick()["destroying"] == [response.request_id]
    assert orchestrator.status(response.request_id, who).status == RequestStatus.DESTROY_FAILED
    fake.fail_on.clear()
    clock.advance(minutes=1)
    assert sweeper.tick() == EMPTY, "the next attempt waits 5 minutes"
    clock.advance(minutes=5)
    assert sweeper.tick()["destroying"] == [response.request_id] and orchestrator.status(response.request_id, who).status == RequestStatus.DESTROYED
    assert not fake.instances


def test_a_refused_expiry_destroy_stops_what_can_stop_meanwhile(tmp_path):
    orchestrator, fake, clock, who = orchestrated(tmp_path)
    sweeper = Sweeper(orchestrator, 0)
    owner = provision(orchestrator, who, {**VM_REQUEST, "network": {}, "lifetime": {"hours": 2, "on_expiry": "destroy"}})
    tenant = provision(orchestrator, who, {"compute": {"cpu": 2, "memory_gb": 4, "network_id": owner.resources.network[0].id}})
    clock.advance(hours=3)
    found = sweeper.tick()
    assert found["stopping"] == [owner.request_id] and found["errors"][0]["error"]["code"] == "INVALID_STATE"
    assert orchestrator.status(owner.request_id, who).status == RequestStatus.STOPPED, "nothing of it runs while the destroy waits"
    orchestrator.destroy(tenant.request_id, who, wait_seconds=45)
    assert sweeper.tick()["destroying"] == [owner.request_id] and orchestrator.status(owner.request_id, who).status == RequestStatus.DESTROYED


def test_a_failing_expiry_stop_backs_off_instead_of_failing_at_every_sweep(tmp_path):
    orchestrator, fake, clock, who = orchestrated(tmp_path)
    sweeper = Sweeper(orchestrator, 0)
    response = provision(orchestrator, who, {**VM_REQUEST, "lifetime": {"hours": 1}})
    fake.fail_on.append(("POST", "/stop"))
    clock.advance(hours=2)
    for minutes in (0, 1, 4, 5, 4):  # attempts at minute 0 and 5; the third waits until minute 15
        clock.advance(minutes=minutes)
        sweeper.tick()
    assert len(operations(orchestrator, response.request_id, "stop")) == 2 and orchestrator.status(response.request_id, who).status == RequestStatus.READY
    clock.advance(minutes=1)
    sweeper.tick()
    assert len(operations(orchestrator, response.request_id, "stop")) == 3


def test_idle_machines_are_stopped_by_the_workers_one_by_one(tmp_path):
    orchestrator, fake, clock, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"compute": {"count": 2, "cpu": 4, "memory_gb": 16, "autostop": {"idle_minutes": 30}}})
    first, second = response.resources.compute
    fake.monitoring_value = 0.0
    clock.advance(hours=1)
    orchestrator.executor = deferred = Deferred()
    found = Sweeper(orchestrator, 0).tick()
    assert found["idle_stopping"] == [first.id, second.id] and [item.status for item in operations(orchestrator, response.request_id, "stop_compute")] == ["RUNNING"] * 2
    assert orchestrator.state.get_resource(first.id).status == ResourceStatus.READY, "the sweep only handed them over"
    assert orchestrator.control(response.request_id, "keep_awake", who, hours=1).lifecycle.awake_until, "the request is not held meanwhile"
    fake.fail_on.append(("POST", f"/instances/{first.name}/stop"))
    deferred.run()
    assert [orchestrator.state.get_resource(item.id).status for item in (first, second)] == [ResourceStatus.READY, ResourceStatus.STOPPED]
    assert [item.status for item in operations(orchestrator, response.request_id, "stop_compute")] == ["FAILED", "SUCCEEDED"]


def test_one_machine_that_cannot_be_measured_does_not_keep_the_others_running(tmp_path):
    orchestrator, fake, clock, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"compute": {"count": 2, "cpu": 4, "memory_gb": 16, "autostop": {"idle_minutes": 30}}})
    first, second = response.resources.compute
    fake.monitoring_value = 0.0
    fake.fail_on.append(("GET", f'instance_id="{first.instance_id}"'))  # never matches a URL: the filter is a parameter
    provider = orchestrator.registry.get("gcp")
    measured = provider.busy_cores

    def busy_cores(resource, minutes):
        if resource.id == first.id:
            from cloudinfra.models.errors import InfrastructureError
            raise InfrastructureError(ErrorCode.PROVIDER_UNAVAILABLE, "monitoring is down")
        return measured(resource, minutes)
    provider.busy_cores = busy_cores
    clock.advance(hours=1)
    found = Sweeper(orchestrator, 0).tick()
    assert found["idle_stopping"] == [second.id] and found["errors"][0]["resource_id"] == first.id


def test_idle_measurement_is_of_the_machine_itself(tmp_path):
    orchestrator, fake, clock, who = orchestrated(tmp_path)
    response = provision(orchestrator, who, {"compute": {"cpu": 4, "memory_gb": 16, "autostop": {"idle_minutes": 30}}})
    [machine] = response.resources.compute
    fake.monitoring_value, fake.monitoring_by_instance = 0.0, {machine.instance_id: 0.9}  # a namesake elsewhere is idle; this one is busy
    clock.advance(hours=1)
    assert Sweeper(orchestrator, 0).tick()["idle_stopping"] == []
    assert fake.monitoring_filters and all(f'resource.labels.instance_id="{machine.instance_id}"' in item and "instance_name" not in item for item in fake.monitoring_filters)


def test_a_restart_cleans_up_an_interrupted_provisioning_and_failed_requests_still_expire(tmp_path):
    orchestrator, fake, clock, who = orchestrated(tmp_path)
    original = fake.request

    def dies_at_the_secret(method, url, json=None, params=None):
        if method == "POST" and url.endswith("/secrets"):
            raise Died()
        return original(method, url, json, params)
    fake.request = dies_at_the_secret
    with pytest.raises(Died):
        orchestrator.provision(InfrastructureRequest.model_validate({**VM_REQUEST, "secret": {"name": "token"}}), who)
    fake.request = original
    assert len(fake.instances) == 1
    [found] = orchestrator.recover()
    assert found["now"] == "FAILED" and not fake.instances, "what it had created is deleted, as after any failure"
    kept, fake, clock, who = orchestrated(tmp_path / "kept", cleanup_on_failure=False)
    original = fake.request
    fake.request = dies_at_the_secret
    with pytest.raises(Died):
        kept.provision(InfrastructureRequest.model_validate({**VM_REQUEST, "secret": {"name": "token"}, "lifetime": {"hours": 2}}), who)
    fake.request = original
    kept.recover()
    assert len(fake.instances) == 1
    clock.advance(hours=3)
    [request_id] = Sweeper(kept, 0).tick()["destroying"]
    assert kept.status(request_id, who).status == RequestStatus.DESTROYED and not fake.instances, "a failed request is destroyed at its expiry"


def test_without_the_sweeper_no_lifetime_or_idle_stop_is_promised(tmp_path):
    with pytest.raises(pydantic.ValidationError, match="needs the sweeper"):
        Config.model_validate({**config_dict(sweep_interval_seconds=0), "limits": {"default_lifetime_hours": 24}})
    orchestrator, _, _, who = orchestrated(tmp_path, sweep_interval_seconds=0)
    error = refused(ErrorCode.INVALID_REQUEST, orchestrator.provision, InfrastructureRequest.model_validate({**VM_REQUEST, "lifetime": {"hours": 24}}), who)
    assert "applies no lifetimes" in error.message
    autostop = InfrastructureRequest.model_validate({"region": "us-central1", "compute": {"cpu": 2, "memory_gb": 4, "autostop": {"idle_minutes": 30}}})
    assert "stops no idle machines" in refused(ErrorCode.INVALID_REQUEST, orchestrator.provision, autostop, who).message


# ---- validation, cost and profiles
def test_a_gpu_machine_on_a_profile_without_drivers_boots_with_secure_boot_off(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path, gcp={"profiles": {"plain": {"image": "projects/platform-images/global/images/family/plain-ubuntu"}}})
    response = provision(orchestrator, who, {"compute": {**GPU_GROUP, "profile": "plain"}})
    [machine] = response.resources.compute
    body = next(call[2] for call in fake.calls if call[0] == "POST" and call[1].endswith("/instances"))
    assert body["shieldedInstanceConfig"]["enableSecureBoot"] is False and machine.secure_boot is False
    assert any("profile plain has no GPU driver" in item for item in response.warnings) and not any("drivers come with" in item for item in response.warnings)
    assert any("an image without GPU drivers" in note for note in machine.connect.notes)


def test_the_disk_limit_counts_the_boot_disk_a_profile_gives(tmp_path):
    big = {"gpu-big": {"image": "projects/platform-images/global/images/family/gpu-big", "min_boot_disk_gb": 500, "accelerator": "required"}}
    orchestrator, _, _, who = orchestrated(tmp_path, limits={"max_storage_gb_per_request": 1000}, gcp={"profiles": big, "default_accelerator_profile": "gpu-big"})
    request = InfrastructureRequest.model_validate({"region": "us-central1", "compute": {**GPU_GROUP, "count": 4}})
    [problem] = validator.validate(request, orchestrator.config, orchestrator.registry.get("gcp"))
    assert problem.code == ErrorCode.LIMIT_EXCEEDED and "1000 GB" in problem.message


def test_the_boot_disk_warning_names_a_size_only_when_one_was_asked(tmp_path):
    orchestrator, _, _, who = orchestrated(tmp_path, gcp={"profiles": PROFILES})
    asked = orchestrator.plan(InfrastructureRequest.model_validate({"region": "us-central1", "compute": {**GPU_GROUP, "profile": "gpu-signed", "boot_disk_gb": 60}}), who)
    assert any(item.endswith("the minimum of profile gpu-signed, not the 60 GB asked") for item in asked.warnings)
    default = orchestrator.plan(InfrastructureRequest.model_validate({"region": "us-central1", "compute": {**GPU_GROUP, "profile": "gpu-signed"}}), who)
    assert any(item.endswith("the minimum of profile gpu-signed") for item in default.warnings)


def test_create_compute_refuses_zero_rather_than_reading_it_as_none(tmp_path):
    orchestrator, _, _, who = orchestrated(tmp_path)
    for field in ("lifetime_hours", "autostop_idle_minutes"):
        with pytest.raises(pydantic.ValidationError):
            orchestrator.create_compute(who, region="us-central1", cpu=2, memory_gb=4, **{field: 0})


def test_cost_prices_what_exists_whatever_was_stored_or_configured_since(tmp_path):
    orchestrator, _, _, who = orchestrated(tmp_path, gcp={"profiles": PROFILES})
    response = provision(orchestrator, who, {"compute": GPU_GROUP})
    with sqlite3.connect(orchestrator.config.database) as db:  # as 1.36 stored it: a 20 GB default the new contract refuses
        stored = json.loads(db.execute("SELECT request_json FROM requests WHERE request_id = ?", (response.request_id,)).fetchone()[0])
        stored["compute"]["boot_disk_gb"] = 20
        db.execute("UPDATE requests SET request_json = ? WHERE request_id = ?", (json.dumps(stored), response.request_id))
    orchestrator.config.providers.gcp.default_accelerator_profile = "gpu-signed"  # the operator's later default, with a 100 GB minimum
    cost = orchestrator.cost(who, request_id=response.request_id)
    [line] = cost["estimate"]["lines"]
    assert "a 50 GB balanced boot disk" in line["basis"] and "nvidia-l4" in line["basis"] and cost["current_monthly_usd"] == line["monthly_usd"]
    [machine] = provision(orchestrator, who, VM_REQUEST).resources.compute
    orchestrator.resize_compute(machine.id, 8, 32, who, wait_seconds=45)
    assert orchestrator.cost(who, resource_id=machine.id)["estimate"]["lines"][0]["basis"].startswith("e2-standard-8 "), "the machine as resized"


# ---- the server
def test_the_rest_routes_never_hold_the_event_loop(tmp_path):
    config = configured(tmp_path)
    provider, _ = make_provider(config)
    app = build_app(config, registry=ProviderRegistry(config, {"gcp": provider}), executor=InlineExecutor())
    orchestrator = app.state.orchestrator
    response = provision(orchestrator, principal(config), VM_REQUEST)
    granting = orchestrator.manage_access

    def slow(*args, **kwargs):
        time.sleep(1.0)  # IAM calls, a lock, a wait: anything slow
        return granting(*args, **kwargs)
    orchestrator.manage_access = slow

    async def both():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            began = time.monotonic()
            call = asyncio.create_task(client.post(f"/infrastructure/{response.request_id}/access", json={"action": "grant", "member": DATA}, headers=HEADERS))
            await asyncio.sleep(0.2)  # the grant is under way
            health = await client.get("/healthz")
            return health, time.monotonic() - began, await call
    health, waited, granted = asyncio.run(both())
    assert health.status_code == 200 and waited < 0.8, f"the health check waited {waited:.2f} s behind a grant"
    assert granted.status_code == 200 and granted.json()["access"][1]["member"] == DATA


def test_a_body_that_is_not_json_is_a_structured_refusal(tmp_path):
    config = configured(tmp_path)
    provider, _ = make_provider(config)
    app = build_app(config, registry=ProviderRegistry(config, {"gcp": provider}), executor=InlineExecutor())
    with TestClient(app) as client:
        for path in ("/infrastructure", "/infrastructure/plan", "/infrastructure/" + "a" * 32 + "/actions", "/infrastructure/" + "a" * 32 + "/access"):
            for body in (b"", b"{not json"):
                refused_body = client.post(path, content=body, headers={**HEADERS, "Content-Type": "application/json"})
                assert refused_body.status_code == 400 and refused_body.json()["error"]["code"] == "INVALID_REQUEST", (path, refused_body.text)
        assert sum(1 for item in app.state.state.recent_events(limit=100) if item["outcome"] == "error") == 8


def test_one_process_per_state_file(tmp_path):
    first, second = ProcessLock(str(tmp_path / "state.db")), ProcessLock(str(tmp_path / "state.db"))
    first.acquire()
    with pytest.raises(RuntimeError, match="one process per state file"):
        second.acquire()
    first.release()
    second.acquire()
    second.release()
    config = configured(tmp_path / "served")
    apps = [build_app(config, registry=ProviderRegistry(config, {"gcp": make_provider(config)[0]}), executor=InlineExecutor()) for _ in range(2)]
    with TestClient(apps[0]):
        with pytest.raises(RuntimeError, match="one process per state file"):
            with TestClient(apps[1]):
                pass


def test_work_finishing_late_never_overwrites_a_status_set_meanwhile(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    orchestrator.executor = deferred = Deferred()
    born = orchestrator.provision(InfrastructureRequest.model_validate(VM_REQUEST), who)
    orchestrator.state.set_request_status(born.request_id, RequestStatus.DESTROYED)  # another process took it over and destroyed it
    deferred.run()
    assert orchestrator.state.get_request(born.request_id)["status"] == RequestStatus.DESTROYED and not fake.instances, "what it made is cleaned up"


def test_the_lock_table_keeps_no_request_it_is_not_working_on(tmp_path):
    orchestrator, _, _, who = orchestrated(tmp_path)
    for _ in range(3):
        provision(orchestrator, who, {**VM_REQUEST, "access": [{"member": DATA}]})
    gc.collect()
    assert len(orchestrator._locks) == 0
    held = orchestrator.lock("x")
    assert orchestrator.lock("x") is held and len(orchestrator._locks) == 1


# ---- connect blocks, the command line, warnings and the audit trail
def test_connect_blocks_name_the_network_and_machine_of_another_request(tmp_path):
    orchestrator, _, _, who = orchestrated(tmp_path)
    network = orchestrator.create_network(who, region="us-central1", name="shared", wait_seconds=45).resources.network[0]
    cache = orchestrator.create_cache(who, region="us-central1", network_id=network.id, wait_seconds=45).resources.cache[0]
    assert cache.connect.prerequisites == ["A machine on the network shared-net: the cache has no public address"]
    database = orchestrator.create_database(who, region="us-central1", network_id=network.id, wait_seconds=45).resources.database[0]
    assert database.connect.prerequisites[0] == "Connect from a machine on the network shared-net"
    machine = provision(orchestrator, who, VM_REQUEST).resources.compute[0]
    disk = orchestrator.create_storage(who, region="us-central1", size_gb=10, zone=machine.zone, wait_seconds=45).resources.storage[0]
    orchestrator.attach_storage(disk.id, machine.id, who)
    attached = orchestrator.get_resource(disk.id, who).resource
    shown = orchestrator.status(attached.request_id, who).resources.storage[0]
    assert shown.connect.notes == [f"A block disk, attached on {machine.name} as /dev/disk/by-id/google-{disk.name}"]
    assert any("every port forwarded with port_forward" in item and "configure_firewall" in item for item in machine.connect.prerequisites)


def test_offline_validation_takes_access_members(tmp_path, capsys):
    path = tmp_path / "request.yaml"
    path.write_text("region: us-central1\ncompute: {cpu: 2, memory_gb: 4}\ndatabase: {public_access: true}\naccess: [{member: 'user:alice@other.org'}]\n")
    assert cli.main(["plan", str(path)]) == 0
    printed = capsys.readouterr()
    assert json.loads(printed.out)["planned"] and "not checked offline" in printed.err


def test_no_credential_reaches_the_audit_trail_or_the_log(tmp_path, caplog):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    with caplog.at_level("INFO", logger="cloudinfra"):
        response = provision(orchestrator, who, {"network": {}, "cache": {}, "database": {}})
    password = base64.b64decode(fake.secret_versions[secret_of(response, "database_password").name][0]).decode()
    events = json.dumps(orchestrator.state.recent_events(limit=500))
    assert base64.b64decode(fake.secret_versions[secret_of(response, "cache_auth").name][0]).decode() == "fake-redis-auth-string", "it went through the service"
    for value in ("fake-redis-auth-string", password):
        assert value not in events and value not in caplog.text and value not in json.dumps(response.model_dump(mode="json"))


def test_the_catalog_says_what_a_resize_of_a_stopped_machine_does():
    spec = next(item for item in CATALOG if item.name == "resize_compute")
    assert "a stopped one is resized and stays stopped" in spec.purpose and "it is stopped, resized and started;" not in spec.purpose
