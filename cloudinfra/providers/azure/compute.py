"""Virtual machines behind the generic compute primitives (docs/research/RESEARCH-2026-11-03-azure.md, section 5.2).

What a machine is made with, and why:
- One PUT makes the machine with its network interface and, when asked, its public address (Standard, static), each
  with deleteOption Delete, and its OS disk with deleteOption Delete: a machine's delete takes its parts with it
  (learn.microsoft.com/azure/virtual-machines/delete), and the adapter deletes each part by name afterwards as well, for
  one a crash left behind.
- Trusted Launch with vTPM, and Secure Boot as the planned software allows (off for an image without GPU drivers on a GPU
  machine, as on Google); a size without Trusted Launch boots as a Standard machine.
- Sign-in is Entra's: a system-assigned identity and the AADSSHLoginForLinux extension, so members log in with their
  own accounts through the login roles (access.py). The extension installs a package from packages.microsoft.com, so it
  is installed only on a machine that reaches the internet (network.reaches_internet); on any other the connect block
  says how else to reach it, rather than a create that waits and fails. Azure requires an administrator key; the adapter
  makes one and keeps no private half, so nobody holds it. No password, no custom data, no user data.
- A machine's interface, public address and OS disk are read from the machine itself (its networkProfile and
  storageProfile), never guessed from the names they were asked for.
- The request's identity a group names is attached as a user-assigned identity, for the machine's workloads.
- stop is deallocate: a machine stopped any other way is still allocated and billed in full
  (learn.microsoft.com/azure/virtual-machines/states-billing). Azure has no run-time limit, so max_run_hours is the
  sweeper's (CloudProvider.enforces_run_limit).
- A resize to a size the machine's hardware offers (its vmSizes) is one PATCH, which restarts a running machine; any
  other deallocates it first and starts it again after.
"""
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ...models.errors import ErrorCode, ErrorDetail, InfrastructureError
from ...models.request import ComputeRequest
from ...models.resources import ComputeResource, ResourceStatus
from ...observability.logging import event
from ..base import Problem
from . import translate
from .api import API_VERSIONS
from .groups import group_of
from .network import reaches_internet

VERSION = API_VERSIONS["Microsoft.Compute"]
NETWORK_VERSION = API_VERSIONS["Microsoft.Network"]
DISK_VERSION = API_VERSIONS["Microsoft.Compute/disks"]
SKUS_VERSION = "2021-07-01"  # Resource SKUs (learn.microsoft.com/rest/api/compute/resource-skus/list), versioned apart from machines
OFFERS_SECONDS = 3600
# A machine shut down from inside is "stopped" but still allocated, and bills in full: it is UNKNOWN with an error saying so,
# never STOPPED, so that a stop deallocates it and no estimate prices it as stopped.
POWER = {"running": ResourceStatus.READY, "deallocated": ResourceStatus.STOPPED, "stopped": ResourceStatus.UNKNOWN, "starting": ResourceStatus.UPDATING,
         "stopping": ResourceStatus.UPDATING, "deallocating": ResourceStatus.UPDATING}
ALLOCATED = ErrorDetail(code=ErrorCode.INVALID_STATE, message="Shut down from inside but still allocated, so Azure bills it in full; stop it with "
                        "control_infrastructure to deallocate it, or start it")
PROVISIONING = {"creating": ResourceStatus.CREATING, "deleting": ResourceStatus.DELETING, "failed": ResourceStatus.FAILED, "updating": ResourceStatus.UPDATING,
                "migrating": ResourceStatus.UPDATING}
SIGN_IN = {"publisher": "Microsoft.Azure.ActiveDirectory", "type": "AADSSHLoginForLinux", "typeHandlerVersion": "1.0", "autoUpgradeMinorVersion": True}
MAX_LUN = 63


def throwaway_key() -> str:
    """An RSA public key whose private half is never kept: Azure requires one on a Linux machine with password sign-in off."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    return key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


def _power(view: dict | None) -> str:
    for status in (view or {}).get("statuses") or []:
        code = str(status.get("code") or "")
        if code.startswith("PowerState/"):
            return code.split("/", 1)[1].lower()
    return ""


def _configuration(interface: dict | None) -> tuple[str | None, str | None]:
    """An interface's private address and the id of its public address, from its first IP configuration."""
    configurations = ((interface or {}).get("properties") or {}).get("ipConfigurations") or [{}]
    properties = configurations[0].get("properties") or {}
    return properties.get("privateIPAddress"), (properties.get("publicIPAddress") or {}).get("id")


def _provisioned_at(view: dict | None) -> str | None:
    for status in (view or {}).get("statuses") or []:
        if str(status.get("code") or "").lower() == "provisioningstate/succeeded":
            return status.get("time")
    return None


class AzureCompute:
    def __init__(self, api, settings, groups, ceiling):
        self.api, self.settings, self.groups, self.ceiling = api, settings, groups, ceiling
        self._key = None
        self._offers: dict[str, tuple[float, dict[str, set[str]]]] = {}

    def key(self) -> str:
        if self._key is None:
            self._key = throwaway_key()
        return self._key

    def _id(self, group: str, kind: str, name: str, namespace: str = "Microsoft.Compute") -> str:
        return f"{self.groups.id(group)}/providers/{namespace}/{kind}/{name}"

    def _url(self, resource_id: str) -> str:
        return self.api.arm(resource_id)

    def parts(self, resource: ComputeResource, machine: dict | None = None) -> dict[str, list[str]]:
        """The ids of a machine's interfaces and OS disk as the machine names them (read when not given); for a machine
        already gone, the names it was made with. Its public addresses are on its interfaces (addresses())."""
        group = group_of(resource.provider_id)
        found = {"interfaces": [self._id(group, "networkInterfaces", f"{resource.name}-nic", "Microsoft.Network")],
                 "os_disk": [self._id(group, "disks", f"{resource.name}-osdisk")]}
        machine = machine if machine is not None else self.api.find(self._url(resource.provider_id), VERSION)
        properties = (machine or {}).get("properties") or {}
        interfaces = [item["id"] for item in (properties.get("networkProfile") or {}).get("networkInterfaces") or [] if item.get("id")]
        disk = (((properties.get("storageProfile") or {}).get("osDisk") or {}).get("managedDisk") or {}).get("id")
        if interfaces:
            found["interfaces"] = interfaces
        if disk:
            found["os_disk"] = [disk]
        return found

    def offered(self, region: str) -> dict[str, set[str]] | None:
        """The machine sizes Azure offers this subscription in a region (Resource SKUs: {size, lowercase: its zones, or
        {""} where the region has none}), without the zones or the region a restriction takes away; read once an hour per
        region. None when the listing cannot be read. A student subscription is offered none of the v5 sizes in most of
        its regions (the real run of 2026-11-05), which Azure otherwise says only when the machine is made."""
        kept = self._offers.get(region)
        if kept is not None and self.api.clock() - kept[0] < OFFERS_SECONDS:
            return kept[1]
        try:
            items = self.api.list(self.api.subscription("/providers/Microsoft.Compute/skus"), SKUS_VERSION, params={"$filter": f"location eq '{region}'"})
        except InfrastructureError as exc:
            event("size_offers_unread", region=region, code=exc.code.value)
            return None
        found = {}
        for sku in items:
            if sku.get("resourceType") != "virtualMachines" or not sku.get("name"):
                continue
            info = next((item for item in sku.get("locationInfo") or [] if str(item.get("location", "")).lower() == region.lower()), None)
            if info is None:
                continue
            listed = set(info.get("zones") or [])
            zones = set(listed) if listed else {""}
            for restriction in sku.get("restrictions") or []:
                if restriction.get("type") == "Location":
                    zones = set()
                elif restriction.get("type") == "Zone":
                    zones -= set((restriction.get("restrictionInfo") or {}).get("zones") or [])
            if zones:
                found[sku["name"].lower()] = zones
        self._offers[region] = (self.api.clock(), found)
        return found

    def interface(self, resource: ComputeResource) -> str:
        """The machine's primary interface, where the Bastion reads it."""
        return self.parts(resource)["interfaces"][0]

    def create(self, spec, planned, ctx, network) -> ComputeResource:
        shape, name = planned.spec, planned.name
        group = self.groups.ensure(ctx)
        machine_id = self._id(group, "virtualMachines", name)
        subnet = network.subnet if network is not None else self.settings.default_subnet
        if not subnet:
            raise InfrastructureError(ErrorCode.INVALID_NETWORK_CONFIGURATION, "Azure has no default network: the request needs a network block or network_id, "
                                      "or the operator's providers.azure.default_subnet")
        tags = {**translate.tags(ctx), "cloudinfra-group": shape["group"]}
        configuration = {"primary": True, "subnet": {"id": subnet}}
        if shape["public_ip"]:
            configuration["publicIPAddressConfiguration"] = {"name": f"{name}-ip", "sku": {"name": "Standard", "tier": "Regional"},
                                                             "properties": {"deleteOption": "Delete", "publicIPAllocationMethod": "Static"}}
        security = ({"securityType": "TrustedLaunch", "uefiSettings": {"secureBootEnabled": shape["secure_boot"], "vTpmEnabled": True}}
                    if shape["trusted_launch"] else {"securityType": "Standard"})
        identity = {"type": "SystemAssigned"}  # the Entra sign-in extension needs one
        if shape.get("identity"):
            workload = self._id(group, "userAssignedIdentities", f"{ctx.name}-{shape['identity']}", "Microsoft.ManagedIdentity")
            identity = {"type": "SystemAssigned, UserAssigned", "userAssignedIdentities": {workload: {}}}
        body = {
            "location": ctx.region, "tags": tags, "identity": identity,
            "properties": {
                "hardwareProfile": {"vmSize": shape["machine_type"]},
                "storageProfile": {"imageReference": translate.image_reference(shape["image"]),
                                   "osDisk": {"name": f"{name}-osdisk", "createOption": "FromImage", "deleteOption": "Delete", "caching": "ReadWrite",
                                              "diskSizeGB": shape["boot_disk_gb"], "managedDisk": {"storageAccountType": translate.OS_DISK_SKU}}},
                "osProfile": {"computerName": name, "adminUsername": translate.ADMIN_USER, "allowExtensionOperations": True,
                              "linuxConfiguration": {"disablePasswordAuthentication": True, "provisionVMAgent": True,
                                                     "ssh": {"publicKeys": [{"path": f"/home/{translate.ADMIN_USER}/.ssh/authorized_keys", "keyData": self.key()}]}}},
                "networkProfile": {"networkApiVersion": "2020-11-01", "networkInterfaceConfigurations": [
                    {"name": f"{name}-nic", "properties": {"primary": True, "deleteOption": "Delete",
                                                          "ipConfigurations": [{"name": "primary", "properties": configuration}]}}]},
                "securityProfile": security,
                "diagnosticsProfile": {"bootDiagnostics": {"enabled": True}},  # in managed storage: no account of the operator's
            },
        }
        if shape.get("zone"):
            body["zones"] = [shape["zone"]]
        if shape.get("plan"):
            body["plan"] = translate.plan_of(shape["image"])
        size = translate.size_of(shape["machine_type"])
        resource = ComputeResource(id=shape["resource_id"], name=name, request_id=ctx.request_id, provider="azure", region=ctx.region, zone=shape.get("zone"),
                                   group=shape.get("group", "vm"), machine_type=shape["machine_type"], cpu=shape["cpu"], memory_gb=shape["memory_gb"],
                                   os_image=shape["image"].rsplit("/", 1)[-1], boot_disk_gb=shape["boot_disk_gb"],
                                   accelerator=f"{size.gpu} x{size.gpus}" if size is not None and size.gpu else None, labels=tags, profile=shape.get("profile"),
                                   secure_boot=shape["secure_boot"], max_run_hours=shape.get("max_run_hours"), autostop_idle_minutes=shape.get("autostop_idle_minutes"),
                                   provider_id=machine_id, status=ResourceStatus.CREATING)
        ctx.accepted(resource)  # the machine and its parts are in the request's group: a cleanup finds them all
        self.api.put(self._url(machine_id), body, VERSION, timeout=self.ceiling("compute"))
        if reaches_internet(network, shape["public_ip"]):
            self.api.put(self._url(f"{machine_id}/extensions/{SIGN_IN['type']}"), {"location": ctx.region, "tags": tags, "properties": SIGN_IN}, VERSION,
                         timeout=self.ceiling("extension"))
        else:
            event("sign_in_skipped", provider="azure", resource=resource.id, reason="the machine reaches no package host")
        return self.get(resource)

    # ---- reading
    def get(self, resource: ComputeResource) -> ComputeResource:
        try:
            machine = self.api.get(self._url(resource.provider_id), VERSION, params={"$expand": "instanceView"})
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED, "internal_ip": None, "public_ip": None})
            raise
        interface = self.api.find(self._url(self.parts(resource, machine)["interfaces"][0]), NETWORK_VERSION)
        internal, public_id = _configuration(interface)
        public = None
        if public_id:
            address = self.api.find(self._url(public_id), NETWORK_VERSION)
            public = ((address or {}).get("properties") or {}).get("ipAddress")
        return self._from_machine(machine, resource, internal, public)

    def _from_machine(self, machine: dict, base: ComputeResource, internal: str | None, public: str | None) -> ComputeResource:
        properties = machine.get("properties") or {}
        view = properties.get("instanceView")
        state = str(properties.get("provisioningState") or "").lower()
        status = PROVISIONING.get(state) or POWER.get(_power(view), ResourceStatus.READY if state == "succeeded" and view is None else ResourceStatus.UNKNOWN)
        size_name = (properties.get("hardwareProfile") or {}).get("vmSize") or base.machine_type
        size = translate.size_of(size_name)
        allocated = _power(view) == "stopped" and not PROVISIONING.get(state)
        return base.model_copy(update={
            "status": status, "error": ALLOCATED if allocated else (None if base.error == ALLOCATED else base.error),
            "machine_type": size_name, "cpu": size.cpu if size else base.cpu, "memory_gb": size.memory_gb if size else base.memory_gb,
            "accelerator": f"{size.gpu} x{size.gpus}" if size is not None and size.gpu else (None if size is not None else base.accelerator),
            "internal_ip": internal, "public_ip": public, "provider_id": machine.get("id") or base.provider_id, "labels": machine.get("tags") or base.labels,
            "instance_id": properties.get("vmId") or base.instance_id,
            "created_at": base.created_at or properties.get("timeCreated") or (machine.get("systemData") or {}).get("createdAt"),
            "started_at": base.started_at or (_provisioned_at(view) if status == ResourceStatus.READY else None),
        })

    def list(self, labels: dict[str, str]) -> list[ComputeResource]:
        """Machines carrying every given tag, from the subscription's lists of machines, interfaces and addresses."""
        machines = self.api.list(self.api.subscription("/providers/Microsoft.Compute/virtualMachines"), VERSION)
        wanted = [machine for machine in machines if all((machine.get("tags") or {}).get(key) == value for key, value in labels.items())]
        if not wanted:
            return []
        interfaces = self.api.list(self.api.subscription("/providers/Microsoft.Network/networkInterfaces"), NETWORK_VERSION)
        addresses = {str(item.get("id", "")).lower(): (item.get("properties") or {}).get("ipAddress")
                     for item in self.api.list(self.api.subscription("/providers/Microsoft.Network/publicIPAddresses"), NETWORK_VERSION)}
        by_machine = {}
        for interface in interfaces:
            properties = interface.get("properties") or {}
            owner = str((properties.get("virtualMachine") or {}).get("id", "")).lower()
            configuration = ((properties.get("ipConfigurations") or [{}])[0].get("properties")) or {}
            public = addresses.get(str((configuration.get("publicIPAddress") or {}).get("id", "")).lower())
            by_machine[owner] = (configuration.get("privateIPAddress"), public)
        found = []
        for machine in wanted:
            tags = machine.get("tags") or {}
            size = translate.size_of((machine.get("properties") or {}).get("hardwareProfile", {}).get("vmSize", ""))
            base = ComputeResource(id="cmp-000000000000", name=machine.get("name", ""), request_id=tags.get("cloudinfra-request", "unknown"), provider="azure",
                                   region=machine.get("location", ""), zone=(machine.get("zones") or [None])[0], machine_type="unknown",
                                   cpu=size.cpu if size else 0, memory_gb=size.memory_gb if size else 0, os_image="unknown", boot_disk_gb=0,
                                   group=tags.get("cloudinfra-group", "vm"), provider_id=machine.get("id"))
            internal, public = by_machine.get(str(machine.get("id", "")).lower(), (None, None))
            found.append(self._from_machine(machine, base, internal, public))
        return found

    # ---- power
    def stop(self, resource: ComputeResource, ctx) -> ComputeResource:
        """Deallocate: the one stop that ends the machine's billing; its disks and a static address still bill."""
        self.api.post(self._url(f"{resource.provider_id}/deallocate"), VERSION, timeout=self.ceiling("power"))
        return self.get(resource)

    def start(self, resource: ComputeResource, ctx) -> ComputeResource:
        self.api.post(self._url(f"{resource.provider_id}/start"), VERSION, timeout=self.ceiling("power"))
        return self.get(resource.model_copy(update={"stop_reason": None, "started_at": None}))

    # ---- resize
    def shape(self, resource: ComputeResource, cpu: int, memory_gb: int) -> translate.Size | Problem:
        """The size a new shape gives a machine, or the problem. A machine with GPUs keeps them and their family; a
        Trusted Launch machine cannot move to a size without it, nor the other way."""
        current = translate.size_of(resource.machine_type)
        gpus = translate.accelerator_of(current)
        if resource.accelerator and gpus is None:
            return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, f"{resource.name} carries GPUs this adapter does not know ({resource.accelerator}); it is not resized",
                           "accelerator")
        found = translate.compute_shape(ComputeRequest(cpu=cpu, memory_gb=memory_gb, accelerator=gpus))
        if isinstance(found, Problem):
            return found
        if current is not None and found.trusted_launch != current.trusted_launch:
            return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, f"{found.name} {'has' if found.trusted_launch else 'has no'} Trusted Launch and "
                           f"{current.name} {'has' if current.trusted_launch else 'has none'}; Azure does not move a machine between them", "compute")
        return found

    def resize(self, resource: ComputeResource, cpu: int, memory_gb: int, ctx) -> ComputeResource:
        size = self.shape(resource, cpu, memory_gb)
        if isinstance(size, Problem):
            raise InfrastructureError(size.code, size.message, {"cpu": cpu, "memory_gb": memory_gb, "accelerator": resource.accelerator})
        if size.name.lower() == resource.machine_type.lower():
            return self.get(resource)  # already that size: nothing to change
        url = self._url(resource.provider_id)
        event("provider_resize", provider="azure", resource=resource.id, machine_type=size.name)
        running = self.get(resource).status == ResourceStatus.READY  # a machine not running is resized and stays as it is
        offered = {str(item.get("name", "")).lower() for item in self.api.list(self._url(f"{resource.provider_id}/vmSizes"), VERSION)}
        moving = size.name.lower() not in offered or translate.size_of(resource.machine_type) is None \
            or (translate.size_of(resource.machine_type).gpu or None) != (size.gpu or None)
        if moving and running:
            self.api.post(self._url(f"{resource.provider_id}/deallocate"), VERSION, timeout=self.ceiling("power"))
        self.api.patch(url, {"properties": {"hardwareProfile": {"vmSize": size.name}}}, VERSION, timeout=self.ceiling("resize"))
        if moving and running:
            self.api.post(self._url(f"{resource.provider_id}/start"), VERSION, timeout=self.ceiling("power"))
        return self.get(resource.model_copy(update={"machine_type": size.name, "cpu": size.cpu, "memory_gb": size.memory_gb,
                                                    "started_at": None if running else resource.started_at}))

    # ---- disks
    def attach_disk(self, compute: ComputeResource, disk_id: str, name: str) -> int:
        """Attach a managed disk at the first free LUN, hot; its LUN. A disk already attached keeps its own."""
        url = self._url(compute.provider_id)
        machine = self.api.get(url, VERSION)
        disks = list(((machine.get("properties") or {}).get("storageProfile") or {}).get("dataDisks") or [])
        for disk in disks:
            if str((disk.get("managedDisk") or {}).get("id", "")).lower() == disk_id.lower():
                return int(disk.get("lun", 0))
        used = {int(disk.get("lun", -1)) for disk in disks}
        lun = next((number for number in range(MAX_LUN + 1) if number not in used), None)
        if lun is None:
            raise InfrastructureError(ErrorCode.INVALID_STORAGE_CONFIGURATION, f"{compute.name} has no free LUN for another disk", {"machine": compute.id})
        disks.append({"lun": lun, "name": name, "createOption": "Attach", "managedDisk": {"id": disk_id}, "deleteOption": "Detach", "caching": "None"})
        self.api.patch(url, {"properties": {"storageProfile": {"dataDisks": disks}}}, VERSION, timeout=self.ceiling("attach"))
        return lun

    # ---- delete
    def delete(self, resource: ComputeResource, ctx) -> None:
        """The machine, then each part it was made with, as the machine named them before it went: a part already gone
        with it (deleteOption Delete) passes, and one a crash left behind goes now."""
        parts = self.parts(resource)
        addresses = [self._id(group_of(resource.provider_id), "publicIPAddresses", f"{resource.name}-ip", "Microsoft.Network")]
        for interface in parts["interfaces"]:
            public_id = _configuration(self.api.find(self._url(interface), NETWORK_VERSION))[1]
            if public_id and public_id.lower() not in [item.lower() for item in addresses]:
                addresses.append(public_id)
        self.api.delete(self._url(resource.provider_id), VERSION, timeout=self.ceiling("delete"))
        for interface in parts["interfaces"]:
            self.api.delete(self._url(interface), NETWORK_VERSION, timeout=self.ceiling("delete"))
        for address in addresses:
            self.api.delete(self._url(address), NETWORK_VERSION, timeout=self.ceiling("delete"))
        for disk in parts["os_disk"]:
            self.api.delete(self._url(disk), DISK_VERSION, timeout=self.ceiling("delete"))
