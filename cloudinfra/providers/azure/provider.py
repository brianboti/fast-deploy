"""AzureProvider: the CloudProvider interface implemented with Microsoft Azure.

Built in steps (docs/research/RESEARCH-2026-11-03-azure.md, section 10). Step 1 gave the adapter its credential
(auth.py), its client (api.py), the clouds, the regions and their zones, and the quota read. Step 2 gave it release
A's kinds: identity, network, storage (disks and buckets) and compute, with grants, stop and start, resize, connect
blocks, metrics and prices. Step 4 gives it the managed services: secrets (a vault per request), queues (a Service Bus
namespace per request), a registry (a repository prefix in the operator's registry), databases and caches (private
behind an endpoint). Everything a request makes is in a resource group of its own (groups.py), removed when nothing of
the request is left, the request's vault purged first.

The other kinds are refused before any call: the validator names what this adapter provides, and every primitive the
interface asks for that it does not have yet is refused with UNSUPPORTED_RESOURCE_KIND (NOT_YET below). Each kind lands
with its own change, adds itself to supported_kinds, and leaves NOT_YET by being written.
"""
import abc

from ...models.errors import ErrorCode, InfrastructureError
from ...models.request import ComputeRequest, InfrastructureRequest
from ...models.response import CostEstimate, Health, PlannedResource, Usage
from ..base import CloudProvider, Problem, ProvisionContext
from . import connect, naming, pricing, private, translate
from .access import AzureAccess
from .api import API_VERSIONS, AzureApi
from .auth import AzureToken
from .cache import AzureCache
from .clouds import CLOUDS
from .compute import AzureCompute
from .database import AzureDatabase
from .groups import AzureGroups
from .identity import AzureIdentity
from .monitoring import AzureMonitoring
from .network import AzureNetwork
from .queue import AzureQueue
from .regions import ZONES
from .registry import AzureRegistry, prefix_of, registry_of
from .secret import AzureSecret
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
            "attach": 600, "power": 900, "resize": 1200, "delete": 1200, "vault": 600, "namespace": 900, "database": 1800, "cache": 1800}
MAX_LOCK_SECONDS = 300  # Service Bus's longest message lock


class AzureProvider(CloudProvider):
    name = "azure"
    supported_kinds: tuple[str, ...] = ("identity", "network", "storage", "compute", "database", "cache", "queue", "secret", "registry")
    stoppable_kinds: tuple[str, ...] = ("compute", "database")
    grantable_kinds: tuple[str, ...] = ("compute", "storage", "secret", "queue", "registry")
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
        self.secret = AzureSecret(self.api, settings, self.cloud, self.groups, self.tenant, self.ceiling)
        self.queue = AzureQueue(self.api, self.groups, self.ceiling)
        self.registries = AzureRegistry(self.api, settings, self.cloud, self.tenant)
        self.database = AzureDatabase(self.api, self.groups, self.tenant, self.ceiling)
        self.cache = AzureCache(self.api, self.groups, self.ceiling)
        self.monitoring = AzureMonitoring(self.api, clock)
        self.prices = pricing.load(settings.prices_file)
        self._tenant = settings.tenant_id

    def tenant(self) -> str:
        """The directory the vaults and the databases' Entra sign-in trust: the operator's tenant_id, else the subscription's own."""
        if not self._tenant:
            self._tenant = self.api.get(self.api.subscription(), API_VERSIONS["Microsoft.Resources"]).get("tenantId", "")
        return self._tenant

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
        if request.database is not None:
            problems.extend(translate.database_problems(request.database))
        if request.cache is not None:
            problems.extend(translate.cache_problems(request.cache))
            if private.zone_of(self.cloud, "redis") is None:
                problems.append(Problem(ErrorCode.UNSUPPORTED_RESOURCE_KIND, f"The adapter offers caches in the public cloud only: Microsoft documents no private "
                                        f"DNS zone for Azure Managed Redis in the {self.cloud.name} cloud", "cache"))
        for queue in request.queues():
            if queue.subscription and queue.ack_deadline_seconds > MAX_LOCK_SECONDS:
                problems.append(Problem(ErrorCode.INVALID_REQUEST, f"queue {queue.name}: Service Bus locks a message {MAX_LOCK_SECONDS} seconds at most; "
                                        f"{queue.ack_deadline_seconds} asked", "queue.ack_deadline_seconds"))
        for registry in request.registries():
            if not self.settings.registry:
                problems.append(Problem(ErrorCode.INVALID_REQUEST, "This operator names no container registry (providers.azure.registry), in which a request's "
                                        "registry would be a repository prefix", "registry"))
                break
            if registry.immutable_tags:
                problems.append(Problem(ErrorCode.INVALID_REQUEST, f"registry {registry.name}: Azure makes a tag immutable only one by one after it is pushed, "
                                        "which the delivery service does not do on Azure yet; leave immutable_tags out and release by digest", "registry.immutable_tags"))
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
        if request.database:
            database = request.database
            label, api_version = translate.database_version(database)
            sku, tier = translate.DB_SKUS[database.size]
            private_database = not database.public_access
            mode = ("ZoneRedundant" if zoned else "SameZone") if database.high_availability else "Disabled"
            mapping = f"{translate.translate_namespace(database.engine)}/flexibleServers, databases, Microsoft.Authorization/locks" + \
                (", Microsoft.Network/privateEndpoints, privateDnsZones" if private_database else "")
            planned.append(PlannedResource(kind="database", name=naming.global_name("flexible_server", prefix, ctx.request_id, "db"), provider_mapping=mapping,
                                           spec={"engine": database.engine, "version": label, "database_version": api_version, "tier": sku, "sku_tier": tier,
                                                 "high_availability": database.high_availability, "high_availability_mode": mode,
                                                 "storage_gb": translate.database_storage(database), "private": private_database,
                                                 "port": translate.DB_PORTS[database.engine], "extended_support": False,
                                                 "recommended_version": translate.DB_VERSIONS[database.engine]["default"],
                                                 "network": (own_network or database.network_id) if private_database else None, "region": region}))
        if request.cache:
            sku, memory = translate.cache_sku(request.cache.memory_gb)
            planned.append(PlannedResource(kind="cache", name=naming.global_name("redis", prefix, ctx.request_id), provider_mapping=(
                "Microsoft.Cache/redisEnterprise, databases/default, Microsoft.Network/privateEndpoints, privateDnsZones"),
                spec={"tier": sku, "memory_gb": memory, "version": translate.CACHE_VERSION, "high_availability": request.cache.high_availability,
                      "network": own_network or request.cache.network_id, "region": region}))
        for index, queue in enumerate(request.queues()):
            mapping = "Microsoft.ServiceBus/namespaces/topics" + (", subscriptions" if queue.subscription else "") + \
                ("; the request's Standard namespace, with its first queue" if index == 0 else "")
            planned.append(PlannedResource(kind="queue", name=f"{prefix}-{queue.name}", provider_mapping=mapping,
                                           spec={"queue": queue.name, "subscription": queue.subscription, "retention_hours": queue.retention_hours,
                                                 "namespace": index == 0, "region": region}))
        for secret in request.secrets():
            planned.append(PlannedResource(kind="secret", name=f"{prefix}-{secret.name}", provider_mapping="Microsoft.KeyVault/vaults/secrets (the vault's own API)",
                                           spec={"secret": secret.name, "versions": 1 if secret.generate_value else 0, "region": region}))
        # The request's own credentials go into secrets it owns, never through a response: the generated database password,
        # and the cache's access key. Secret names are unique within the request's own vault, so no salt is needed.
        for owner, secret, purpose in (("database", "db-password", "database_password"), ("cache", "cache-auth", "cache_auth")):
            if getattr(request, owner) is not None:
                planned.append(PlannedResource(kind="secret", name=f"{prefix}-{secret}", provider_mapping="Microsoft.KeyVault/vaults/secrets (the vault's own API)",
                                               spec={"secret": secret, "versions": 1, "purpose": purpose, "region": region}))
        for registry in request.registries():
            name = f"{prefix}-{registry.name}"
            planned.append(PlannedResource(kind="registry", name=name, provider_mapping="a repository prefix in the operator's registry (nothing made until a push)",
                                           spec={"registry": registry.name, "prefix": prefix_of(name, ctx.request_id), "size_gb": registry.size_gb,
                                                 "immutable_tags": registry.immutable_tags, "region": region}))
        return planned

    def estimate_cost(self, planned: list[PlannedResource]) -> CostEstimate:
        return pricing.estimate(planned, self.prices)

    def estimate_resources(self, resources: list) -> CostEstimate:
        planned = [pricing.as_planned(item, bool(self.zones(item.region))) for item in resources]
        first_queue = next((item for item in planned if item.kind == "queue"), None)
        if first_queue is not None:  # the request's namespace, priced once, as in its plan
            first_queue.spec["namespace"] = True
        return pricing.estimate(planned, self.prices)

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
        """The request's vault is deleted and purged, then its group goes with whatever is left of the request in it."""
        try:
            self.secret.remove_vault(ctx)
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
        if resource.type == "object" and self.api.find(self.api.arm(resource.provider_id), API_VERSIONS["Microsoft.Storage"]) is not None:
            self.storage.refuse_if_holding(resource)  # before any grant goes: a bucket kept keeps its members
        self._release(resource, ctx)
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

    def create_secret(self, spec, planned, ctx):
        return self.secret.create(spec, planned, ctx)

    def get_secret(self, resource):
        return self.secret.get(resource)

    def delete_secret(self, resource, ctx):
        self._release(resource, ctx)
        self.secret.delete(resource, ctx)

    def create_queue(self, spec, planned, ctx):
        return self.queue.create(spec, planned, ctx)

    def get_queue(self, resource):
        return self.queue.get(resource)

    def delete_queue(self, resource, ctx):
        self._release(resource, ctx)
        self.queue.delete(resource, ctx)

    def create_registry(self, spec, planned, ctx):
        return self.registries.create(spec, planned, ctx)

    def get_registry(self, resource):
        return self.registries.get(resource)

    def delete_registry(self, resource, ctx):
        self._release(resource, ctx)
        self.registries.delete(resource, ctx)

    def create_database(self, spec, planned, ctx, network):
        return self.database.create(spec, planned, ctx, network)

    def get_database(self, resource):
        return self.database.get(resource)

    def delete_database(self, resource, ctx):
        self.database.delete(resource, ctx)

    def create_cache(self, spec, planned, ctx, network):
        return self.cache.create(spec, planned, ctx, network)

    def get_cache(self, resource):
        return self.cache.get(resource)

    def delete_cache(self, resource, ctx):
        self.cache.delete(resource, ctx)

    # ---- optional capabilities
    def stop_compute(self, resource, ctx):
        return self.compute.stop(resource, ctx)

    def start_compute(self, resource, ctx):
        return self.compute.start(resource, ctx)

    def stop_database(self, resource, ctx):
        return self.database.stop(resource, ctx)

    def start_database(self, resource, ctx):
        return self.database.start(resource, ctx)

    def put_secret_value(self, resource, value, ctx):
        return self.secret.put_value(resource, value, ctx)

    def read_cache_auth(self, resource) -> str:
        return self.cache.read_auth(resource)

    def _targets(self, resource) -> list[tuple]:
        """(scope, role table[, repository prefix]) of a resource; empty for one that takes no grants (a disk, a network)."""
        kind = resource.kind.value
        if kind == "compute":
            return [(resource.provider_id, "machine"), (self.compute.interface(resource), "interface")]
        if kind == "storage" and resource.type == "object":
            return [(self.storage.container_scope(resource), "bucket")]
        if kind == "secret":
            return [(resource.provider_id, "secret")]
        if kind == "queue":
            return [(resource.topic, "topic")] + ([(resource.subscription, "subscription")] if resource.subscription else [])
        if kind == "registry":
            return [(registry_of(resource), "registry", resource.repository)]  # the registry the prefix was made in
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

    def _release(self, resource, ctx) -> None:
        """Before a resource is deleted, remove the role assignments this service made on it: Azure keeps an assignment whose
        scope is gone, where it still counts against the subscription's 5,000, and a registry's scope is the operator's
        registry, which outlives the request. A machine's are released by the orchestrator before its delete."""
        if resource.kind.value == "storage" and resource.type != "object":
            return
        # every member the request ever had, not only those recorded on the resource: a grant that failed halfway may have
        # made one of its assignments before it failed, and a revoke of one never made finds nothing
        for member in dict.fromkeys([*(grant.member for grant in resource.access), *ctx.principals]):
            self.revoke(resource, member, ctx)

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
