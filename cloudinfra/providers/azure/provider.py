"""AzureProvider: the CloudProvider interface implemented with Microsoft Azure.

Built in steps (docs/research/RESEARCH-2026-11-03-azure.md, section 10). Step 1 gave the adapter its credential
(auth.py), its client (api.py), the clouds, the regions and their zones, and the quota read. Step 2 gives it release
A's kinds: identity, network, storage (disks and buckets) and compute, with grants, stop and start, resize, connect
blocks, metrics and prices. Everything a request makes is in a resource group of its own (groups.py), removed when
nothing of the request is left.

The other kinds are refused before any call: the validator names what this adapter provides, and every primitive the
interface asks for that it does not have yet is refused with UNSUPPORTED_RESOURCE_KIND (NOT_YET below). Each kind lands
with its own change, adds itself to supported_kinds, and leaves NOT_YET by being written.
"""
import abc

from ...models.errors import ErrorCode, InfrastructureError
from ...models.request import ComputeRequest, InfrastructureRequest
from ...models.response import CostEstimate, Health, PlannedResource, Usage
from ..base import CloudProvider, Problem, ProvisionContext
from . import connect, naming, pricing, translate
from .access import AzureAccess
from .api import API_VERSIONS, AzureApi
from .auth import AzureToken
from .clouds import CLOUDS
from .compute import AzureCompute
from .groups import AzureGroups
from .identity import AzureIdentity
from .monitoring import AzureMonitoring
from .network import AzureNetwork
from .regions import ZONES
from .storage import AzureStorage

# The quota names the orchestrator checks before it provisions, and the usages they are read from
# (learn.microsoft.com/rest/api/compute/usage/list, …/network/usages): regional vCPUs, virtual machines and Standard
# public addresses. Every family's vCPUs are kept too, under Azure's own names (standardDSv5Family…), and the plan names
# each machine's family, so that a family's own limit is checked too: a GPU family starts at zero in a new subscription.
QUOTA_NAMES = {"cores": "CPUS", "virtualMachines": "INSTANCES"}
ADDRESS_QUOTAS = ("StandardSkuPublicIpAddresses", "PublicIPAddresses")
MAX_DISK_GB = 32767  # Azure's largest managed disk
# How long the adapter waits for each kind of operation, in seconds; the operator's operation timeout caps every one
# (docs/research/RESEARCH-2026-11-03-azure.md, section 9.1; REQUIREMENTS.md lists them).
CEILINGS = {"resource_group": 300, "network": 600, "flow_log": 300, "identity": 300, "disk": 600, "storage_account": 600, "compute": 1200, "extension": 900,
            "attach": 600, "power": 900, "resize": 1200, "delete": 1200}


class AzureProvider(CloudProvider):
    name = "azure"
    supported_kinds: tuple[str, ...] = ("identity", "network", "storage", "compute")
    stoppable_kinds: tuple[str, ...] = ("compute",)
    grantable_kinds: tuple[str, ...] = ("compute", "storage")
    enforces_run_limit = False  # Azure has no maximum run duration: the sweeper stops a machine at its max_run_hours

    def __init__(self, settings, api: AzureApi | None = None, token=None, operation_timeout: int = 900, clock=None):
        self.settings = settings
        self.cloud = CLOUDS[settings.cloud]
        self.operation_timeout = operation_timeout
        self.api = api or AzureApi(settings.subscription_id, token or AzureToken(self.cloud, client_id=settings.identity_client_id, tenant_id=settings.tenant_id),
                                   cloud=self.cloud, operation_timeout=operation_timeout)
        self.groups = AzureGroups(self.api, self.ceiling)
        self.access = AzureAccess(self.api, self.cloud)
        self.network = AzureNetwork(self.api, settings, self.groups, self.ceiling)
        self.compute = AzureCompute(self.api, settings, self.groups, self.ceiling)
        self.storage = AzureStorage(self.api, settings, self.cloud, self.groups, self.compute, self.access, self.ceiling)
        self.identity = AzureIdentity(self.api, self.groups, self.ceiling)
        self.monitoring = AzureMonitoring(self.api, clock)
        self.prices = pricing.load(settings.prices_file)

    def ceiling(self, kind: str, default: int | None = None) -> int:
        """The wait for one kind of operation (a caller's own figure, else the table's), never above the operator's operation timeout."""
        wanted = default if default is not None else CEILINGS.get(kind, self.operation_timeout)
        return min(wanted, self.operation_timeout)

    # ---- knowledge
    def regions(self) -> list[str]:
        known = set(ZONES) if self.settings.cloud == "public" else set()
        return sorted(known | set(self.settings.zones))

    def zones(self, region: str) -> list[str]:
        """A region's logical zones ("1", "2", "3"), the first the default; [] for a region without zones, where a
        resource is regional."""
        if region in self.settings.zones:
            return list(self.settings.zones[region])
        return list(ZONES.get(region, [])) if self.settings.cloud == "public" else []

    def validate(self, request: InfrastructureRequest) -> list[Problem]:
        if request.region not in self.regions():
            return [Problem(ErrorCode.INVALID_REGION, f"The Azure adapter does not know the region {request.region}; regions are named as eastus2, and the "
                            "operator lists any other under providers.azure.zones", "region")]
        zones = self.zones(request.region)
        if request.zone and not zones:
            return [Problem(ErrorCode.INVALID_REGION, f"{request.region} has no availability zones; leave zone out", "zone")]
        if request.zone and request.zone not in zones:
            return [Problem(ErrorCode.INVALID_REGION, f"Zone {request.zone} is not one of {request.region}'s: {', '.join(zones)} (Azure's zones are numbered)", "zone")]
        problems = []
        groups = request.compute_groups()
        for group in groups:
            size = translate.compute_shape(group)
            software = translate.software(group, self.settings, None if isinstance(size, Problem) else size)
            found = ([size] if isinstance(size, Problem) else []) + ([software] if isinstance(software, Problem) else [])
            if request.network is None and group.network_id is None and not self.settings.default_subnet:
                found.append(Problem(ErrorCode.INVALID_NETWORK_CONFIGURATION, "Azure has no default network: add a network block or network_id (the operator "
                                     "names none in providers.azure.default_subnet)", "compute.network_id"))
            problems.extend(Problem(item.code, f"{group.group_name}: {item.message}" if len(groups) > 1 else item.message, item.field) for item in found)
        if request.storage is not None and request.storage.type == "persistent" and request.storage.size_gb > MAX_DISK_GB:
            problems.append(Problem(ErrorCode.INVALID_STORAGE_CONFIGURATION, f"Azure's managed disks hold at most {MAX_DISK_GB} GB; {request.storage.size_gb} asked",
                                    "storage.size_gb"))
        return problems

    def plan(self, request: InfrastructureRequest, ctx: ProvisionContext) -> list[PlannedResource]:
        unsupported = [kind for kind in request.kinds() if kind not in self.supported_kinds]
        if unsupported:  # the validator refuses these first; a plan never reaches here with one
            raise InfrastructureError(ErrorCode.UNSUPPORTED_RESOURCE_KIND, f"The azure adapter does not provide {', '.join(unsupported)} yet",
                                      {"provider": self.name, "kinds": unsupported})
        prefix, zone, region, planned = ctx.name, ctx.zone, ctx.region, []
        zoned = bool(self.zones(region))
        for identity in request.identities():  # first: machines carry them, and they are granted on everything after
            planned.append(PlannedResource(kind="identity", name=f"{prefix}-{identity.identity_name}", provider_mapping="Microsoft.ManagedIdentity/userAssignedIdentities",
                                           spec={"identity": identity.identity_name, "level": identity.level, "kubernetes": False, "region": region}))
        groups = request.compute_groups()
        own_network = f"{prefix}-net" if request.network else None
        if request.network:
            deny_egress = self.settings.deny_egress
            nat = request.network.nat and not deny_egress
            flow_logs = bool(self.settings.flow_logs_storage_account)
            rules = [f"{own_network}-allow-internal"] + ([f"{own_network}-allow-bastion-ssh"] if request.network.iap_ssh and self.settings.bastion_subnet else []) \
                + ([f"{own_network}-allow-public"] if request.network.public_access else []) + [f"{own_network}-allow-probes", f"{own_network}-deny-inbound"]
            rules += [f"{own_network}-allow-egress-internal", f"{own_network}-allow-egress-entra", f"{own_network}-deny-egress"] if deny_egress else []
            mapping = "Microsoft.Network/networkSecurityGroups, virtualNetworks (one subnet, no default outbound access)" \
                + (", publicIPAddresses, natGateways" if nat else "") + (", networkWatchers/flowLogs" if flow_logs else "")
            planned.append(PlannedResource(kind="network", name=own_network, provider_mapping=mapping,
                                           spec={"subnet": f"{prefix}-subnet", "cidr": request.network.cidr, "public_access": request.network.public_access,
                                                 "firewall_rules": rules, "nat": nat, "deny_egress": deny_egress, "flow_logs": flow_logs, "zone": zone, "region": region}))
        if request.storage and request.storage.type == "persistent":
            targets = [group for group in groups if request.storage.group in (None, group.group_name)]
            machines = [(f"{prefix}-{group.group_name}-disk-{index}", f"{prefix}-{group.group_name}-{index}") for group in targets for index in range(1, group.count + 1)]
            for index, (disk, machine) in enumerate(machines or [(f"{prefix}-disk-1", None)], start=1):
                planned.append(PlannedResource(kind="storage", name=disk, index=index,
                                               provider_mapping="Microsoft.Compute/disks" + (", virtualMachines (dataDisks)" if machine else ""),
                                               spec={"type": "persistent", "zone": zone, "disk_sku": translate.disk_sku(request.storage.performance),
                                                     "size_gb": request.storage.size_gb, "attach_to": machine, "region": region}))
        elif request.storage:
            planned.append(PlannedResource(kind="storage", name=naming.global_name("storage_account", prefix, ctx.request_id),
                                           provider_mapping="Microsoft.Storage/storageAccounts, blobServices/default/containers",
                                           spec={"type": "object", "location": region, "size_gb": request.storage.size_gb,
                                                 "redundancy": "Standard_ZRS" if zoned else "Standard_LRS", "region": region}))
        for group in groups:
            size = translate.compute_shape(group)
            software = translate.software(group, self.settings, None if isinstance(size, Problem) else size)
            for problem in (size, software):
                if isinstance(problem, Problem):  # a stored request whose profile the operator has since removed
                    raise InfrastructureError(problem.code, problem.message, {"field": problem.field})
            accelerator = {"type": size.gpu, "count": size.gpus} if size.gpu else None
            for index in range(1, group.count + 1):
                planned.append(PlannedResource(kind="compute", name=f"{prefix}-{group.group_name}-{index}", index=index,
                                               provider_mapping="Microsoft.Compute/virtualMachines (with its interface and OS disk), extensions/AADSSHLoginForLinux",
                                               spec={"group": group.group_name, "zone": zone, "machine_type": size.name, "cpu": size.cpu, "memory_gb": size.memory_gb,
                                                     "cpu_quota": size.quota, "image": software["image"], "plan": software["plan"], "profile": software["profile"],
                                                     "secure_boot": software["secure_boot"], "trusted_launch": software["trusted_launch"],
                                                     "drivers": software["drivers"], "boot_disk_gb": software["boot_disk_gb"], "public_ip": group.public_ip,
                                                     "accelerator": accelerator, "max_run_hours": group.max_run_hours,
                                                     "network": own_network or group.network_id or "the operator's default subnet", "identity": group.identity,
                                                     "region": region}))
        return planned

    def estimate_cost(self, planned: list[PlannedResource]) -> CostEstimate:
        return pricing.estimate(planned, self.prices)

    def estimate_resources(self, resources: list) -> CostEstimate:
        return pricing.estimate([pricing.as_planned(item, bool(self.zones(item.region))) for item in resources], self.prices)

    def takes_grants(self, kind: str, storage_type: str | None = None) -> bool:
        return kind in self.grantable_kinds and (kind != "storage" or storage_type == "object")  # a disk has no data role of its own

    def boot_disk_gb(self, group: ComputeRequest) -> int:
        size = translate.compute_shape(group)
        software = translate.software(group, self.settings, None if isinstance(size, Problem) else size)
        return group.boot_disk_gb if isinstance(software, Problem) else software["boot_disk_gb"]

    def profile_infos(self) -> list[dict]:
        return [{"name": name, "description": profile.description, "accelerator": profile.accelerator} for name, profile in sorted(self.settings.profiles.items())]

    def quota(self, region: str) -> dict[str, dict]:
        """Regional vCPUs, machines and Standard public addresses under the orchestrator's names, and each family's vCPUs
        under Azure's; {} when Compute's usages cannot be read (never an error)."""
        try:
            usages = self.api.list(self.api.subscription(f"/providers/Microsoft.Compute/locations/{region}/usages"), API_VERSIONS["Microsoft.Compute"])
        except InfrastructureError:
            return {}
        found = {}
        for usage in usages:
            name = str((usage.get("name") or {}).get("value") or "")
            if name in QUOTA_NAMES:
                found[QUOTA_NAMES[name]] = _entry(usage)
            elif name.endswith("Family"):
                found[name] = _entry(usage)
        try:
            network = {str((usage.get("name") or {}).get("value") or ""): usage
                       for usage in self.api.list(self.api.subscription(f"/providers/Microsoft.Network/locations/{region}/usages"), API_VERSIONS["Microsoft.Network"])}
        except InfrastructureError:
            network = {}
        address = next((network[name] for name in ADDRESS_QUOTAS if name in network), None)
        if address is not None:
            found["IN_USE_ADDRESSES"] = _entry(address)
        return found

    # ---- members and the request's group
    def resolve_member(self, member: str) -> dict | None:
        return self.access.resolve(member)

    def identity_member(self, resource) -> str:
        return f"serviceAccount:{resource.principal_id}"

    def finished(self, ctx: ProvisionContext) -> None:
        try:
            self.groups.remove(ctx)
        except InfrastructureError as exc:
            exc.details.setdefault("container", f"resource group {naming.resource_group(ctx.name, ctx.request_id)}")
            raise

    # ---- primitives
    def create_network(self, spec, planned, ctx):
        return self.network.create(spec, planned, ctx)

    def configure_firewall(self, network, ports, sources, ctx):
        return self.network.configure_firewall(network, ports, sources, ctx)

    def get_network(self, resource):
        return self.network.get(resource)

    def delete_network(self, resource, ctx):
        self.network.delete(resource, ctx)

    def create_storage(self, spec, planned, ctx):
        return self.storage.create(spec, planned, ctx)

    def attach_storage(self, storage, compute, ctx):
        return self.storage.attach(storage, compute, ctx)

    def get_storage(self, resource):
        return self.storage.get(resource)

    def delete_storage(self, resource, ctx):
        self.storage.delete(resource, ctx)

    def create_compute(self, spec, planned, ctx, network):
        return self.compute.create(spec, planned, ctx, network)

    def get_compute(self, resource):
        return self.compute.get(resource)

    def list_compute(self, labels):
        return self.compute.list(labels)

    def resize_compute(self, resource, cpu, memory_gb, ctx):
        return self.compute.resize(resource, cpu, memory_gb, ctx)

    def resize_problem(self, resource, cpu, memory_gb):
        shape = self.compute.shape(resource, cpu, memory_gb)
        return shape if isinstance(shape, Problem) else None

    def resize_preview(self, resource, cpu, memory_gb):
        shape = self.compute.shape(resource, cpu, memory_gb)
        if isinstance(shape, Problem):
            raise InfrastructureError(shape.code, shape.message, {"cpu": cpu, "memory_gb": memory_gb})
        return resource.model_copy(update={"machine_type": shape.name, "cpu": shape.cpu, "memory_gb": shape.memory_gb})

    def delete_compute(self, resource, ctx):
        self.compute.delete(resource, ctx)

    def create_identity(self, spec, planned, ctx):
        return self.identity.create(spec, planned, ctx)

    def get_identity(self, resource):
        return self.identity.get(resource)

    def delete_identity(self, resource, ctx):
        self.identity.delete(resource, ctx)

    # ---- optional capabilities
    def stop_compute(self, resource, ctx):
        return self.compute.stop(resource, ctx)

    def start_compute(self, resource, ctx):
        return self.compute.start(resource, ctx)

    def _targets(self, resource) -> list[tuple[str, str]]:
        """(scope, role table) pairs of a resource; empty for one that takes no grants (a disk, a network)."""
        if resource.kind.value == "compute":
            return [(resource.provider_id, "machine"), (self.compute.interface(resource), "interface")]
        if resource.kind.value == "storage" and resource.type == "object":
            return [(self.storage.container_scope(resource), "bucket")]
        return []

    def _principal(self, member: str, ctx: ProvisionContext) -> dict:
        found = ctx.principals.get(member)
        if found is None:  # kept with the access list from the request's first provisioning; looked up now only for one stored before that
            found = ctx.principals[member] = self.resolve_member(member)
        return found

    def grant(self, resource, member, level, ctx) -> list[str]:
        targets = self._targets(resource)
        if not targets:
            return []
        return self.access.grant(targets, self._principal(member, ctx), level, f"cloudinfra request {ctx.request_id} ({ctx.name}): {member} at {level}")

    def revoke(self, resource, member, ctx) -> None:
        targets = self._targets(resource)
        if not targets:
            return
        try:
            principal = self._principal(member, ctx)
        except InfrastructureError as exc:
            if exc.code != ErrorCode.INVALID_ACCESS:
                raise
            return  # Azure's directory cannot name it, so no grant names it either: nothing to remove
        self.access.revoke(targets, principal)

    def connect_info(self, resource, resources):
        profile = self.settings.profiles.get(resource.profile) if resource.kind.value == "compute" and resource.profile else None
        return connect.info(resource, resources, self.settings, self.settings.subscription_id, drivers=profile is not None and profile.accelerator == "required")

    def busy_cores(self, resource, minutes: int) -> float | None:
        return self.monitoring.busy_cores(resource, minutes)

    def resource_health(self, resource) -> Health:
        return self.monitoring.health(self.refresh(resource))

    def resource_usage(self, resource, minutes: int) -> Usage:
        return self.monitoring.usage(resource, minutes)


def _entry(usage: dict) -> dict:
    limit = usage.get("limit")
    return {"limit": limit if isinstance(limit, int) and limit >= 0 else None, "usage": usage.get("currentValue", 0)}


def _not_yet(operation: str):
    def refuse(self, *args, **kwargs):
        raise InfrastructureError(ErrorCode.UNSUPPORTED_RESOURCE_KIND, f"The azure adapter does not provide this yet ({operation})",
                                  {"provider": "azure", "operation": operation})
    refuse.__name__ = operation
    return refuse


# Every primitive of the interface this adapter has not written yet, refused with a structured error.
NOT_YET = tuple(sorted(AzureProvider.__abstractmethods__))
for _operation in NOT_YET:
    setattr(AzureProvider, _operation, _not_yet(_operation))
abc.update_abstractmethods(AzureProvider)
