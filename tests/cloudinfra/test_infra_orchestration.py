"""The lifecycle at the boundary: validate, plan, provision, status, destroy; failure with cleanup; authorization; the design test."""
import json

import pytest
from gcp_fakes import FULL_REQUEST, VM_REQUEST, make_config, make_orchestrator, principal

from cloudinfra.models import InfrastructureRequest, RequestStatus, ResourceKind, ResourceStatus
from cloudinfra.models.errors import ErrorCode, InfrastructureError


def test_design_test_one_ubuntu_vm_with_four_cpus_and_sixteen_gb_and_nothing_about_an_application(tmp_path):
    """A caller says "give me one Ubuntu VM with 4 CPUs and 16 GB RAM": no repository, no source, no Dockerfile, no workflow, no tests, no build."""
    orchestrator, fake = make_orchestrator(tmp_path)
    request = InfrastructureRequest.model_validate({"provider": "gcp", "region": "us-central1", "compute": {"cpu": 4, "memory_gb": 16, "os": "ubuntu"}})
    assert set(request.model_dump(exclude_none=True)) == {"provider", "region", "labels", "compute", "access"} and request.access == []
    response = orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert response.status == RequestStatus.READY and response.provider == "gcp" and response.region == "us-central1"
    [machine] = response.resources.compute
    assert machine.cpu == 4 and machine.memory_gb == 16 and machine.os_image == "ubuntu-2404-lts-amd64" and machine.machine_type == "e2-standard-4"
    assert machine.status == ResourceStatus.READY and machine.internal_ip and machine.public_ip is None and machine.id.startswith("cmp-")
    assert response.endpoints.internal_ips == [machine.internal_ip] and response.endpoints.public_ips == [] and response.estimated_monthly_cost_usd > 0
    assert [operation.kind for operation in response.operations] == ["provision"] and response.operations[0].status == "SUCCEEDED"
    assert response.credentials is None and response.error is None
    assert not any(word in json.dumps([call[2] for call in fake.calls], default=str).lower() for word in ("github", "docker", "repository", "startup-script"))


def test_full_request_provisions_in_dependency_order_and_returns_endpoints(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = orchestrator.provision(InfrastructureRequest.model_validate(FULL_REQUEST), who, wait_seconds=45)
    assert response.status == RequestStatus.READY and response.name == "analytics" and response.labels == {"team": "data"}
    creates = [("sql" if "sqladmin" in call[1] else "") + call[1].split("/")[-1] for call in fake.calls if call[0] == "POST" and call[2].get("name")]
    kinds = [{"networks": "network", "subnetworks": "subnet", "firewalls": "firewall", "disks": "disk", "instances": "instance", "addresses": "address", "sqlinstances": "database"}.get(item, item)
             for item in creates]
    assert kinds.index("network") < kinds.index("disk") < kinds.index("instance") < kinds.index("address") < kinds.index("database")
    assert len(kinds) - 1 - kinds[::-1].index("instance") < kinds.index("database"), kinds  # every machine before the database
    assert len(response.resources.compute) == 2 and len(response.resources.storage) == 2 and len(response.resources.network) == 1 and len(response.resources.database) == 1
    assert [disk.attached_to for disk in response.resources.storage] == [machine.id for machine in response.resources.compute]
    assert [machine.attached_disks for machine in response.resources.compute] == [[disk.id] for disk in response.resources.storage]
    assert len(response.endpoints.public_ips) == 2 and response.endpoints.database_host == "10.20.0.3" and response.endpoints.database_port == 5432
    [secret] = response.resources.secret
    assert response.credentials.database_password is None and response.credentials.database_password_secret == secret.id == response.resources.database[0].password_secret
    assert secret.name == "analytics-db-password" and secret.purpose == "database_password" and secret.versions == 1
    password = fake.sql_users["analytics-db-" + response.request_id[:6]][0]["password"]
    assert response.resources.network[0].private_services_range == "analytics-net-services"
    status = orchestrator.status(response.request_id, who)
    assert status.status == RequestStatus.READY and status.credentials is None, "credentials are named once, in the creating response"
    assert json.dumps(orchestrator.state.get_request(response.request_id), default=str).count(password) == 0


def test_provisioning_failure_cleans_up_what_was_created_and_reports_it(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    fake.fail_on.append(("POST", "sqladmin.googleapis.com/v1/projects/test-project/instances"))
    response = orchestrator.provision(InfrastructureRequest.model_validate(FULL_REQUEST), principal(orchestrator.config), wait_seconds=45)
    assert response.status == RequestStatus.FAILED
    assert response.error.code == ErrorCode.PROVISIONING_FAILED and "at database" in response.error.message and response.error.details["cause"] == "PROVIDER_ERROR"
    cleanup = response.error.details["cleanup"]
    assert cleanup["policy"] == "delete" and len(cleanup["deleted"]) == 5 and cleanup["remaining"] == []
    assert not fake.instances and not fake.disks and not fake.networks and not fake.subnets and not fake.firewalls and not fake.peerings
    statuses = {resource.kind.value: resource.status for resource in response.resources.all()}
    assert statuses == {"network": ResourceStatus.DELETED, "storage": ResourceStatus.DELETED, "compute": ResourceStatus.DELETED, "database": ResourceStatus.FAILED,
                        "secret": ResourceStatus.PENDING}, "the password's secret was never reached"
    assert not fake.secrets and not fake.sql_users
    assert [operation.kind for operation in response.operations] == ["provision", "cleanup"] and [operation.status for operation in response.operations] == ["FAILED", "SUCCEEDED"]


def test_cleanup_names_what_it_could_not_delete(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    fake.fail_on.append(("POST", "/zones/us-central1-a/instances"))
    fake.fail_on.append(("DELETE", "/global/networks/"))
    response = orchestrator.provision(InfrastructureRequest.model_validate({**FULL_REQUEST, "database": None}), principal(orchestrator.config), wait_seconds=45)
    assert response.status == RequestStatus.FAILED and "at compute analytics-vm-1" in response.error.message
    cleanup = response.error.details["cleanup"]
    assert len(cleanup["deleted"]) == 2 and [item["name"] for item in cleanup["remaining"]] == ["analytics-net"] and cleanup["remaining"][0]["error"]["code"] == "PROVIDER_ERROR"
    assert response.operations[-1].kind == "cleanup" and response.operations[-1].status == "FAILED"


def test_cleanup_can_be_turned_off_by_the_operator(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path, config=make_config(tmp_path, cleanup_on_failure=False))
    fake.fail_on.append(("POST", "/zones/us-central1-a/instances"))
    response = orchestrator.provision(InfrastructureRequest.model_validate({**FULL_REQUEST, "database": None}), principal(orchestrator.config), wait_seconds=45)
    assert response.status == RequestStatus.FAILED and response.error.details["cleanup"]["policy"] == "kept" and len(response.error.details["cleanup"]["remaining"]) == 3
    assert fake.networks and fake.disks


def test_destroy_in_reverse_order_then_destroyed(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = orchestrator.provision(InfrastructureRequest.model_validate(FULL_REQUEST), who, wait_seconds=45)
    fake.calls.clear()
    destroyed = orchestrator.destroy(response.request_id, who, wait_seconds=45)
    deletes = [call[1] for call in fake.calls if call[0] == "DELETE"]
    order = ["secret" if "secretmanager" in url else "sqladmin" if "sqladmin" in url else "instances" if "/instances/" in url else "disks" if "/disks/" in url else "network"
             for url in deletes]
    assert order.index("secret") < order.index("sqladmin") < order.index("instances") < order.index("disks") < order.index("network")
    assert destroyed.status == RequestStatus.DESTROYED and all(resource.status == ResourceStatus.DELETED for resource in destroyed.resources.all())
    assert not fake.instances and not fake.disks and not fake.sql and not fake.networks and not fake.peerings and not fake.secrets
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.destroy(response.request_id, who)
    assert caught.value.code == ErrorCode.INVALID_STATE


def test_destroy_failure_names_the_remaining_resources_and_can_be_retried(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = orchestrator.provision(InfrastructureRequest.model_validate({**FULL_REQUEST, "database": None}), who, wait_seconds=45)
    fake.fail_on.append(("DELETE", "/disks/analytics-vm-disk-2"))
    failed = orchestrator.destroy(response.request_id, who, wait_seconds=45)
    assert failed.status == RequestStatus.DESTROY_FAILED and failed.error.code == ErrorCode.DESTROY_FAILED and [item["name"] for item in failed.error.details["remaining"]] == ["analytics-vm-disk-2"]
    assert {resource.name: resource.status for resource in failed.resources.all()}["analytics-vm-disk-2"] == ResourceStatus.FAILED
    fake.fail_on.clear()
    retried = orchestrator.destroy(response.request_id, who, wait_seconds=45)
    assert retried.status == RequestStatus.DESTROYED and not fake.disks and not fake.networks


def test_status_refreshes_from_the_provider_and_tolerates_its_absence(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = orchestrator.provision(InfrastructureRequest.model_validate(VM_REQUEST), who, wait_seconds=45)
    machine = response.resources.compute[0]
    fake.instances[(machine.zone, machine.name)]["status"] = "TERMINATED"
    assert orchestrator.status(response.request_id, who).resources.compute[0].status == ResourceStatus.STOPPED
    del fake.instances[(machine.zone, machine.name)]
    assert orchestrator.status(response.request_id, who).resources.compute[0].status == ResourceStatus.DELETED
    other = orchestrator.provision(InfrastructureRequest.model_validate(VM_REQUEST), who, wait_seconds=45)
    fake.fail_on.append(("GET", "/instances/"))
    refreshed = orchestrator.status(other.request_id, who).resources.compute[0]
    assert refreshed.status == ResourceStatus.UNKNOWN and refreshed.error.code == ErrorCode.PROVIDER_ERROR


def test_authorization_and_visibility(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    config = orchestrator.config
    reader, writer, operator = principal(config, "reader"), principal(config, "app-layer"), principal(config, "operator")
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.provision(InfrastructureRequest.model_validate(VM_REQUEST), reader)
    assert caught.value.code == ErrorCode.PERMISSION_DENIED
    response = orchestrator.provision(InfrastructureRequest.model_validate(VM_REQUEST), writer, wait_seconds=45)
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.status(response.request_id, reader)
    assert caught.value.code == ErrorCode.NOT_FOUND, "another caller's request is invisible, not merely forbidden"
    assert orchestrator.status(response.request_id, operator).owner == "app-layer"
    assert orchestrator.list_requests(reader).count == 0 and orchestrator.list_requests(writer).count == 1 and orchestrator.list_requests(operator).count == 1
    assert orchestrator.list_compute(reader)["labels"] == {"managed-by": "cloudinfra", "cloudinfra-owner": "reader"} and orchestrator.list_compute(reader)["count"] == 0
    assert orchestrator.list_compute(operator)["count"] == 1 and orchestrator.list_compute(writer, response.request_id)["count"] == 1
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.destroy(response.request_id, reader)
    assert caught.value.code == ErrorCode.PERMISSION_DENIED


def test_limits_before_any_provider_call(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path, config=make_config(tmp_path, limits={"max_monthly_cost_usd": 50.0, "max_active_requests_per_principal": 1}))
    who = principal(orchestrator.config)
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.provision(InfrastructureRequest.model_validate({"region": "us-central1", "compute": {"count": 4, "cpu": 8, "memory_gb": 32}}), who)
    assert caught.value.code == ErrorCode.COST_LIMIT_EXCEEDED and fake.calls == []
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.provision(InfrastructureRequest.model_validate({**VM_REQUEST, "provider": "aws"}), who)
    assert caught.value.code == ErrorCode.UNSUPPORTED_PROVIDER and fake.calls == []
    orchestrator.provision(InfrastructureRequest.model_validate({"region": "us-central1", "compute": {"cpu": 1, "memory_gb": 1}}), who, wait_seconds=45)
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.provision(InfrastructureRequest.model_validate({"region": "us-central1", "compute": {"cpu": 1, "memory_gb": 1}}), who)
    assert caught.value.code == ErrorCode.LIMIT_EXCEEDED and "live requests" in caught.value.message


def test_quota_headroom_is_checked_when_the_provider_reports_it(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    fake.quotas["CPUS"] = {"limit": 8, "usage": 6}
    response = orchestrator.provision(InfrastructureRequest.model_validate({"region": "us-central1", "compute": {"count": 2, "cpu": 2, "memory_gb": 8}}),
                                      principal(orchestrator.config), wait_seconds=45)
    assert response.status == RequestStatus.FAILED and response.error.details["cause"] == "QUOTA_EXCEEDED" and not fake.instances
    plan = orchestrator.plan(InfrastructureRequest.model_validate(VM_REQUEST), principal(orchestrator.config), check_quota=True)
    assert plan.quota["CPUS"]["limit"] == 8 and plan.status == RequestStatus.PLANNED and plan.estimate.monthly_usd > 0 and plan.planned[0].provider_mapping == "compute.instances.insert"


def test_plan_and_validate_create_nothing(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    plan = orchestrator.plan(InfrastructureRequest.model_validate(FULL_REQUEST), who)
    assert [item.kind for item in plan.planned] == ["network", "storage", "storage", "compute", "compute", "database", "secret"] and plan.warnings
    checked = orchestrator.validate(InfrastructureRequest.model_validate(FULL_REQUEST), who)
    assert checked.valid and checked.problems == [] and checked.request.compute.count == 2
    bad = orchestrator.validate(InfrastructureRequest.model_validate({"region": "europe-west1", "compute": {"cpu": 3, "memory_gb": 1}, "database": {}}), who)
    assert not bad.valid and [item.code for item in bad.problems] == [ErrorCode.INVALID_COMPUTE_CONFIGURATION, ErrorCode.INVALID_DATABASE_CONFIGURATION]
    assert fake.calls == [] and orchestrator.list_requests(who).count == 0


def test_primitives_compose_and_share_one_lifecycle(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    network = orchestrator.create_network(who, region="us-central1", name="shared", public_access=True, wait_seconds=45)
    network_id = network.resources.network[0].id
    machines = orchestrator.create_compute(who, region="us-central1", cpu=2, memory_gb=8, count=1, public_ip=True, network_id=network_id, wait_seconds=45)
    machine = machines.resources.compute[0]
    assert machine.status == ResourceStatus.READY and fake.instances[(machine.zone, machine.name)]["networkInterfaces"][0]["network"].endswith("/shared-net")
    database = orchestrator.create_database(who, region="us-central1", network_id=network_id, wait_seconds=45)
    assert database.status == RequestStatus.READY and database.credentials.database_password_secret and database.endpoints.database_host == "10.20.0.3"
    assert orchestrator.get_resource(network_id, who).resource.private_services_range == "shared-net-services"
    disk = orchestrator.create_storage(who, region="us-central1", size_gb=50, wait_seconds=45)
    attached = orchestrator.attach_storage(disk.resources.storage[0].id, machine.id, who)
    assert attached.resource.attached_to == machine.id and orchestrator.get_resource(machine.id, who).resource.attached_disks == [disk.resources.storage[0].id]
    rule = orchestrator.configure_firewall(network_id, [8443], ["203.0.113.0/24"], who)
    assert rule.resource.firewall_rules[-1].ports == [8443] and rule.operation.status == "SUCCEEDED"
    resized = orchestrator.resize_compute(machine.id, 4, 16, who, wait_seconds=45)
    assert resized.resource.machine_type == "e2-standard-4" and resized.operation.kind == "resize_compute" and resized.operation.status == "SUCCEEDED"
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.delete_resource(network_id, who, ResourceKind.network)
    assert caught.value.code == ErrorCode.INVALID_STATE and sorted(caught.value.details["tenants"]) == sorted([machine.id, database.resources.database[0].id])
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.destroy(network.request_id, who)
    assert caught.value.code == ErrorCode.INVALID_STATE and "other requests" in caught.value.message
    assert machine.network == network_id and database.resources.database[0].network == network_id
    gone = orchestrator.delete_resource(machine.id, who, ResourceKind.compute, wait_seconds=45)
    assert gone.status == "DELETED" and orchestrator.status(machines.request_id, who).status == RequestStatus.DESTROYED
    assert orchestrator.get_resource(disk.resources.storage[0].id, who).resource.status == ResourceStatus.READY, "a disk attached later survives its machine"
    health = orchestrator.health(network_id, who)
    assert health.healthy is True
    usage = orchestrator.usage(database.resources.database[0].id, 120, who)
    assert set(usage.metrics) == {"cpu_utilization_mean", "memory_utilization_mean", "disk_utilization_mean"}
    cost = orchestrator.cost(who, request_id=database.request_id)
    assert cost["estimate"]["monthly_usd"] > 0 and cost["actual"] is None
    by_resource = orchestrator.cost(who, resource_id=database.resources.database[0].id)
    assert by_resource["estimate"]["lines"][0]["kind"] == "database"
    operation = orchestrator.operation(rule.operation.operation_id, who)
    assert operation.kind == "configure_firewall"
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.get_resource(machine.id, who, ResourceKind.storage)
    assert caught.value.code == ErrorCode.NOT_FOUND


def test_existing_network_must_be_ready_and_in_the_same_region(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    network = orchestrator.create_network(who, region="europe-west1", wait_seconds=45)
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.create_compute(who, region="us-central1", cpu=2, memory_gb=8, network_id=network.resources.network[0].id)
    assert caught.value.code == ErrorCode.INVALID_NETWORK_CONFIGURATION
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.create_compute(who, region="us-central1", cpu=2, memory_gb=8, network_id="net-000000000000")
    assert caught.value.code == ErrorCode.NOT_FOUND


def test_service_status_names_providers_and_limits(tmp_path):
    orchestrator, _ = make_orchestrator(tmp_path)
    status = orchestrator.service_status("1.35.0")
    assert status["providers"] == {"gcp": {"project": "test-project", "allowed_regions": ["us-central1", "europe-west1"], "planned": False}}
    assert status["planned_providers"] == ["aws", "azure", "jetstream"] and status["limits"]["max_monthly_cost_usd"] == 500.0
