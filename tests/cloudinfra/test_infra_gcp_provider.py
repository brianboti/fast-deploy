"""GCPProvider against the fake API: the bodies it sends, the resources it reads back, and the errors it translates."""
import httpx
import pytest
from gcp_fakes import FakeGcpApi, make_config, make_provider

from cloudinfra.models import ComputeRequest, DatabaseRequest, InfrastructureRequest, NetworkRequest, ResourceStatus, StorageRequest
from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.orchestration import planner
from cloudinfra.providers.gcp.api import GcpApi, translate_http_error
from cloudinfra.providers.gcp.auth import GoogleToken


def planned_for(provider, config, data, request_id="a" * 32):
    request = InfrastructureRequest.model_validate(data)
    ctx = planner.context(request, request_id, "app-layer", config, provider)
    return request, ctx, planner.plan(request, ctx, provider)


def test_create_compute_body_is_least_privilege_and_labelled():
    config = make_config()
    provider, fake = make_provider(config)
    request, ctx, planned = planned_for(provider, config, {"region": "us-central1", "name": "vm", "labels": {"team": "data"}, "compute": {"cpu": 4, "memory_gb": 16, "public_ip": True}})
    resource = provider.create_compute(request.compute, planned[0], ctx, None)
    method, url, body = next(call for call in fake.calls if call[0] == "POST" and url_is_instances(call[1]))
    assert body["machineType"] == "zones/us-central1-a/machineTypes/e2-standard-4"
    assert body["disks"][0]["initializeParams"]["sourceImage"].endswith("ubuntu-2404-lts-amd64") and body["disks"][0]["initializeParams"]["diskSizeGb"] == "20"
    assert body["networkInterfaces"][0]["network"].endswith("/global/networks/default") and body["networkInterfaces"][0]["accessConfigs"][0]["type"] == "ONE_TO_ONE_NAT"
    assert body["metadata"] == {"items": [{"key": "enable-oslogin", "value": "TRUE"}, {"key": "block-project-ssh-keys", "value": "TRUE"},
                                          {"key": "serial-port-enable", "value": "FALSE"}]}
    assert body["serviceAccounts"] == [] and body["shieldedInstanceConfig"]["enableSecureBoot"] and body["deletionProtection"] is True
    assert "startup-script" not in str(body) and body["labels"]["managed-by"] == "cloudinfra" and body["labels"]["team"] == "data" and body["labels"]["cloudinfra-owner"] == "app-layer"
    assert resource.status == ResourceStatus.READY and resource.internal_ip.startswith("10.10.0.") and resource.public_ip.startswith("34.") and resource.machine_type == "e2-standard-4"
    assert resource.cpu == 4 and resource.memory_gb == 16 and resource.os_image == "ubuntu-2404-lts-amd64" and resource.id == planned[0].spec["resource_id"]


def url_is_instances(url):
    return url.endswith("/zones/us-central1-a/instances")


def test_compute_read_resize_list_and_delete():
    config = make_config()
    provider, fake = make_provider(config)
    request, ctx, planned = planned_for(provider, config, {"region": "us-central1", "name": "vm", "compute": {"cpu": 2, "memory_gb": 8}})
    resource = provider.create_compute(request.compute, planned[0], ctx, None)
    resized = provider.resize_compute(resource, 4, 16, ctx)
    actions = [call[1].rsplit("/", 1)[-1] for call in fake.calls if call[0] == "POST" and "/instances/vm-vm-1/" in call[1]]
    assert actions == ["stop", "setMachineType", "start"] and resized.machine_type == "e2-standard-4" and resized.cpu == 4 and resized.status == ResourceStatus.READY
    with pytest.raises(InfrastructureError) as caught:
        provider.resize_compute(resource, 3, 1, ctx)
    assert caught.value.code == ErrorCode.INVALID_COMPUTE_CONFIGURATION
    listed = provider.list_compute({"managed-by": "cloudinfra", "cloudinfra-owner": "app-layer"})
    assert [item.name for item in listed] == ["vm-vm-1"] and listed[0].request_id == "a" * 32
    assert provider.list_compute({"managed-by": "cloudinfra", "cloudinfra-owner": "someone-else"}) == []
    provider.delete_compute(resource, ctx)
    assert provider.get_compute(resource).status == ResourceStatus.DELETED
    provider.delete_compute(resource, ctx)  # a second delete of a gone machine is not an error


def test_storage_disk_attach_and_bucket():
    config = make_config()
    provider, fake = make_provider(config)
    request, ctx, planned = planned_for(provider, config, {"region": "us-central1", "name": "data", "compute": {"cpu": 2, "memory_gb": 8}, "storage": {"size_gb": 200, "performance": "ssd"}})
    disk = provider.create_storage(request.storage, planned[0], ctx)
    machine = provider.create_compute(request.compute, planned[1], ctx, None)
    attached = provider.attach_storage(disk, machine, ctx)
    body = next(call[2] for call in fake.calls if call[1].endswith("/attachDisk"))
    assert body == {"source": disk.provider_id, "deviceName": "data-vm-disk-1", "autoDelete": False} and attached.attached_to == machine.id
    disk_body = next(call[2] for call in fake.calls if call[0] == "POST" and call[1].endswith("/disks"))
    assert disk_body["type"].endswith("/pd-ssd") and disk_body["sizeGb"] == "200" and disk_body["labels"]["managed-by"] == "cloudinfra"
    with pytest.raises(InfrastructureError) as caught:
        provider.attach_storage(disk, machine.model_copy(update={"zone": "us-central1-b"}), ctx)
    assert caught.value.code == ErrorCode.INVALID_STORAGE_CONFIGURATION
    request, ctx, planned = planned_for(provider, config, {"region": "us-central1", "name": "obj", "storage": {"type": "object", "size_gb": 50}})
    bucket = provider.create_storage(request.storage, planned[0], ctx)
    bucket_body = next(call[2] for call in fake.calls if call[1].endswith("/storage/v1/b"))
    assert bucket.url == "gs://test-project-obj-aaaaaa" and bucket_body["iamConfiguration"]["publicAccessPrevention"] == "enforced"
    fake.bucket_objects.add(bucket.name)
    with pytest.raises(InfrastructureError) as caught:
        provider.delete_storage(bucket, ctx)
    assert caught.value.code == ErrorCode.PROVIDER_CONFLICT and "no data is destroyed" in caught.value.message
    fake.bucket_objects.clear()
    provider.delete_storage(bucket, ctx)
    assert provider.get_storage(bucket).status == ResourceStatus.DELETED


def test_network_rules_private_services_and_teardown():
    config = make_config()
    provider, fake = make_provider(config)
    request, ctx, planned = planned_for(provider, config, {"region": "us-central1", "name": "web", "network": {"public_access": True, "allowed_ports": [443], "allowed_sources": ["203.0.113.0/24"]},
                                                          "database": {}})
    network = provider.create_network(request.network, planned[0], ctx)
    rules = {rule.name: rule for rule in network.firewall_rules}
    assert set(rules) == {"web-net-allow-internal", "web-net-allow-iap-ssh", "web-net-allow-public"}
    assert rules["web-net-allow-iap-ssh"].sources == ["35.235.240.0/20"] and rules["web-net-allow-public"].ports == [443] and rules["web-net-allow-public"].sources == ["203.0.113.0/24"]
    bodies = {call[2]["name"]: call[2] for call in fake.calls if call[0] == "POST" and call[1].endswith("/global/firewalls")}
    assert all(body["direction"] == "INGRESS" for body in bodies.values()) and not any("0.0.0.0/0" in body["sourceRanges"] and "22" in str(body["allowed"]) for body in bodies.values())
    subnet = next(call[2] for call in fake.calls if call[1].endswith("/subnetworks"))
    assert subnet["ipCidrRange"] == "10.10.0.0/24" and subnet["privateIpGoogleAccess"] is True and network.private_services_range is None
    prepared = provider.prepare_network_for_database(network, ctx)
    assert prepared.private_services_range == "web-net-services" and "projects/123456789/global/networks/web-net" in fake.peerings
    assert provider.prepare_network_for_database(prepared, ctx) == prepared and len([c for c in fake.calls if c[1].endswith("/connections")]) == 1
    extra = provider.configure_firewall(prepared, [8080], ["10.0.0.0/8"], ctx)
    assert extra.name == "web-net-allow-4" and extra.ports == [8080]
    provider.delete_network(prepared.model_copy(update={"firewall_rules": [*prepared.firewall_rules, extra]}), ctx)
    assert not fake.networks and not fake.subnets and not fake.firewalls and not fake.addresses and not fake.peerings
    assert provider.get_network(prepared).status == ResourceStatus.DELETED


def test_database_private_encrypted_protected_and_password_handed_over_once():
    config = make_config()
    provider, fake = make_provider(config)
    request, ctx, planned = planned_for(provider, config, {"region": "us-central1", "name": "pg", "network": {}, "database": {"engine": "postgresql", "size": "medium", "storage_gb": 20}})
    network = provider.prepare_network_for_database(provider.create_network(request.network, planned[0], ctx), ctx)
    ctx.database_password = "generated-by-the-orchestrator"
    database, password = provider.create_database(request.database, planned[1], ctx, network)
    body = next(call[2] for call in fake.calls if call[0] == "POST" and call[1].endswith("/instances") and "sqladmin" in call[1])
    settings = body["settings"]
    assert body["databaseVersion"] == "POSTGRES_18" and settings["tier"] == "db-custom-2-7680" and settings["deletionProtectionEnabled"] is True
    assert settings["ipConfiguration"] == {"ipv4Enabled": False, "sslMode": "ENCRYPTED_ONLY", "privateNetwork": network.provider_id}
    assert settings["backupConfiguration"]["enabled"] and settings["userLabels"]["managed-by"] == "cloudinfra"
    # the default user gets a password nobody keeps (S2, M13): never one without, never the app's
    assert len(body["rootPassword"]) >= 32 and body["rootPassword"] != "generated-by-the-orchestrator"
    assert password == "generated-by-the-orchestrator" and fake.sql_users["pg-db-aaaaaa"] == [{"name": "app", "password": password}]
    assert database.host == "10.20.0.3" and database.port == 5432 and database.status == ResourceStatus.READY and database.connection_name.endswith(":pg-db-aaaaaa")
    assert password not in database.model_dump_json() and database.password_secret is None  # the orchestrator links the secret it makes
    assert body["rootPassword"] not in database.model_dump_json()
    provider.delete_database(database, ctx)
    patched = next(call[2] for call in fake.calls if call[0] == "PATCH")
    assert patched == {"settings": {"deletionProtectionEnabled": False}} and provider.get_database(database).status == ResourceStatus.DELETED


def test_database_without_network_is_refused_and_public_uses_primary_address():
    config = make_config()
    provider, fake = make_provider(config)
    request, ctx, planned = planned_for(provider, config, {"region": "us-central1", "database": {"engine": "mysql", "public_access": True}})
    with pytest.raises(InfrastructureError):
        provider.create_database(DatabaseRequest(), planned[0], ctx, None)
    database, _ = provider.create_database(request.database, planned[0], ctx, None)
    assert database.host == "35.0.0.9" and database.port == 3306 and database.version == "8.4"


def test_health_usage_and_quota():
    config = make_config()
    provider, fake = make_provider(config)
    request, ctx, planned = planned_for(provider, config, {"region": "us-central1", "compute": {"cpu": 2, "memory_gb": 8}})
    machine = provider.create_compute(request.compute, planned[0], ctx, None)
    health = provider.resource_health(machine)
    assert health.healthy is True and health.status == "READY" and health.detail["internal_ip"]
    usage = provider.resource_usage(machine, 30)
    assert usage.metrics == {"cpu_utilization_mean": 0.42, "cpu_utilization_max": 0.42} and usage.window_minutes == 30
    params = next(call for call in fake.calls if "timeSeries" in call[1])
    assert params[0] == "GET"
    assert provider.quota("us-central1")["CPUS"] == {"limit": 24, "usage": 1}
    fake.fail_on.append(("GET", "/regions/us-central1"))
    assert provider.quota("us-central1") == {}


def test_validate_shapes_zones_and_regions():
    provider, _ = make_provider(make_config())
    assert provider.zones("us-central1") == ["us-central1-a", "us-central1-b", "us-central1-c", "us-central1-f"] and "europe-west1" in provider.regions()
    assert provider.validate(InfrastructureRequest(region="us-central1", compute=ComputeRequest(cpu=4, memory_gb=16))) == []
    found = provider.validate(InfrastructureRequest(region="us-central1", zone="us-central1-z", compute=ComputeRequest(cpu=3, memory_gb=16),
                                                    database=DatabaseRequest(version="99", public_access=True), storage=StorageRequest(), network=NetworkRequest()))
    assert [item.code for item in found] == [ErrorCode.INVALID_REGION, ErrorCode.INVALID_COMPUTE_CONFIGURATION, ErrorCode.INVALID_DATABASE_CONFIGURATION]


# ---- the real client's error translation and operation waits, with a transport that answers from here
def client_answering(handler):
    return GcpApi("test-project", token=lambda: "t", client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)


@pytest.mark.parametrize("status, body, code", [
    (401, {"error": {"message": "Invalid Credentials"}}, ErrorCode.PROVIDER_AUTH_FAILED),
    (403, {"error": {"message": "Quota 'CPUS' exceeded. Limit: 24.0 in region us-central1.", "errors": [{"reason": "quotaExceeded"}]}}, ErrorCode.QUOTA_EXCEEDED),
    (403, {"error": {"message": "Required 'compute.instances.create' permission", "errors": [{"reason": "forbidden"}]}}, ErrorCode.PROVIDER_PERMISSION_DENIED),
    (404, {"error": {"message": "The resource was not found"}}, ErrorCode.NOT_FOUND),
    (409, {"error": {"message": "already exists", "errors": [{"reason": "alreadyExists"}]}}, ErrorCode.PROVIDER_CONFLICT),
    (429, {"error": {"message": "slow down"}}, ErrorCode.PROVIDER_UNAVAILABLE),
    (503, "unavailable", ErrorCode.PROVIDER_UNAVAILABLE),
    (400, {"error": {"message": "Invalid value for field 'resource.name'"}}, ErrorCode.PROVIDER_ERROR),
])
def test_http_errors_become_structured_errors(status, body, code):
    response = httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)
    error = translate_http_error(response)
    assert error.code == code and error.details["http_status"] == status and "provider_message" in error.details


def test_request_errors_and_token_failures():
    def unreachable(request):
        raise httpx.ConnectError("no route")
    with pytest.raises(InfrastructureError) as caught:
        client_answering(unreachable).get("https://compute.googleapis.com/compute/v1/projects/p/zones/z/instances/x")
    assert caught.value.code == ErrorCode.PROVIDER_UNAVAILABLE

    def never(request):
        raise AssertionError("no request without a token")
    api = GcpApi("p", token=GoogleToken(httpx.Client(transport=httpx.MockTransport(unreachable)), runner=lambda args: (_ for _ in ()).throw(OSError("no gcloud"))),
                 client=httpx.Client(transport=httpx.MockTransport(never)))
    with pytest.raises(InfrastructureError) as caught:
        api.get("https://compute.googleapis.com/x")
    assert caught.value.code == ErrorCode.PROVIDER_AUTH_FAILED and "gcloud auth login" in caught.value.message


def test_google_token_impersonates_when_configured():
    commands = []
    token = GoogleToken(httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404))), runner=lambda args: commands.append(args) or "tok",
                        impersonate="svc@p.iam.gserviceaccount.com")
    assert token() == "tok" and commands == [["gcloud", "auth", "print-access-token", "--impersonate-service-account", "svc@p.iam.gserviceaccount.com"]]
    assert token() == "tok" and len(commands) == 1  # cached


def test_operation_waits_poll_and_report_failures():
    answers = iter([{"status": "RUNNING", "name": "op", "selfLink": "https://compute.googleapis.com/op"}, {"status": "DONE", "name": "op"}])
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path.endswith("/wait"):
            return httpx.Response(200, json=next(answers))
        return httpx.Response(200, json={"status": "RUNNING", "name": "op", "selfLink": "https://compute.googleapis.com/op"})
    api = client_answering(handler)
    done = api.wait_compute({"status": "RUNNING", "name": "op", "selfLink": "https://compute.googleapis.com/op"})
    assert done["status"] == "DONE" and calls == ["https://compute.googleapis.com/op/wait", "https://compute.googleapis.com/op/wait"]
    with pytest.raises(InfrastructureError) as caught:
        api.wait_compute({"status": "DONE", "name": "op", "error": {"errors": [{"code": "QUOTA_EXCEEDED", "message": "Quota 'CPUS' exceeded"}]}})
    assert caught.value.code == ErrorCode.PROVIDER_ERROR and "Quota" in caught.value.message
    slow = GcpApi("p", token=lambda: "t", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "PENDING", "name": "sql"}))),
                  sleep=lambda s: None, clock=iter(range(0, 10_000, 400)).__next__, operation_timeout=900)
    with pytest.raises(InfrastructureError) as caught:
        slow.wait_sql({"status": "PENDING", "name": "sql"})
    assert caught.value.code == ErrorCode.OPERATION_TIMEOUT


def test_fake_is_honest_about_unknown_endpoints():
    with pytest.raises(AssertionError):
        FakeGcpApi().get("https://compute.googleapis.com/compute/v1/projects/p/nothing")
