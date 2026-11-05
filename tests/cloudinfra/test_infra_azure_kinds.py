"""The Azure adapter's second step (docs/research/RESEARCH-2026-11-03-azure.md, section 10): release A's kinds on Azure
(identity, network, storage, compute), the request's resource group, members resolved to object ids before anything is
made, grants as role assignments, deallocate and start, resize, the sweeper's run limit, connect blocks, metrics, quota
and prices. Every test runs against the fake Azure in azure_fakes.py; none touches a cloud."""
import datetime
import json

import httpx
import pytest
from azure_fakes import (
    AZURE_FULL_REQUEST,
    AZURE_VM_REQUEST,
    BASTION,
    DEFAULT_SUBNET,
    GROUPS,
    GUESTS,
    SUBSCRIPTION,
    USERS,
    make_azure_config,
    make_azure_orchestrator,
    make_azure_provider,
    refusal,
    usage,
)
from gcp_fakes import config_dict, principal

from cloudinfra import cli
from cloudinfra.models import InfrastructureRequest, RequestStatus, ResourceKind, ResourceStatus
from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.models.request import ComputeRequest
from cloudinfra.orchestration import planner
from cloudinfra.orchestration.sweeper import Sweeper
from cloudinfra.providers.azure import access, pricing, translate
from cloudinfra.providers.azure.access import BLOB_CONTRIBUTOR, BLOB_OWNER, READER, VM_ADMIN_LOGIN, VM_USER_LOGIN, assignment_name
from cloudinfra.security import validation

BASTION_SETTINGS = {"bastion": BASTION, "bastion_subnet": "10.0.1.0/26"}
SERVICE_PRINCIPAL = "99999999-9999-4999-8999-999999999999"


def provision(orchestrator, request: dict, who=None):
    return orchestrator.provision(InfrastructureRequest.model_validate(request), who or principal(orchestrator.config), wait_seconds=45)


def puts(fake, kind: str) -> list[dict]:
    """The bodies of every PUT of one resource type (the segment before the resource's name), in order."""
    return [call[2] for call in fake.calls if call[0] == "PUT" and call[1].split("?")[0].split("/")[-2] == kind]


def writes(fake) -> list[tuple[str, str]]:
    """(method, resource type and name) of every write, in order: virtualMachines/analytics-vm-1."""
    return [(method, "/".join(path.split("/")[-2:])) for method, path in fake.order]


# ---- sizes, images and what validation refuses

def test_a_shape_azure_sells_becomes_its_size_and_any_other_is_refused_with_the_sizes_there_are():
    sizes = {(cpu, memory): translate.compute_shape(ComputeRequest(cpu=cpu, memory_gb=memory)) for cpu, memory in ((2, 8), (4, 32), (2, 4), (1, 2), (20, 160), (96, 672))}
    assert {shape: size.name for shape, size in sizes.items()} == {(2, 8): "Standard_D2s_v5", (4, 32): "Standard_E4s_v5", (2, 4): "Standard_D2ls_v5",
                                                                   (1, 2): "Standard_B1ms", (20, 160): "Standard_E20s_v5", (96, 672): "Standard_E96s_v5"}
    assert translate.compute_shape(ComputeRequest(cpu=2, memory_gb=6)).message == "With 2 vCPUs, Azure's sizes have 4, 8 or 16 GB of memory"
    odd = translate.compute_shape(ComputeRequest(cpu=3, memory_gb=12))
    assert odd.code == ErrorCode.INVALID_COMPUTE_CONFIGURATION and "there are no custom shapes" in odd.message and "1, 2, 4, 8, 16, 20, 32, 48, 64 or 96 vCPUs" in odd.message
    assert translate.compute_shape(ComputeRequest(cpu=1, memory_gb=4)).message == "With 1 vCPUs, Azure's sizes have 1 or 2 GB of memory"


def test_gpus_get_their_familys_smallest_covering_size_l4_is_refused_and_nd_sizes_boot_without_trusted_launch():
    def shape(gpu, count, cpu, memory):
        return translate.compute_shape(ComputeRequest(cpu=cpu, memory_gb=memory, accelerator={"type": gpu, "count": count}))
    assert shape("t4", 1, 4, 16).name == "Standard_NC4as_T4_v3" and shape("t4", 1, 8, 40).name == "Standard_NC8as_T4_v3" and shape("t4", 4, 8, 32).gpus == 4
    assert shape("a100", 8, 8, 64).name == "Standard_ND96asr_v4" and not shape("a100", 8, 8, 64).trusted_launch and shape("h100", 2, 8, 64).name == "Standard_NC80adis_H100_v5"
    assert shape("t4", 2, 4, 16).message == "On Azure, t4 comes 1 or 4 per machine" and shape("a100", 3, 4, 16).field == "compute.accelerator.count"
    assert shape("l4", 1, 4, 16).message == "Azure has no NVIDIA L4: ask for t4, a100 or h100" and shape("l4", 1, 4, 16).field == "compute.accelerator.type"
    assert "come with 4 vCPUs and 28 GB; 8 vCPUs and 56 GB; 16 vCPUs and 110 GB" in shape("t4", 1, 32, 64).message


def test_machines_need_a_network_and_disks_azures_size_and_the_problems_are_named_before_any_call(tmp_path):
    provider, fake = make_azure_provider(make_azure_config())

    def problems(**request):
        return provider.validate(InfrastructureRequest.model_validate({"provider": "azure", "region": "eastus2", **request}))
    assert [item.field for item in problems(compute={"cpu": 2, "memory_gb": 8})] == ["compute.network_id"], "Azure has no default network"
    assert problems(network={}, compute={"cpu": 2, "memory_gb": 8}) == [] and problems(compute={"cpu": 2, "memory_gb": 8, "network_id": "net-000000000000"}) == []
    with_subnet, _ = make_azure_provider(make_azure_config(azure={"default_subnet": DEFAULT_SUBNET}))
    assert with_subnet.validate(InfrastructureRequest.model_validate({"provider": "azure", "region": "eastus2", "compute": {"cpu": 2, "memory_gb": 8}})) == []
    assert [item.message for item in problems(network={}, compute=[{"group": "a", "cpu": 3, "memory_gb": 3}, {"group": "b", "cpu": 2, "memory_gb": 8, "profile": "nope"}])] == \
        ["a: Azure's sizes have 1, 2, 4, 8, 16, 20, 32, 48, 64 or 96 vCPUs, with 2, 4 or 8 GB each (1 vCPU: 1 or 2 GB); there are no custom shapes",
         "b: Unknown profile 'nope'; profiles: none configured"]
    assert problems(storage={"size_gb": 40000})[0].message == "Azure's managed disks hold at most 32767 GB; 40000 asked" and problems(storage={"type": "object", "size_gb": 40000}) == []
    assert fake.calls == []


def test_profiles_name_marketplace_plans_and_gallery_images_and_a_small_boot_disk_is_raised_to_the_images():
    gallery = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/images/providers/Microsoft.Compute/galleries/approved/images/hardened/versions/1.4.0"
    config = make_azure_config(azure={"profiles": {"hardened": {"image": gallery, "min_boot_disk_gb": 64, "description": "CIS level 1"},
                                                   "gpu": {"image": "microsoft-dsvm:ubuntu-hpc:2204:latest", "accelerator": "required"}},
                                      "default_accelerator_profile": "gpu"})
    provider, _ = make_azure_provider(config)

    def compute(**group):
        request = InfrastructureRequest.model_validate({"provider": "azure", "region": "eastus2", "network": {}, "compute": group})
        ctx = planner.context(request, "0123456789abcdef0123456789abcdef", "app-layer", config, provider)
        return next(item for item in provider.plan(request, ctx) if item.kind == "compute"), request
    rocky, _ = compute(cpu=2, memory_gb=8, os="rocky")
    assert rocky.spec["image"] == "resf:rockylinux-x86_64:9-base:latest" and rocky.spec["plan"] is True and rocky.spec["boot_disk_gb"] == 30
    hardened, request = compute(cpu=2, memory_gb=8, profile="hardened")
    assert (hardened.spec["image"], hardened.spec["boot_disk_gb"], hardened.spec["secure_boot"]) == (gallery, 64, True)
    assert translate.image_reference(gallery) == {"id": gallery} and translate.plan_of(rocky.spec["image"]) == {"name": "9-base", "publisher": "resf", "product": "rockylinux-x86_64"}
    gpu, _ = compute(cpu=4, memory_gb=16, accelerator={"type": "t4"})
    assert (gpu.spec["profile"], gpu.spec["drivers"], gpu.spec["secure_boot"]) == ("gpu", True, True), "a GPU profile with its drivers keeps Secure Boot"
    plain, request = compute(cpu=4, memory_gb=16, os="ubuntu", accelerator={"type": "t4"})
    assert (plain.spec["drivers"], plain.spec["secure_boot"], plain.spec["trusted_launch"]) == (False, False, True)
    assert any("the minimum of the provider's image, not the 20 GB asked" in item for item in planner.warnings(
        InfrastructureRequest.model_validate({"provider": "azure", "region": "eastus2", "network": {}, "compute": {"cpu": 2, "memory_gb": 8, "boot_disk_gb": 20}}),
        [compute(cpu=2, memory_gb=8, boot_disk_gb=20)[0]], config, provider))


def test_the_plan_names_every_resource_in_creation_order_with_its_family_quota():
    config = make_azure_config(azure=BASTION_SETTINGS)
    provider, fake = make_azure_provider(config)

    def plan(request: dict):
        request = InfrastructureRequest.model_validate(request)
        ctx = planner.context(request, "0123456789abcdef0123456789abcdef", "app-layer", config, provider)
        return provider.plan(request, ctx), ctx
    planned, ctx = plan({**AZURE_FULL_REQUEST, "identity": {}})
    assert [(item.kind, item.name) for item in planned] == [("identity", "analytics-app"), ("network", "analytics-net"), ("storage", "analytics-vm-disk-1"),
                                                            ("storage", "analytics-vm-disk-2"), ("compute", "analytics-vm-1"), ("compute", "analytics-vm-2")]
    network, disk, machine = planned[1].spec, planned[2].spec, planned[4].spec
    assert network["firewall_rules"] == ["analytics-net-allow-internal", "analytics-net-allow-bastion-ssh", "analytics-net-allow-public", "analytics-net-allow-probes",
                                         "analytics-net-deny-inbound"] and network["nat"] and ctx.zone == "1"
    assert (disk["disk_sku"], disk["attach_to"], disk["zone"]) == ("Premium_LRS", "analytics-vm-1", "1")
    assert (machine["machine_type"], machine["cpu_quota"], machine["public_ip"], machine["trusted_launch"]) == ("Standard_D4s_v5", "standardDSv5Family", True, True)
    [bucket], ctx = plan({"provider": "azure", "region": "westus", "name": "data-team", "storage": {"type": "object"}})
    assert bucket.name == "datateam01234567" and bucket.spec["redundancy"] == "Standard_LRS" and ctx.zone is None, "westus has no zones: locally redundant"
    assert plan({"provider": "azure", "region": "eastus2", "storage": {"type": "object"}})[0][0].spec["redundancy"] == "Standard_ZRS"
    assert fake.calls == [], "plan is pure"


# ---- provisioning, cleanup and destroy

def test_design_test_one_ubuntu_vm_on_azure_and_nothing_about_an_application(tmp_path):
    """The same request as Google's design test, on Azure: one machine, made private, signed in to with Entra, nothing about an application."""
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, azure=BASTION_SETTINGS))
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "network": {}, "compute": {"cpu": 4, "memory_gb": 16, "os": "ubuntu"}})
    assert response.status == RequestStatus.READY and response.provider == "azure" and response.zone == "1"
    [machine] = response.resources.compute
    assert machine.machine_type == "Standard_D4s_v5" and machine.os_image == "Canonical:ubuntu-24_04-lts:server:latest" and machine.internal_ip == "10.10.0.4"
    assert machine.public_ip is None and machine.instance_id and machine.secure_boot is True and machine.boot_disk_gb == 30
    [body] = puts(fake, "virtualMachines")
    properties = body["properties"]
    assert body["zones"] == ["1"] and body["identity"] == {"type": "SystemAssigned"} and "plan" not in body
    assert properties["securityProfile"] == {"securityType": "TrustedLaunch", "uefiSettings": {"secureBootEnabled": True, "vTpmEnabled": True}}
    linux = properties["osProfile"]["linuxConfiguration"]
    assert linux["disablePasswordAuthentication"] is True and "adminPassword" not in properties["osProfile"] and linux["ssh"]["publicKeys"][0]["keyData"].startswith("ssh-rsa ")
    assert "customData" not in properties["osProfile"] and "userData" not in properties
    assert properties["storageProfile"]["osDisk"]["deleteOption"] == "Delete" and properties["storageProfile"]["osDisk"]["managedDisk"]["storageAccountType"] == "StandardSSD_LRS"
    interface = properties["networkProfile"]["networkInterfaceConfigurations"][0]
    assert interface["properties"]["deleteOption"] == "Delete" and "publicIPAddressConfiguration" not in interface["properties"]["ipConfigurations"][0]["properties"]
    [extension] = puts(fake, "extensions")
    assert extension["properties"]["type"] == "AADSSHLoginForLinux" and extension["properties"]["publisher"] == "Microsoft.Azure.ActiveDirectory"
    group = f"infra-{response.request_id[:8]}-{response.request_id[:8]}"
    assert machine.provider_id == f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{group}/providers/Microsoft.Compute/virtualMachines/{machine.name}"
    [group_body] = [call[2] for call in fake.calls if call[0] == "PUT" and call[1].endswith(f"/resourceGroups/{group}")]
    assert group_body["tags"] == {"managed-by": "cloudinfra", "cloudinfra-request": response.request_id, "cloudinfra-name": response.name, "cloudinfra-owner": "app-layer"}
    assert body["tags"]["cloudinfra-group"] == "vm" and all(call[1].startswith("https://management.azure.com/") for call in fake.calls)
    assert not any(word in json.dumps([call[2] for call in fake.calls], default=str).lower() for word in ("github", "docker", "repository", "startup-script", "password\":"))


def test_the_network_admits_its_subnet_the_bastion_and_its_public_ports_then_refuses_the_rest(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, azure=BASTION_SETTINGS))
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "web", "network": {"public_access": True, "allowed_ports": [443],
                                                                                                         "allowed_sources": ["203.0.113.0/24"]}})
    [network] = response.resources.network
    [nsg] = puts(fake, "networkSecurityGroups")
    rules = {rule["name"]: rule["properties"] for rule in nsg["properties"]["securityRules"]}
    assert {name: (rule["priority"], rule["direction"], rule["access"]) for name, rule in rules.items()} == {
        "web-net-allow-internal": (100, "Inbound", "Allow"), "web-net-allow-bastion-ssh": (110, "Inbound", "Allow"), "web-net-allow-public": (120, "Inbound", "Allow"),
        "web-net-allow-probes": (3900, "Inbound", "Allow"), "web-net-deny-inbound": (4000, "Inbound", "Deny")}
    assert (rules["web-net-allow-bastion-ssh"]["sourceAddressPrefix"], rules["web-net-allow-bastion-ssh"]["destinationPortRanges"]) == ("10.0.1.0/26", ["22"])
    assert (rules["web-net-allow-public"]["sourceAddressPrefix"], rules["web-net-allow-public"]["destinationPortRanges"]) == ("203.0.113.0/24", ["443"])
    [vnet] = puts(fake, "virtualNetworks")
    subnet = vnet["properties"]["subnets"][0]["properties"]
    assert subnet["defaultOutboundAccess"] is False and subnet["natGateway"]["id"] == network.router and subnet["networkSecurityGroup"]["id"].endswith("/web-net-nsg")
    [address], [gateway] = puts(fake, "publicIPAddresses"), puts(fake, "natGateways")
    assert address["zones"] == gateway["zones"] == ["1"] and address["sku"]["name"] == "Standard" and gateway["properties"]["publicIpAddresses"][0]["id"].endswith("/web-net-nat-ip")
    assert [rule.name for rule in network.firewall_rules] == list(rules) and network.nat and network.subnet.endswith("/virtualNetworks/web-net/subnets/web-subnet")
    assert writes(fake)[:5] == [("PUT", f"resourceGroups/web-{response.request_id[:8]}"), ("PUT", "networkSecurityGroups/web-net-nsg"),
                                ("PUT", "publicIPAddresses/web-net-nat-ip"), ("PUT", "natGateways/web-net-nat"), ("PUT", "virtualNetworks/web-net")]
    rule = orchestrator.configure_firewall(network.id, [8443], ["10.20.0.0/16"], principal(orchestrator.config))
    added = fake.calls[-1]
    assert rule.resource.firewall_rules[-1].name == "web-net-allow-1" and added[1].endswith("/networkSecurityGroups/web-net-nsg/securityRules/web-net-allow-1")
    assert added[2]["properties"]["priority"] == 200 and added[2]["properties"]["destinationPortRanges"] == ["8443"]


def test_without_a_bastion_no_ssh_rule_and_with_egress_denied_no_nat_and_only_entra_out(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, azure={"deny_egress": True}))
    response = provision(orchestrator, {"provider": "azure", "region": "westus", "name": "quiet", "network": {}})
    [network] = response.resources.network
    names = [rule.name for rule in network.firewall_rules]
    assert "quiet-net-allow-bastion-ssh" not in names and names[-3:] == ["quiet-net-allow-egress-internal", "quiet-net-allow-egress-entra", "quiet-net-deny-egress"]
    rules = {rule["name"]: rule["properties"] for rule in puts(fake, "networkSecurityGroups")[0]["properties"]["securityRules"]}
    assert rules["quiet-net-allow-egress-entra"]["destinationAddressPrefix"] == "AzureActiveDirectory" and rules["quiet-net-deny-egress"]["destinationAddressPrefix"] == "Internet"
    assert not network.nat and network.router is None and puts(fake, "natGateways") == [] and network.zone is None, "westus has no zones"


def test_a_flow_log_goes_to_the_operators_account_through_the_regions_watcher_and_goes_with_the_network(tmp_path):
    account = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/logs/providers/Microsoft.Storage/storageAccounts/flowlogs01"
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, azure={"flow_logs_storage_account": account}))
    watcher_group = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/NetworkWatcherRG"
    fake.resources[watcher_group.lower()] = {"id": watcher_group, "name": "NetworkWatcherRG", "type": "Microsoft.Resources/resourceGroups", "properties": {}, "_top": False}
    fake.refuse[("PUT", "/flowLogs/")] = [refusal(404, "NotFound", "Network watcher not found")]  # Azure enables it a moment after the first network
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "logged", "network": {}})
    [network] = response.resources.network
    assert network.flow_log == f"{watcher_group}/providers/Microsoft.Network/networkWatchers/NetworkWatcher_eastus2/flowLogs/logged-net-{response.request_id[:8]}-flowlog"
    body = puts(fake, "flowLogs")[-1]  # the first was refused while the watcher was not there yet
    assert body["properties"]["targetResourceId"] == network.provider_id and body["properties"]["storageId"] == account and body["properties"]["enabled"]
    destroyed = orchestrator.destroy(response.request_id, principal(orchestrator.config), wait_seconds=45)
    assert destroyed.status == RequestStatus.DESTROYED and not fake.of_type("Microsoft.Network/networkWatchers/flowLogs")


def test_a_full_request_is_made_in_order_its_disks_attached_and_its_endpoints_answered(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, azure=BASTION_SETTINGS))
    response = provision(orchestrator, AZURE_FULL_REQUEST)
    assert response.status == RequestStatus.READY and response.labels == {"team": "data"}
    kinds = [name.split("/")[0] for method, name in writes(fake) if method == "PUT"]
    assert kinds.index("virtualNetworks") < kinds.index("disks") < kinds.index("virtualMachines") < kinds.index("extensions")
    machines, disks = response.resources.compute, response.resources.storage
    assert [disk.attached_to for disk in disks] == [machine.id for machine in machines] and [machine.attached_disks for machine in machines] == [[disk.id] for disk in disks]
    attached = fake.machine("analytics-vm-1")["properties"]["storageProfile"]["dataDisks"]
    assert attached == [{"lun": 0, "name": "analytics-vm-disk-1", "createOption": "Attach", "managedDisk": {"id": disks[0].provider_id}, "deleteOption": "Detach", "caching": "None"}]
    assert puts(fake, "disks")[0]["sku"] == {"name": "Premium_LRS"} and puts(fake, "disks")[0]["zones"] == ["1"]
    assert sorted(response.endpoints.public_ips) == ["20.1.2.11", "20.1.2.12"] and response.endpoints.internal_ips == ["10.10.0.4", "10.10.0.5"]
    assert response.estimated_monthly_cost_usd > 0 and response.error is None


def test_a_failure_deletes_what_was_made_and_the_requests_group_with_it(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    fake.fail_on.append(("PUT", "/virtualMachines/analytics-vm-2"))
    response = provision(orchestrator, AZURE_FULL_REQUEST)
    assert response.status == RequestStatus.FAILED and "at compute analytics-vm-2" in response.error.message
    cleanup = response.error.details["cleanup"]
    assert cleanup["policy"] == "delete" and len(cleanup["deleted"]) == 5 and cleanup["remaining"] == []
    assert fake.resources == {}, "everything went, the resource group last"
    assert writes(fake)[-1] == ("DELETE", f"resourceGroups/analytics-{response.request_id[:8]}")


def test_destroy_deletes_in_reverse_then_the_group(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, {**AZURE_FULL_REQUEST, "identity": {}})
    fake.order.clear()
    destroyed = orchestrator.destroy(response.request_id, who, wait_seconds=45)
    deletes = [name.split("/")[0] for method, name in writes(fake) if method == "DELETE"]
    assert deletes.index("virtualMachines") < deletes.index("disks") < deletes.index("virtualNetworks") < deletes.index("userAssignedIdentities") < deletes.index("resourceGroups")
    assert destroyed.status == RequestStatus.DESTROYED and all(item.status == ResourceStatus.DELETED for item in destroyed.resources.all()) and fake.resources == {}


def test_a_group_that_holds_something_this_service_did_not_make_is_kept_and_the_destroy_says_so(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "kept", "network": {}})
    group = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/kept-{response.request_id[:8]}"
    foreign = f"{group}/providers/Microsoft.Network/publicIPAddresses/by-hand"
    fake.resources[foreign.lower()] = {"id": foreign, "name": "by-hand", "type": "Microsoft.Network/publicIPAddresses", "tags": {}, "properties": {}, "_top": True}
    failed = orchestrator.destroy(response.request_id, who, wait_seconds=45)
    assert failed.status == RequestStatus.DESTROY_FAILED and failed.error.message.startswith(f"Every resource was deleted, but not resource group kept-{response.request_id[:8]}")
    assert "by-hand" in failed.error.message and failed.error.details["remaining"][0]["resource_id"] is None
    del fake.resources[foreign.lower()]
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED and fake.resources == {}


def test_status_reads_the_power_state_and_a_vanished_machine(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, AZURE_VM_REQUEST)
    machine = response.resources.compute[0]
    fake.machine(machine.name)["_power"] = "deallocated"
    assert orchestrator.status(response.request_id, who).resources.compute[0].status == ResourceStatus.STOPPED
    fake.machine(machine.name)["_power"] = "starting"
    assert orchestrator.status(response.request_id, who).resources.compute[0].status == ResourceStatus.UPDATING
    fake.machine(machine.name)["_power"] = "stopped"  # shut down from inside: still allocated, still billed in full
    allocated = orchestrator.status(response.request_id, who).resources.compute[0]
    assert allocated.status == ResourceStatus.UNKNOWN and "still allocated" in allocated.error.message
    stopped = orchestrator.stop(response.request_id, who, wait_seconds=45)
    assert stopped.status == RequestStatus.STOPPED and fake.machine(machine.name)["_power"] == "deallocated" and stopped.resources.compute[0].error is None
    del fake.resources[machine.provider_id.lower()]
    assert orchestrator.status(response.request_id, who).resources.compute[0].status == ResourceStatus.DELETED


def test_a_familys_quota_is_checked_before_anything_is_made(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    fake.usages["eastus2"] = [usage("cores", 0, 100), usage("standardDSv5Family", 6, 10)]
    response = provision(orchestrator, {**AZURE_VM_REQUEST, "compute": {"count": 2, "cpu": 4, "memory_gb": 16}})
    assert response.status == RequestStatus.FAILED and response.error.details["cause"] == "QUOTA_EXCEEDED" and response.error.details["metric"] == "standardDSv5Family"
    assert not [call for call in fake.calls if call[0] == "PUT"], "nothing was made, not even the group"


# ---- members and grants

def test_members_are_looked_up_once_before_anything_is_made_and_kept_with_the_access_list(tmp_path):
    config = make_azure_config(tmp_path, access={"allowed_domains": ["example.com", "partner.example.com"]})
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=config)
    request = {**AZURE_VM_REQUEST, "access": [{"member": "user:Ada@Example.com"}, {"member": "group:data-team@example.com", "level": "admin"},
                                              {"member": "user:linus@partner.example.com"}]}
    response = provision(orchestrator, request)
    assert response.status == RequestStatus.READY
    graph = [index for index, call in enumerate(fake.calls) if call[1].startswith("https://graph.microsoft.com/")]
    first_put = next(index for index, call in enumerate(fake.calls) if call[0] == "PUT")
    assert len(graph) == 4 and max(graph) < first_put, "ada, the group, linus by name (not found: a guest), then linus by mail; all before any create"
    assert {entry.member: entry.principal for entry in response.access} == {
        "user:ada@example.com": {"id": USERS["ada@example.com"], "type": "User"}, "group:data-team@example.com": {"id": GROUPS["data-team@example.com"], "type": "Group"},
        "user:linus@partner.example.com": {"id": GUESTS["linus@partner.example.com"], "type": "User"}}
    assert all(entry.status == "GRANTED" for entry in response.access) and all(audience == "https://graph.microsoft.com/" for url, audience in fake.audiences)
    fake.calls.clear()
    orchestrator.manage_access(response.request_id, "grant", "user:ada@example.com", principal(config), level="admin")
    assert not [call for call in fake.calls if call[1].startswith("https://graph.microsoft.com/")], "a kept id is never looked up again"


def test_a_member_the_directory_does_not_know_creates_nothing(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    response = provision(orchestrator, {**AZURE_VM_REQUEST, "access": [{"member": "user:nobody@example.com"}]})
    assert response.status == RequestStatus.FAILED and response.error.details["cause"] == "INVALID_ACCESS"
    assert "Azure's directory has no such user" in response.error.message and not [call for call in fake.calls if call[0] == "PUT"]


def test_a_directory_the_service_may_not_read_says_which_permissions_and_that_object_ids_need_none(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    fake.graph_denied = True
    response = provision(orchestrator, {**AZURE_VM_REQUEST, "access": [{"member": "user:ada@example.com"}]})
    assert response.status == RequestStatus.FAILED and response.error.details["cause"] == "PROVIDER_PERMISSION_DENIED"
    assert "User.Read.All and Group.Read.All" in response.error.message and "user:<object id>" in response.error.message


def test_object_ids_are_members_on_azure_only_and_only_as_a_principals_identity(tmp_path):
    principals = [{**item, "identity": f"serviceAccount:{SERVICE_PRINCIPAL}"} if item["name"] == "app-layer" else item for item in config_dict()["principals"]]
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, principals=principals))
    with pytest.raises(ValueError) as caught:
        InfrastructureRequest.model_validate({"provider": "gcp", "region": "us-central1", "compute": {"cpu": 2, "memory_gb": 8}, "access": [{"member": f"user:{USERS['ada@example.com']}"}]})
    assert "INVALID_ACCESS" in str(caught.value) and "which the gcp adapter does not take; write an email address" in str(caught.value)
    response = provision(orchestrator, AZURE_VM_REQUEST)
    [requester] = response.access
    assert requester.member == f"serviceAccount:{SERVICE_PRINCIPAL}" and requester.principal == {"id": SERVICE_PRINCIPAL, "type": "ServicePrincipal"}
    assert requester.status == "GRANTED" and not [call for call in fake.calls if call[1].startswith("https://graph.microsoft.com/")], "an object id needs no directory read"
    stranger = f"user:{USERS['grace@example.com']}"
    with pytest.raises(InfrastructureError) as caught:
        provision(orchestrator, {**AZURE_VM_REQUEST, "access": [{"member": stranger}]})
    assert caught.value.code == ErrorCode.INVALID_ACCESS and "outside the domains" in caught.value.message
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.manage_access(response.request_id, "grant", stranger, principal(orchestrator.config))
    assert caught.value.code == ErrorCode.INVALID_ACCESS and "outside the domains" in caught.value.message, "no domain admits an object id"
    assert validation.check_member(f"group:{GROUPS['data-team@example.com'].upper()}", provider="azure") == f"group:{GROUPS['data-team@example.com']}"
    with pytest.raises(InfrastructureError):
        validation.check_member(f"group:{GROUPS['data-team@example.com']}", provider="gcp")


def test_a_service_principal_by_address_is_refused_on_azure(tmp_path):
    orchestrator, _ = make_azure_orchestrator(tmp_path)
    response = provision(orchestrator, {**AZURE_VM_REQUEST, "access": [{"member": "serviceAccount:builder@example.com"}]})
    assert response.status == RequestStatus.FAILED and "name it serviceAccount:<object id>" in response.error.message


def test_grants_are_role_assignments_on_each_resource_named_from_scope_principal_and_role(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "shared", "network": {}, "compute": {"cpu": 2, "memory_gb": 8},
                                        "access": [{"member": "user:ada@example.com"}]})
    assert response.status == RequestStatus.READY
    ada = USERS["ada@example.com"]
    machine = response.resources.compute[0]
    interface = fake.machine(machine.name)["properties"]["networkProfile"]["networkInterfaces"][0]["id"].rsplit("/", 1)[-1]
    assert interface != "shared-vm-1-nic", "Azure names the interface; the adapter reads it from the machine"
    assert fake.assignments() == [(f"networkInterfaces/{interface}", ada, READER), ("virtualMachines/shared-vm-1", ada, VM_USER_LOGIN)]
    assert [grant.role for grant in machine.access] == ["Virtual Machine User Login", "Reader"]
    [assignment] = [item for item in fake.resources.values() if item["type"] == "Microsoft.Authorization/roleAssignments" and item["properties"]["scope"] == machine.provider_id]
    assert assignment["name"] == assignment_name(machine.provider_id, ada, VM_USER_LOGIN) and assignment["properties"]["principalType"] == "User"
    assert assignment["properties"]["roleDefinitionId"] == f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{VM_USER_LOGIN}"
    assert {role for _, _, role in fake.assignments()} <= access.GRANTED_ROLES and assignment_name("/A", ada, READER) == assignment_name("/a", ada.upper(), READER.upper())
    changed = orchestrator.manage_access(response.request_id, "grant", "user:ada@example.com", who, level="admin")
    assert [entry.status for entry in changed.access] == ["GRANTED"]
    assert fake.assignments() == [(f"networkInterfaces/{interface}", ada, READER), ("virtualMachines/shared-vm-1", ada, VM_ADMIN_LOGIN)], "the use role went first"
    revoked = orchestrator.manage_access(response.request_id, "revoke", "user:ada@example.com", who)
    assert revoked.access[0].status == "REVOKED" and fake.assignments() == []


def test_the_same_grant_made_before_under_another_name_is_taken_as_there(tmp_path):
    provider, fake = make_azure_provider(make_azure_config())
    principal_id = {"id": USERS["ada@example.com"], "type": "User"}
    fake.refuse[("PUT", "/roleAssignments/")] = [refusal(409, "RoleAssignmentExists", "The role assignment already exists.")]
    provider.access.assign(f"/subscriptions/{SUBSCRIPTION}/resourceGroups/x", principal_id, READER, "test")
    assert fake.refuse[("PUT", "/roleAssignments/")] == []


def test_an_identity_made_moments_ago_is_granted_once_azure_has_it(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    fake.refuse[("PUT", "/roleAssignments/")] = [refusal(400, "PrincipalNotFound", "Principal does not exist in the directory"),
                                                 refusal(400, "PrincipalNotFound", "Principal does not exist in the directory")]
    response = provision(orchestrator, {**AZURE_VM_REQUEST, "identity": {}, "compute": {"cpu": 2, "memory_gb": 8, "identity": "app"}})
    [identity] = response.resources.identity
    assert response.status == RequestStatus.READY and identity.principal_id and identity.client_id and identity.email == ""
    [member] = response.access
    assert member.member == f"serviceAccount:{identity.principal_id}" and member.status == "GRANTED" and member.principal["type"] == "ServicePrincipal"
    [body] = puts(fake, "virtualMachines")
    assert body["identity"] == {"type": "SystemAssigned, UserAssigned", "userAssignedIdentities": {identity.provider_id: {}}}
    assert fake.now >= 20, "two waits of ten seconds for the directory to know it"


# ---- buckets and disks

def test_a_bucket_takes_entra_sign_in_only_and_is_deleted_only_when_empty(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "data-team", "storage": {"type": "object", "size_gb": 50},
                                        "access": [{"member": "user:ada@example.com", "level": "admin"}]})
    [bucket] = response.resources.storage
    account = f"datateam{response.request_id[:8]}"
    assert bucket.name == account and bucket.url == f"https://{account}.blob.core.windows.net/data" and response.endpoints.bucket_urls == [bucket.url]
    [body] = puts(fake, "storageAccounts")
    assert body["sku"] == {"name": "Standard_ZRS"} and body["kind"] == "StorageV2"
    assert {key: body["properties"][key] for key in ("allowBlobPublicAccess", "allowSharedKeyAccess", "minimumTlsVersion", "supportsHttpsTrafficOnly")} == \
        {"allowBlobPublicAccess": False, "allowSharedKeyAccess": False, "minimumTlsVersion": "TLS1_2", "supportsHttpsTrafficOnly": True}
    [service] = puts(fake, "blobServices")
    assert service["properties"]["deleteRetentionPolicy"] == {"enabled": True, "days": 7} and puts(fake, "containers") == [{"properties": {"publicAccess": "None"}}]
    assert fake.assignments() == [("containers/data", USERS["ada@example.com"], BLOB_OWNER)] and [grant.role for grant in bucket.access] == ["Storage Blob Data Owner"]
    fake.blobs[account] = ["results.parquet"]
    failed = orchestrator.destroy(response.request_id, who, wait_seconds=45)
    assert failed.status == RequestStatus.DESTROY_FAILED and failed.error.details["remaining"][0]["error"]["code"] == "PROVIDER_CONFLICT"
    assert ("https://" + f"{account}.blob.core.windows.net/data", "https://storage.azure.com/") in fake.audiences and fake.of_type("Microsoft.Storage/storageAccounts")
    assert fake.assignments() == [("containers/data", USERS["ada@example.com"], BLOB_OWNER)], "a bucket kept keeps its members' access"
    fake.blobs[account] = []
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED
    assert fake.resources == {} and fake.assignments() == [], "the grants went with it"
    assert BLOB_CONTRIBUTOR in access.GRANTED_ROLES


def test_a_bucket_the_service_may_not_list_is_not_deleted(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    response = provision(orchestrator, {"provider": "azure", "region": "eastus2", "storage": {"type": "object"}})
    fake.refuse[("GET", ".blob.core.windows.net/")] = [refusal(403, "AuthorizationPermissionMismatch", "This request is not authorized")]
    failed = orchestrator.destroy(response.request_id, principal(orchestrator.config), wait_seconds=45)
    assert failed.status == RequestStatus.DESTROY_FAILED and "Storage Blob Data Reader" in failed.error.details["remaining"][0]["error"]["message"]


def test_a_disk_attaches_in_its_zone_at_the_next_lun_and_outlives_its_machine(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    machines = provision(orchestrator, {**AZURE_VM_REQUEST, "storage": {"size_gb": 10, "performance": "standard"}})
    machine = machines.resources.compute[0]
    later = orchestrator.create_storage(who, region="eastus2", size_gb=20, provider="azure", wait_seconds=45)
    attached = orchestrator.attach_storage(later.resources.storage[0].id, machine.id, who)
    disks = fake.machine(machine.name)["properties"]["storageProfile"]["dataDisks"]
    assert attached.resource.attached_to == machine.id and [disk["lun"] for disk in disks] == [0, 1] and disks[0]["managedDisk"]["id"] == machines.resources.storage[0].provider_id
    assert puts(fake, "disks")[0]["sku"] == {"name": "Standard_LRS"}
    elsewhere = orchestrator.create_storage(who, region="eastus2", zone="2", size_gb=20, provider="azure", wait_seconds=45)
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.attach_storage(elsewhere.resources.storage[0].id, machine.id, who)
    assert caught.value.code == ErrorCode.INVALID_STORAGE_CONFIGURATION and "attaches within its zone" in caught.value.message
    adapter = orchestrator.registry.get("azure")
    with pytest.raises(InfrastructureError) as caught:
        adapter.delete_storage(attached.resource, orchestrator._context(orchestrator.state.get_request(later.request_id)))
    assert caught.value.code == ErrorCode.PROVIDER_CONFLICT and "attached" in caught.value.message, "Azure refuses to delete an attached disk"
    gone = orchestrator.delete_resource(machine.id, who, ResourceKind.compute, wait_seconds=45)
    assert gone.status == "DELETED" and fake.of_type("Microsoft.Compute/disks").keys() >= {machines.resources.storage[0].name, attached.resource.name}
    survivor = orchestrator.get_resource(later.resources.storage[0].id, who).resource
    assert survivor.status == ResourceStatus.READY, "a disk attached later survives its machine"


# ---- power, resize and the sweeper

def test_stop_deallocates_and_start_starts_again(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, AZURE_VM_REQUEST)
    machine = response.resources.compute[0]
    first = machine.started_at
    stopped = orchestrator.stop(response.request_id, who, wait_seconds=45)
    assert stopped.status == RequestStatus.STOPPED and fake.machine(machine.name)["_power"] == "deallocated"
    assert [path.rsplit("/", 1)[-1] for method, path in fake.order if method == "POST"] == ["deallocate"], "never powerOff: a stopped machine still bills"
    cost = orchestrator.cost(who, request_id=response.request_id)
    line = next(item for item in cost["estimate"]["lines"] if item["kind"] == "compute")
    assert line["stopped_usd"] == 2.4 and cost["current_monthly_usd"] < cost["estimate"]["monthly_usd"], "deallocated: its OS disk bills, not the machine"
    started = orchestrator.start(response.request_id, who, wait_seconds=45)
    assert started.status == RequestStatus.READY and started.resources.compute[0].started_at != first and started.resources.compute[0].stop_reason is None


def test_a_resize_the_hardware_offers_is_one_patch_and_any_other_deallocates_first(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, AZURE_VM_REQUEST)
    machine = response.resources.compute[0]
    fake.order.clear()
    resized = orchestrator.resize_compute(machine.id, 4, 16, who, wait_seconds=45)
    assert resized.resource.machine_type == "Standard_D4s_v5" and [method for method, _ in fake.order] == ["PATCH"]
    fake.order.clear()
    moved = orchestrator.resize_compute(machine.id, 4, 32, who, wait_seconds=45)
    assert moved.resource.machine_type == "Standard_E4s_v5" and [(method, path.rsplit("/", 1)[-1]) for method, path in fake.order] == \
        [("POST", "deallocate"), ("PATCH", machine.name), ("POST", "start")]
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.resize_compute(machine.id, 3, 12, who)
    assert caught.value.code == ErrorCode.INVALID_COMPUTE_CONFIGURATION and fake.machine(machine.name)["properties"]["hardwareProfile"]["vmSize"] == "Standard_E4s_v5"
    orchestrator.stop(response.request_id, who, wait_seconds=45)
    fake.order.clear()
    still = orchestrator.resize_compute(machine.id, 2, 16, who, wait_seconds=45)
    assert still.resource.machine_type == "Standard_E2s_v5" and still.resource.status == ResourceStatus.STOPPED and [method for method, _ in fake.order] == ["PATCH"]


def test_a_gpu_machine_keeps_its_gpus_when_resized(tmp_path):
    provider, _ = make_azure_provider(make_azure_config())
    from cloudinfra.models.resources import ComputeResource
    machine = ComputeResource(id="cmp-000000000001", name="gpu-1", request_id="r", provider="azure", region="eastus2", machine_type="Standard_NC4as_T4_v3", cpu=4, memory_gb=28,
                              os_image="x", boot_disk_gb=50, accelerator="t4 x1", provider_id=f"/subscriptions/{SUBSCRIPTION}/resourceGroups/g/providers/Microsoft.Compute/virtualMachines/gpu-1")
    assert provider.resize_preview(machine, 16, 64).machine_type == "Standard_NC16as_T4_v3"
    assert provider.resize_problem(machine, 2, 8) is None and provider.resize_preview(machine, 2, 8).machine_type == "Standard_NC4as_T4_v3", "the smallest T4 size covering it"
    assert provider.resize_problem(machine.model_copy(update={"machine_type": "Standard_NV6", "accelerator": "m60 x1"}), 8, 32).field == "accelerator"


def test_the_sweeper_stops_a_machine_at_its_max_run_hours_on_azure_keep_awake_or_not(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, {**AZURE_VM_REQUEST, "compute": {"cpu": 2, "memory_gb": 8, "max_run_hours": 2}})
    machine = response.resources.compute[0]
    assert not orchestrator.registry.get("azure").enforces_run_limit and machine.max_run_hours == 2
    started = datetime.datetime.fromisoformat(machine.started_at.replace("Z", "+00:00"))
    orchestrator.wallclock = lambda: started + datetime.timedelta(hours=1)
    orchestrator.keep_awake(response.request_id, who, 24)
    assert Sweeper(orchestrator, 0).tick()["run_limit_stopping"] == []
    orchestrator.wallclock = lambda: started + datetime.timedelta(hours=2, minutes=1)
    found = Sweeper(orchestrator, 0).tick()
    assert found["run_limit_stopping"] == [machine.id] and found["idle_stopping"] == [] and fake.machine(machine.name)["_power"] == "deallocated"
    assert orchestrator.get_resource(machine.id, who).resource.stop_reason == "max_run"


def test_idle_stop_reads_azure_monitors_busiest_minute(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    response = provision(orchestrator, {**AZURE_VM_REQUEST, "compute": {"cpu": 2, "memory_gb": 8, "autostop": {"idle_minutes": 30}}})
    machine = response.resources.compute[0]
    started = datetime.datetime.fromisoformat(machine.started_at.replace("Z", "+00:00"))
    orchestrator.wallclock = lambda: started + datetime.timedelta(hours=1)
    fake.cpu_percent[machine.provider_id.lower()] = 50.0  # one of two vCPUs busy: a core
    assert Sweeper(orchestrator, 0).tick()["idle_stopping"] == []
    fake.cpu_percent[machine.provider_id.lower()] = 10.0  # a fifth of a core, under the half-core threshold
    assert Sweeper(orchestrator, 0).tick()["idle_stopping"] == [machine.id] and fake.machine(machine.name)["_power"] == "deallocated"
    metrics = next(call for call in fake.calls if "Microsoft.Insights/metrics" in call[1])
    assert metrics[3]["aggregation"] == "Maximum" and "/" in metrics[3]["timespan"]


def test_usage_and_health_of_a_machine(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, AZURE_VM_REQUEST)
    machine = response.resources.compute[0]
    fake.cpu_percent[machine.provider_id.lower()] = 40.0
    usage_now = orchestrator.usage(machine.id, 60, who)
    assert usage_now.metrics == {"cpu_utilization_mean": 0.3, "cpu_utilization_max": 0.4} and "Azure Monitor agent" in usage_now.note
    health = orchestrator.health(machine.id, who)
    assert health.healthy is True and health.detail["internal_ip"] == machine.internal_ip
    assert orchestrator.usage(response.resources.network[0].id, 60, who).metrics == {}


# ---- primitives, connect blocks and the listing

def test_primitives_join_a_network_across_requests_and_each_requests_group_goes_with_its_last_resource(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    network = orchestrator.create_network(who, region="eastus2", provider="azure", name="shared", wait_seconds=45)
    network_id = network.resources.network[0].id
    machines = orchestrator.create_compute(who, region="eastus2", provider="azure", cpu=2, memory_gb=8, network_id=network_id, wait_seconds=45)
    machine = machines.resources.compute[0]
    interface = puts(fake, "virtualMachines")[0]["properties"]["networkProfile"]["networkInterfaceConfigurations"][0]
    subnet = interface["properties"]["ipConfigurations"][0]["properties"]["subnet"]["id"]
    assert subnet == network.resources.network[0].subnet and f"/resourceGroups/shared-{network.request_id[:8]}/" in subnet
    assert f"/resourceGroups/infra-{machines.request_id[:8]}-{machines.request_id[:8]}/" in machine.provider_id, "the machine is in its own request's group"
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.destroy(network.request_id, who)
    assert caught.value.code == ErrorCode.INVALID_STATE and "other requests" in caught.value.message
    gone = orchestrator.delete_resource(machine.id, who, ResourceKind.compute, wait_seconds=45)
    assert gone.status == "DELETED" and orchestrator.status(machines.request_id, who).status == RequestStatus.DESTROYED
    assert not [key for key in fake.resources if f"infra-{machines.request_id[:8]}" in key], "its group went with its last resource"
    assert orchestrator.destroy(network.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED and fake.resources == {}


def test_connect_blocks_name_the_bastion_the_bucket_and_the_identity(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, azure=BASTION_SETTINGS))
    who = principal(orchestrator.config)
    response = provision(orchestrator, {**AZURE_VM_REQUEST, "identity": {}, "compute": {"cpu": 2, "memory_gb": 8, "identity": "app"}})
    connect = response.resources.compute[0].connect
    assert connect.commands["ssh"] == (f"az network bastion ssh --name hub-bastion --resource-group hub --subscription {SUBSCRIPTION} "
                                       f"--target-resource-id {response.resources.compute[0].provider_id} --auth-type AAD")
    assert "--resource-port PORT --port PORT" in connect.commands["port_forward"] and "Reader on the Bastion, which the platform team grants once" in connect.prerequisites
    identity = response.resources.identity[0].connect
    assert f"client_id={response.resources.identity[0].client_id}" in identity.commands["token"]
    (tmp_path / "plain").mkdir()
    plain, _ = make_azure_orchestrator(tmp_path / "plain")
    alone = provision(plain, AZURE_VM_REQUEST)
    machine = alone.resources.compute[0]
    assert machine.connect.commands == {"ssh": f"az ssh vm --ip {machine.internal_ip} --subscription {SUBSCRIPTION}"}
    assert "a path into the machine's network" in machine.connect.prerequisites[0]
    bucket = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "files", "storage": {"type": "object"}}).resources.storage[0].connect
    assert bucket.commands["list"].startswith("az storage blob list --account-name files") and "--auth-mode login" in bucket.commands["copy"]
    assert orchestrator.status(response.request_id, who).resources.network[0].connect is None


def test_the_listing_finds_machines_by_their_tags_with_their_addresses(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, {**AZURE_VM_REQUEST, "compute": {"cpu": 2, "memory_gb": 8, "public_ip": True}, "network": {"public_access": True}})
    listed = orchestrator.list_compute(who)
    [machine] = listed["instances"]
    assert listed["count"] == 1 and machine["provider"] == "azure" and machine["request_id"] == response.request_id
    assert (machine["internal_ip"], machine["public_ip"], machine["machine_type"], machine["zone"]) == ("10.10.0.4", response.endpoints.public_ips[0], "Standard_D2s_v5", "1")
    assert orchestrator.list_compute(principal(orchestrator.config, "reader"))["count"] == 0


# ---- prices

def test_estimates_from_the_built_in_table_and_from_a_dated_snapshot(tmp_path):
    provider, _ = make_azure_provider(make_azure_config())
    request = InfrastructureRequest.model_validate({**AZURE_VM_REQUEST, "storage": {"size_gb": 100, "performance": "balanced"}})
    ctx = planner.context(request, "0123456789abcdef0123456789abcdef", "app-layer", make_azure_config(), provider)
    estimate = provider.estimate_cost(provider.plan(request, ctx))
    lines = {line.kind: line for line in estimate.lines}
    assert lines["compute"].monthly_usd == round(0.096 * 730 + 2.4, 2) and lines["storage"].monthly_usd == 9.6 and lines["network"].monthly_usd == round(0.05 * 730, 2)
    assert "sold as the E tier of 128 GiB" in lines["storage"].basis and estimate.verified is None and "built-in" in estimate.source
    snapshot = {"verified": "2026-11-04", "regions": {"eastus2": {"vm_hour": {"Standard_D2s_v5": 0.1}, "disk_month": {"StandardSSD_LRS": {"128": 10.0}}}}}
    (tmp_path / "prices.json").write_text(json.dumps(snapshot))
    dated, _ = make_azure_provider(make_azure_config(azure={"prices_file": str(tmp_path / "prices.json")}))
    again = dated.estimate_cost(dated.plan(request, ctx))
    lines = {line.kind: line for line in again.lines}
    assert lines["compute"].monthly_usd == round(0.1 * 730 + 2.4, 2) and lines["storage"].monthly_usd == 10.0 and again.verified == "2026-11-04"
    assert again.source.startswith("https://prices.azure.com/api/retail/prices")
    assert pricing.tier(129, "Premium_LRS") == 256 and pricing.tier(1, "Standard_LRS") == 32, "standard HDD tiers start at 32 GiB"
    lost, _ = make_azure_provider(make_azure_config(azure={"prices_file": str(tmp_path / "gone.json")}))
    fallback = lost.estimate_cost(lost.plan(request, ctx))
    assert fallback.monthly_usd == estimate.monthly_usd and "gone.json could not be read" in fallback.note


def test_the_snapshot_reads_the_retail_prices_api_with_no_credential(tmp_path):
    seen = []

    def answer(request):
        seen.append(request)
        assert "authorization" not in {name.lower() for name in request.headers}
        query = request.url.params["$filter"]
        if "Virtual Machines" in query:
            items = [{"armSkuName": "Standard_D2s_v5", "retailPrice": 0.096, "type": "Consumption", "unitOfMeasure": "1 Hour", "productName": "Virtual Machines Dsv5 Series",
                      "skuName": "D2s v5", "meterName": "D2s v5"},
                     {"armSkuName": "Standard_D2s_v5", "retailPrice": 0.188, "type": "Consumption", "unitOfMeasure": "1 Hour", "productName": "Virtual Machines Dsv5 Series Windows",
                      "skuName": "D2s v5", "meterName": "D2s v5"},
                     {"armSkuName": "Standard_D2s_v5", "retailPrice": 0.02, "type": "Consumption", "unitOfMeasure": "1 Hour", "productName": "Virtual Machines Dsv5 Series",
                      "skuName": "D2s v5 Spot", "meterName": "D2s v5 Spot"}] if "Standard_D2s_v5" in query else []
            return httpx.Response(200, json={"Items": items, "NextPageLink": None})
        if "Managed Disks" in query:
            if request.url.params.get("page") != "2":
                return httpx.Response(200, json={"Items": [{"productName": "Premium SSD Managed Disks", "meterName": "P10 LRS Disk", "unitOfMeasure": "1/Month", "retailPrice": 20.0}],
                                                 "NextPageLink": "https://prices.azure.com/api/retail/prices?page=2&$filter=" + query.replace(" ", "%20")})
            return httpx.Response(200, json={"Items": [{"productName": "Standard SSD Managed Disks", "meterName": "E10 LRS Disk Operations", "unitOfMeasure": "10K", "retailPrice": 1}]})
        if "Block Blob" in query:
            return httpx.Response(200, json={"Items": [{"meterName": "Hot LRS Data Stored", "tierMinimumUnits": 0, "retailPrice": 0.02},
                                                       {"meterName": "Hot LRS Data Stored", "tierMinimumUnits": 51200, "retailPrice": 0.019}]})
        return httpx.Response(200, json={"Items": [{"meterName": "Standard IPv4 Static Public IP", "unitOfMeasure": "1 Hour", "retailPrice": 0.005, "productName": "IP Addresses"},
                                                   {"meterName": "Standard Gateway", "unitOfMeasure": "1 Hour", "retailPrice": 0.045, "productName": "NAT Gateway"}]})
    client = httpx.Client(transport=httpx.MockTransport(answer))
    snapshot = pricing.snapshot(["eastus2"], client, datetime.date(2026, 11, 4))
    figures = snapshot["regions"]["eastus2"]
    assert snapshot["verified"] == "2026-11-04" and figures["vm_hour"] == {"Standard_D2s_v5": 0.096}, "Linux pay-as-you-go: not Windows, not Spot"
    assert figures["disk_month"]["Premium_LRS"] == {128: 20.0} and figures["blob_gb_month"] == {"Standard_LRS": 0.02} and (figures["public_ip_hour"], figures["nat_hour"]) == (0.005, 0.045)
    assert "Standard_E4s_v5" in snapshot["missing"]["eastus2"] and all(str(request.url).startswith(pricing.RETAIL) for request in seen)


def test_the_operator_writes_the_snapshot_from_the_command_line(tmp_path, monkeypatch, capsys):
    config = tmp_path / "cloudinfra.yaml"
    config.write_text(json.dumps(make_azure_config().model_dump(mode="json", exclude_none=True)))
    monkeypatch.setattr(pricing, "snapshot", lambda regions: {"source": "x", "verified": "2026-11-04", "currency": "USD", "regions": {region: {} for region in regions},
                                                              "missing": {"eastus2": ["Standard_B1s"]}})
    assert cli.main(["prices", "--config", str(config), "--write", str(tmp_path / "prices.json")]) == 0
    assert json.loads((tmp_path / "prices.json").read_text())["regions"] == {"eastus2": {}, "westus": {}} and "1 figures not found" in capsys.readouterr().err
    monkeypatch.delenv("CLOUDINFRA_CONFIG", raising=False)
    assert cli.main(["prices", "--write", str(tmp_path / "x.json")]) == 1


def test_an_azure_request_is_planned_offline_without_any_call(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("CLOUDINFRA_CONFIG", raising=False)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({**AZURE_VM_REQUEST, "storage": {"type": "object"}}))
    assert cli.main(["plan", str(request)]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert [(item["kind"], item["spec"].get("machine_type") or item["spec"].get("redundancy")) for item in planned["planned"]] == \
        [("network", None), ("storage", "Standard_ZRS"), ("compute", "Standard_D2s_v5")] and planned["estimate"]["monthly_usd"] > 0


# ---- what the review of 2026-11-04 found

def test_a_machine_that_reaches_no_package_host_is_made_without_the_sign_in_extension_and_says_so(tmp_path):
    """The extension installs from packages.microsoft.com: on a network with no NAT, or egress denied, it would wait and fail."""
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, azure={"deny_egress": True}))
    denied = provision(orchestrator, {**AZURE_VM_REQUEST, "name": "sealed"})
    assert denied.status == RequestStatus.READY and puts(fake, "extensions") == []
    assert denied.resources.compute[0].connect.commands == {} and "Entra sign-in is not installed" in denied.resources.compute[0].connect.notes[0]
    (tmp_path / "nat").mkdir()
    plain, fake = make_azure_orchestrator(tmp_path / "nat")
    no_nat = provision(plain, {**AZURE_VM_REQUEST, "network": {"nat": False}})
    assert no_nat.status == RequestStatus.READY and puts(fake, "extensions") == []
    own_address = provision(plain, {**AZURE_VM_REQUEST, "network": {"nat": False, "public_access": True}, "compute": {"cpu": 2, "memory_gb": 8, "public_ip": True}})
    assert len(puts(fake, "extensions")) == 1 and "ssh" in own_address.resources.compute[0].connect.commands, "its own address reaches the package host"


def test_two_requests_of_one_name_keep_a_flow_log_each_in_the_shared_watcher_group(tmp_path):
    account = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/logs/providers/Microsoft.Storage/storageAccounts/flowlogs01"
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, azure={"flow_logs_storage_account": account}))
    watcher_group = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/NetworkWatcherRG"
    fake.resources[watcher_group.lower()] = {"id": watcher_group, "name": "NetworkWatcherRG", "type": "Microsoft.Resources/resourceGroups", "properties": {}, "_top": False}
    first = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "same", "network": {}})
    second = provision(orchestrator, {"provider": "azure", "region": "eastus2", "name": "same", "network": {}})
    assert len(fake.of_type("Microsoft.Network/networkWatchers/flowLogs")) == 2
    orchestrator.destroy(second.request_id, principal(orchestrator.config), wait_seconds=45)
    [left] = fake.of_type("Microsoft.Network/networkWatchers/flowLogs").values()
    assert left["id"] == first.resources.network[0].flow_log and left["properties"]["targetResourceId"] == first.resources.network[0].provider_id


def test_a_requesters_identity_azure_cannot_name_is_recorded_as_failed_and_the_request_goes_on(tmp_path):
    """In a deployment with both clouds, a principal's identity may be a Google service account: on Azure it has no object id."""
    principals = [{**item, "identity": "serviceAccount:app-layer@test-project.iam.gserviceaccount.com"} if item["name"] == "app-layer" else item
                  for item in config_dict()["principals"]]
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, principals=principals))
    response = provision(orchestrator, {**AZURE_VM_REQUEST, "access": [{"member": "user:ada@example.com"}]})
    assert response.status == RequestStatus.READY
    entries = {entry.member: entry for entry in response.access}
    requester = entries["serviceAccount:app-layer@test-project.iam.gserviceaccount.com"]
    assert requester.status == "FAILED" and requester.principal is None and "has no address" in requester.error.message
    assert entries["user:ada@example.com"].status == "GRANTED"


def test_a_member_that_was_never_found_can_still_be_revoked(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, AZURE_VM_REQUEST)
    granted = orchestrator.manage_access(response.request_id, "grant", "user:nobody@example.com", who)
    assert [entry.status for entry in granted.access] == ["FAILED"] and "no such user" in granted.access[0].error.message
    revoked = orchestrator.manage_access(response.request_id, "revoke", "user:nobody@example.com", who)
    assert [entry.status for entry in revoked.access] == ["REVOKED"], "nothing named it, so nothing was left to remove"


def test_a_part_a_crash_left_behind_goes_with_the_group_and_a_machines_parts_are_read_not_guessed(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    response = provision(orchestrator, {**AZURE_VM_REQUEST, "name": "crash", "compute": {"cpu": 2, "memory_gb": 8, "public_ip": True}, "network": {"public_access": True}})
    machine = fake.machine("crash-vm-1")
    parts = [item["name"] for item in fake.resources.values() if item.get("_delete_with") == machine["id"].lower()]
    assert sorted(parts) != sorted(["crash-vm-1-nic", "crash-vm-1-ip", "crash-vm-1-osdisk"]) and len(parts) == 3, "Azure names them"
    assert response.resources.compute[0].public_ip and response.resources.compute[0].internal_ip == "10.10.0.4", "the addresses come from the parts Azure named"
    group = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/crash-{response.request_id[:8]}"
    leftover = f"{group}/providers/Microsoft.Network/networkInterfaces/crash-vm-9-nic"
    fake.resources[leftover.lower()] = {"id": leftover, "name": "crash-vm-9-nic", "type": "Microsoft.Network/networkInterfaces", "properties": {}, "_top": True}
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED and fake.resources == {}, \
        "an untagged part named after the request is the request's"
