"""GCPProvider: the CloudProvider interface implemented with Google Cloud, composed from the per-kind adapters."""
from ...models.errors import ErrorCode
from ...models.request import InfrastructureRequest
from ...models.response import CostEstimate, Health, PlannedResource, Usage
from ..base import CloudProvider, Problem, ProvisionContext
from . import pricing, translate
from .api import COMPUTE, GcpApi
from .auth import GoogleToken
from .compute import GcpCompute
from .database import GcpDatabase
from .monitoring import GcpMonitoring
from .network import GcpNetwork
from .storage import GcpStorage

QUOTA_METRICS = ("CPUS", "INSTANCES", "DISKS_TOTAL_GB", "SSD_TOTAL_GB", "IN_USE_ADDRESSES")


class GCPProvider(CloudProvider):
    name = "gcp"

    def __init__(self, settings, api: GcpApi | None = None, token=None):
        self.settings = settings
        self.api = api or GcpApi(settings.project_id, token or GoogleToken(impersonate=settings.impersonate_service_account))
        self.compute = GcpCompute(self.api, settings)
        self.storage = GcpStorage(self.api, settings, self.compute)
        self.network = GcpNetwork(self.api, settings)
        self.database = GcpDatabase(self.api, settings)
        self.monitoring = GcpMonitoring(self.api)

    # ---- knowledge
    def regions(self) -> list[str]:
        return sorted(set(translate.ZONES) | set(self.settings.zones))

    def zones(self, region: str) -> list[str]:
        suffixes = self.settings.zones.get(region) or translate.ZONES.get(region) or []
        return [f"{region}-{suffix}" for suffix in suffixes]

    def validate(self, request: InfrastructureRequest) -> list[Problem]:
        problems = []
        zones = self.zones(request.region)
        if not zones:
            problems.append(Problem(ErrorCode.INVALID_REGION, f"The Google Cloud adapter knows no zones for {request.region}; add them under providers.gcp.zones", "region"))
        elif request.zone and request.zone not in zones:
            problems.append(Problem(ErrorCode.INVALID_REGION, f"Zone {request.zone} is not in {request.region}; zones: {', '.join(zones)}", "zone"))
        if request.compute:
            problems.extend(translate.compute_problems(request.compute))
        if request.database:
            version = translate.database_version(request.database)
            if isinstance(version, Problem):
                problems.append(version)
        return problems

    def plan(self, request: InfrastructureRequest, ctx: ProvisionContext) -> list[PlannedResource]:
        prefix, zone, planned = ctx.name, ctx.zone or self.zones(request.region)[0], []
        private_database = request.database is not None and not request.database.public_access
        if request.network:
            rules = [f"{prefix}-net-allow-internal"] + ([f"{prefix}-net-allow-iap-ssh"] if request.network.iap_ssh else []) + ([f"{prefix}-net-allow-public"] if request.network.public_access else [])
            mapping = "compute.networks.insert, compute.subnetworks.insert, compute.firewalls.insert"
            planned.append(PlannedResource(kind="network", name=f"{prefix}-net", provider_mapping=mapping,
                                           spec={"subnet": f"{prefix}-subnet", "cidr": request.network.cidr, "public_access": request.network.public_access,
                                                 "firewall_rules": rules, "private_services": False}))
        if request.storage and request.storage.type == "persistent":
            count = request.compute.count if request.compute else 1
            for index in range(1, count + 1):
                planned.append(PlannedResource(kind="storage", name=f"{prefix}-disk-{index}", index=index,
                                               provider_mapping="compute.disks.insert" + (", compute.instances.attachDisk" if request.compute else ""),
                                               spec={"type": "persistent", "zone": zone, "disk_type": translate.disk_type(request.storage.performance), "size_gb": request.storage.size_gb,
                                                     "attach_to": f"{prefix}-vm-{index}" if request.compute else None}))
        elif request.storage:
            planned.append(PlannedResource(kind="storage", name=f"{self.settings.project_id}-{prefix}-{ctx.request_id[:6]}", provider_mapping="storage.buckets.insert",
                                           spec={"type": "object", "location": request.region, "size_gb": request.storage.size_gb}))
        if request.compute:
            machine = translate.machine_type(request.compute.cpu, request.compute.memory_gb)
            for index in range(1, request.compute.count + 1):
                planned.append(PlannedResource(kind="compute", name=f"{prefix}-vm-{index}", index=index, provider_mapping="compute.instances.insert",
                                               spec={"zone": zone, "machine_type": machine, "image": translate.image(request.compute.os), "boot_disk_gb": request.compute.boot_disk_gb,
                                                     "public_ip": request.compute.public_ip,
                                                     "network": f"{prefix}-net" if request.network else (request.compute.network_id or self.settings.default_network)}))
        if request.database:
            label, api_version = translate.database_version(request.database)
            mapping = ("compute.addresses.insert, servicenetworking.connections.create, " if private_database else "") + "sqladmin.instances.insert, sqladmin.databases.insert, sqladmin.users.insert"
            planned.append(PlannedResource(kind="database", name=f"{prefix}-db-{ctx.request_id[:6]}", provider_mapping=mapping,
                                           spec={"engine": request.database.engine, "version": label, "database_version": api_version, "tier": translate.DB_TIERS[request.database.size],
                                                 "high_availability": request.database.high_availability, "storage_gb": request.database.storage_gb, "private": private_database,
                                                 "port": translate.DB_PORTS[request.database.engine],
                                                 "network": (f"{prefix}-net" if request.network else request.database.network_id) if private_database else None}))
        return planned

    def estimate_cost(self, planned: list[PlannedResource]) -> CostEstimate:
        return pricing.estimate(planned)

    def quota(self, region: str) -> dict[str, dict]:
        try:
            data = self.api.get(f"{COMPUTE}/projects/{self.api.project}/regions/{region}")
        except Exception:  # best effort: a quota read never stops a request; the provisioner logs the absence
            return {}
        return {item["metric"]: {"limit": item.get("limit"), "usage": item.get("usage")} for item in data.get("quotas", []) if item.get("metric") in QUOTA_METRICS}

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

    def delete_compute(self, resource, ctx):
        self.compute.delete(resource, ctx)

    def prepare_network_for_database(self, network, ctx):
        return self.network.ensure_private_services(network, ctx)

    def create_database(self, spec, planned, ctx, network):
        return self.database.create(spec, planned, ctx, network)

    def get_database(self, resource):
        return self.database.get(resource)

    def delete_database(self, resource, ctx):
        self.database.delete(resource, ctx)

    def resource_health(self, resource) -> Health:
        return self.monitoring.health(self.refresh(resource))

    def resource_usage(self, resource, minutes: int) -> Usage:
        return self.monitoring.usage(resource, minutes)
