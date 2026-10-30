"""GCPProvider: the CloudProvider interface implemented with Google Cloud, composed from the per-kind adapters."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.request import RESOURCE_KINDS, ComputeRequest, InfrastructureRequest
from ...models.response import CostEstimate, Health, PlannedResource, Usage
from ..base import CloudProvider, Problem, ProvisionContext
from . import connect, pricing, translate
from .api import COMPUTE, GcpApi
from .armor import rules as armor_rules
from .auth import GoogleToken
from .budget import GcpBudget
from .cache import GcpCache
from .cluster import GcpCluster
from .compute import GcpCompute
from .data import GcpNoSql, GcpWarehouse
from .database import GcpDatabase
from .dns import GcpDns
from .iam import GcpIam
from .identity import GcpIdentity
from .kms import GcpKms, key_name, key_ring
from .loadbalancer import GcpLoadBalancer
from .logs import GcpLogExport
from .monitor import GcpMonitor
from .monitoring import GcpMonitoring
from .network import GcpNetwork
from .queue import GcpQueue
from .run import GcpRun, GcpRunJob
from .secret import GcpSecret
from .storage import GcpStorage

QUOTA_METRICS = ("CPUS", "INSTANCES", "DISKS_TOTAL_GB", "SSD_TOTAL_GB", "IN_USE_ADDRESSES")
HTTP_LB_MAPPING = ("compute.instanceGroups.insert, compute.firewalls.insert, compute.healthChecks.insert, compute.backendServices.insert, compute.urlMaps.insert, "
                   "compute.targetHttpProxies.insert, compute.globalAddresses.insert, compute.globalForwardingRules.insert")
TCP_LB_MAPPING = ("compute.instanceGroups.insert, compute.firewalls.insert, compute.regionHealthChecks.insert, compute.regionBackendServices.insert, compute.addresses.insert, "
                  "compute.forwardingRules.insert")


class GCPProvider(CloudProvider):
    name = "gcp"
    supported_kinds = RESOURCE_KINDS
    stoppable_kinds = ("compute", "database")
    grantable_kinds = ("compute", "storage", "secret", "queue", "cluster", "service", "job", "dns", "warehouse")

    def __init__(self, settings, api: GcpApi | None = None, token=None, kube=None):
        self.settings = settings
        self.api = api or GcpApi(settings.project_id, token or GoogleToken(impersonate=settings.impersonate_service_account), quota_project=settings.quota_project)
        self.compute = GcpCompute(self.api, settings)
        self.storage = GcpStorage(self.api, settings, self.compute)
        self.network = GcpNetwork(self.api, settings)
        self.database = GcpDatabase(self.api, settings)
        self.cache = GcpCache(self.api, settings)
        self.queue = GcpQueue(self.api, settings)
        self.secret = GcpSecret(self.api, settings)
        self.cluster = GcpCluster(self.api, settings, kube)
        self.run = GcpRun(self.api, settings)
        self.job = GcpRunJob(self.api, settings)
        self.identity = GcpIdentity(self.api, settings)
        self.dns = GcpDns(self.api, settings)
        self.monitor = GcpMonitor(self.api, settings)
        self.budget = GcpBudget(self.api, settings)
        self.kms = GcpKms(self.api, settings)
        self.warehouse, self.nosql = GcpWarehouse(self.api, settings), GcpNoSql(self.api, settings)
        self.load_balancer = GcpLoadBalancer(self.api, settings, self.network)
        self.monitoring = GcpMonitoring(self.api)
        self.iam = GcpIam(self.api)
        self.log_export = GcpLogExport(self.api, settings, self.iam, self.warehouse)

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
        for group in request.compute_groups():
            software = translate.software(group, self.settings)
            found = translate.compute_problems(group) + ([software] if isinstance(software, Problem) else [])
            problems.extend(Problem(item.code, f"{group.group_name}: {item.message}" if len(request.compute_groups()) > 1 else item.message, item.field)
                            for item in found)
        if request.database:
            version = translate.database_version(request.database)
            if isinstance(version, Problem):
                problems.append(version)
        if request.cache:
            version = translate.cache_version(request.cache)
            if isinstance(version, Problem):
                problems.append(version)
        for cluster in request.clusters():
            problems.extend(translate.cluster_problems(cluster))
            outside = [name for name in (cluster.public.hostnames if cluster.public else [])
                       if not any(name == domain or name.endswith("." + domain) for domain in self.settings.public_domains)]
            if outside:
                problems.append(Problem(ErrorCode.INVALID_CLUSTER_CONFIGURATION, f"Not in a domain the operator lets a front door serve: {', '.join(outside)}; "
                                        f"allowed: {', '.join(self.settings.public_domains) or 'none (plain HTTP on the address only)'}", "cluster.public.hostnames"))
            if cluster.registries and not self.settings.cluster_node_accounts:
                problems.append(Problem(ErrorCode.INVALID_CLUSTER_CONFIGURATION, "This operator runs cluster nodes as the project's default account, so a request "
                                        "cannot give them registries (providers.gcp.cluster_node_accounts)", "cluster.registries"))
            elif unknown := [registry for registry in cluster.registries if registry not in self.settings.registries]:
                problems.append(Problem(ErrorCode.INVALID_CLUSTER_CONFIGURATION, f"Not a registry the operator lets requests name: {', '.join(unknown)}; allowed: "
                                        f"{', '.join(self.settings.registries) or 'none'}", "cluster.registries"))
        for service in request.services():
            problems.extend(translate.service_problems(service))
            if service.public and not self.settings.allow_public_services:
                problems.append(Problem(ErrorCode.INVALID_SERVICE_CONFIGURATION, f"service {service.service_name}: this operator does not allow services callable "
                                        "without signing in (providers.gcp.allow_public_services)", "service.public"))
        for job in request.jobs():
            problems.extend(translate.service_problems(job, "job"))
        outside = [name for name in (request.load_balancer.hostnames if request.load_balancer else [])
                   if not any(name == domain or name.endswith("." + domain) for domain in self.settings.public_domains)]
        if outside:
            problems.append(Problem(ErrorCode.INVALID_LOAD_BALANCER_CONFIGURATION, f"Not in a domain the operator lets requests use: {', '.join(outside)}; allowed: "
                                    f"{', '.join(self.settings.public_domains) or 'none'}", "load_balancer.hostnames"))
        if request.dns is not None and not any(request.dns.zone == domain or request.dns.zone.endswith("." + domain) for domain in self.settings.public_domains):
            problems.append(Problem(ErrorCode.INVALID_DNS_CONFIGURATION, f"Not a domain the operator lets requests use: {request.dns.zone}; allowed: "
                                    f"{', '.join(self.settings.public_domains) or 'none'}", "dns.zone"))
        return problems

    def plan(self, request: InfrastructureRequest, ctx: ProvisionContext) -> list[PlannedResource]:
        prefix, zone, planned = ctx.name, ctx.zone or self.zones(request.region)[0], []
        if request.budget is not None:  # first: it counts from the request's first cost
            planned.append(PlannedResource(kind="budget", name=f"{prefix}-budget", provider_mapping="monitoring.notificationChannels.create, billingbudgets.budgets.create",
                                           spec={"watch": request.budget.request_id or ctx.request_id, "monthly_amount": request.budget.monthly_amount}))
        kms_key = None
        if request.encryption_key is not None:  # before everything it encrypts, and so destroyed after it
            ring = key_ring(prefix, ctx.request_id)
            kms_key = key_name(self.settings.project_id, request.region, ring)
            encrypts = (["storage" if request.storage.type == "persistent" else "bucket"] if request.storage else []) + (["compute"] if request.compute else []) \
                + (["database"] if request.database else [])
            planned.append(PlannedResource(kind="encryption_key", name=f"{prefix}-key", provider_mapping="cloudkms.keyRings.create, cloudkms.cryptoKeys.create, "
                                           "cloudkms.cryptoKeys.setIamPolicy (service agents)", spec={"key_ring": ring, "key": kms_key, "encrypts": encrypts}))
        for identity in request.identities():  # first: machines run as them, and they are granted on everything after
            planned.append(PlannedResource(kind="identity", name=f"{prefix}-{identity.identity_name}", provider_mapping="iam.serviceAccounts.create"
                                           + (", iam.serviceAccounts.setIamPolicy (Workload Identity)" if request.clusters() else ""),
                                           spec={"identity": identity.identity_name, "level": identity.level, "kubernetes": bool(request.clusters())}))
        groups = request.compute_groups()
        own_network = f"{prefix}-net" if request.network else None
        private_database = request.database is not None and not request.database.public_access
        if request.network:
            rules = [f"{prefix}-net-allow-internal"] + ([f"{prefix}-net-allow-iap-ssh"] if request.network.iap_ssh else []) + ([f"{prefix}-net-allow-public"] if request.network.public_access else [])
            behind_nat = sum(group.count for group in groups) + sum(cluster.node_count for cluster in request.clusters())
            planned.append(PlannedResource(kind="network", name=own_network, provider_mapping="compute.networks.insert, compute.subnetworks.insert, compute.firewalls.insert"
                                           + (", compute.routers.insert (Cloud NAT)" if request.network.nat else ""),
                                           spec={"subnet": f"{prefix}-subnet", "cidr": request.network.cidr, "public_access": request.network.public_access,
                                                 "firewall_rules": rules, "private_services": False, "nat": request.network.nat, "machines": max(behind_nat, 1)}))
        if request.storage and request.storage.type == "persistent":
            targets = [group for group in groups if request.storage.group in (None, group.group_name)]
            machines = [(f"{prefix}-{group.group_name}-disk-{index}", f"{prefix}-{group.group_name}-{index}") for group in targets for index in range(1, group.count + 1)]
            snapshots = request.storage.snapshots.model_dump() if request.storage.snapshots else None
            for index, (disk, machine) in enumerate(machines or [(f"{prefix}-disk-1", None)], start=1):
                planned.append(PlannedResource(kind="storage", name=disk, index=index, provider_mapping=("compute.resourcePolicies.insert (snapshot schedule), " if snapshots else "")
                                               + "compute.disks.insert" + (", compute.instances.attachDisk" if machine else ""),
                                               spec={"type": "persistent", "zone": zone, "disk_type": translate.disk_type(request.storage.performance), "size_gb": request.storage.size_gb,
                                                     "attach_to": machine, "snapshots": snapshots, "kms_key": kms_key}))
        elif request.storage:
            planned.append(PlannedResource(kind="storage", name=f"{self.settings.project_id}-{prefix}-{ctx.request_id[:6]}", provider_mapping="storage.buckets.insert",
                                           spec={"type": "object", "location": request.region, "size_gb": request.storage.size_gb, "kms_key": kms_key}))
        for group in groups:
            machine, cpu, memory = translate.compute_shape(group)
            accelerator = {"type": translate.ACCELERATORS[group.accelerator.type], "count": group.accelerator.count} if group.accelerator else None
            software = translate.software(group, self.settings)
            if isinstance(software, Problem):  # a stored request whose profile the operator has since removed
                raise InfrastructureError(software.code, software.message, {"field": software.field})
            for index in range(1, group.count + 1):
                planned.append(PlannedResource(kind="compute", name=f"{prefix}-{group.group_name}-{index}", index=index, provider_mapping="compute.instances.insert",
                                               spec={"group": group.group_name, "zone": zone, "machine_type": machine, "cpu": cpu, "memory_gb": memory,
                                                     "image": software["image"], "profile": software["profile"], "secure_boot": software["secure_boot"],
                                                     "drivers": software["drivers"],
                                                     "boot_disk_gb": software["boot_disk_gb"], "public_ip": group.public_ip, "accelerator": accelerator,
                                                     "max_run_hours": group.max_run_hours, "network": own_network or group.network_id or self.settings.default_network,
                                                     "identity": group.identity, "kms_key": kms_key}))
        if request.database:
            label, api_version = translate.database_version(request.database)
            mapping = ("compute.addresses.insert, servicenetworking.connections.create, " if private_database else "") + "sqladmin.instances.insert, sqladmin.databases.insert, sqladmin.users.insert"
            planned.append(PlannedResource(kind="database", name=f"{prefix}-db-{ctx.request_id[:6]}", provider_mapping=mapping,
                                           spec={"engine": request.database.engine, "version": label, "database_version": api_version, "tier": translate.DB_TIERS[request.database.size],
                                                 "high_availability": request.database.high_availability, "storage_gb": request.database.storage_gb, "private": private_database,
                                                 "port": translate.DB_PORTS[request.database.engine],
                                                 "extended_support": label in translate.EXTENDED_SUPPORT[request.database.engine],
                                                 "recommended_version": translate.DB_VERSIONS[request.database.engine]["default"],
                                                 "network": (own_network or request.database.network_id) if private_database else None, "kms_key": kms_key}))
        if request.cache:
            label, api_version = translate.cache_version(request.cache)
            planned.append(PlannedResource(kind="cache", name=f"{prefix}-cache", provider_mapping="redis.instances.create",
                                           spec={"tier": "STANDARD_HA" if request.cache.high_availability else "BASIC", "memory_gb": request.cache.memory_gb, "version": label,
                                                 "redis_version": api_version, "network": own_network or request.cache.network_id}))
        for queue in request.queues():
            planned.append(PlannedResource(kind="queue", name=f"{prefix}-{queue.name}", provider_mapping="pubsub.topics.create" + (", pubsub.subscriptions.create" if queue.subscription else ""),
                                           spec={"queue": queue.name, "subscription": queue.subscription, "retention_hours": queue.retention_hours}))
        for secret in request.secrets():
            mapping = "secretmanager.secrets.create" + (", secretmanager.secrets.addVersion" if secret.generate_value else "")
            planned.append(PlannedResource(kind="secret", name=f"{prefix}-{secret.name}", provider_mapping=mapping, spec={"secret": secret.name, "versions": 1 if secret.generate_value else 0}))
        # The request's own credentials go into secrets it owns, never through a response: the generated database password,
        # and the cache's AUTH string copied from the provider. Secret ids are unique in the project, and request names are
        # not, so the request id is part of the name, as it is of the database's.
        for owner, secret, purpose in (("database", "db-password", "database_password"), ("cache", "cache-auth", "cache_auth")):
            if getattr(request, owner) is not None:
                planned.append(PlannedResource(kind="secret", name=f"{prefix}-{secret}-{ctx.request_id[:6]}",
                                               provider_mapping="secretmanager.secrets.create, secretmanager.secrets.addVersion",
                                               spec={"secret": secret, "versions": 1, "purpose": purpose}))
        for warehouse in request.warehouses():
            planned.append(PlannedResource(kind="warehouse", name=f"{prefix}-{warehouse.warehouse_name}", provider_mapping="bigquery.datasets.create",
                                           spec={"warehouse": warehouse.warehouse_name, "table_expiration_days": warehouse.table_expiration_days}))
        for nosql in request.nosqls():
            planned.append(PlannedResource(kind="nosql", name=f"{prefix}-{nosql.nosql_name}", provider_mapping="datastore.databases.create",
                                           spec={"nosql": nosql.nosql_name}))
        for cluster in request.clusters():
            mapping = "container.clusters.create" + (", iam.serviceAccounts.create, projects.setIamPolicy (node role)" if self.settings.cluster_node_accounts else "")
            mapping += ", artifactregistry.repositories.setIamPolicy" if cluster.registries else ""
            mapping += ", compute.globalAddresses.insert" + (", compute.sslCertificates.insert" if cluster.public and cluster.public.hostnames else "") \
                if cluster.public else ""
            planned.append(PlannedResource(kind="cluster", name=f"{prefix}-{cluster.cluster_name}", provider_mapping=mapping,
                                           spec={"cluster": cluster.cluster_name, "location": zone, "registries": list(cluster.registries),
                                                 "machine_type": translate.machine_type(cluster.node_cpu, cluster.node_memory_gb),
                                                 "node_count": cluster.node_count, "node_disk_gb": cluster.node_disk_gb, "private_nodes": cluster.private_nodes,
                                                 "network": own_network or cluster.network_id or self.settings.default_network,
                                                 "public": {"hostnames": list(cluster.public.hostnames),
                                                            "waf_rules": len(armor_rules(cluster.public.waf)) if cluster.public.waf else 0} if cluster.public else None}))
        for service in request.services():
            mapping = "run.services.create, iam.serviceAccounts.create" + (", run.services.setIamPolicy (invoker check off)" if service.public else "")
            planned.append(PlannedResource(kind="service", name=f"{prefix}-{service.service_name}", provider_mapping=mapping,
                                           spec={"service": service.service_name, "cpu": service.cpu, "memory_mb": service.memory_mb, "min_instances": service.min_instances,
                                                 "max_instances": service.max_instances, "public": service.public, "ingress": service.ingress}))
        for job in request.jobs():
            mapping = "run.jobs.create, iam.serviceAccounts.create" + (", cloudscheduler.jobs.create, run.jobs.setIamPolicy (the schedule's account)" if job.schedule else "")
            planned.append(PlannedResource(kind="job", name=f"{prefix}-{job.job_name}", provider_mapping=mapping,
                                           spec={"job": job.job_name, "cpu": job.cpu, "memory_mb": job.memory_mb, "tasks": job.tasks,
                                                 "schedule": {"cron": job.schedule.cron, "time_zone": job.schedule.time_zone} if job.schedule else None}))
        if request.load_balancer:
            balancer = request.load_balancer
            group = request.group(balancer.backend_group)
            backends = [f"{prefix}-{group.group_name}-{index}" for index in range(1, group.count + 1)] if group else list(balancer.backends or [])
            planned.append(PlannedResource(kind="load_balancer", name=f"{prefix}-lb", provider_mapping=HTTP_LB_MAPPING if balancer.type == "http" else TCP_LB_MAPPING,
                                           spec={"type": balancer.type, "port": balancer.port, "backend_group": group.group_name if group else "external", "backends": backends,
                                                 "health_check_path": balancer.health_check_path if balancer.type == "http" else None,
                                                 "hostnames": list(balancer.hostnames), "waf_rules": len(armor_rules(balancer.waf)) if balancer.waf else 0}))
        if request.monitor is not None:  # after what it watches
            planned.append(PlannedResource(kind="monitor", name=f"{prefix}-monitor", provider_mapping="monitoring.notificationChannels.create, "
                                           "monitoring.uptimeCheckConfigs.create, monitoring.alertPolicies.create",
                                           spec={"checks": len(request.monitor.uptime), "notify": len(request.monitor.notify)}))
        if request.log_export is not None:  # after what it reads and where it writes
            planned.append(PlannedResource(kind="log_export", name=f"{prefix}-logs", provider_mapping="logging.sinks.create, and a grant on the destination",
                                           spec={"to": request.log_export.to, "min_severity": request.log_export.min_severity}))
        if request.dns is not None:  # last: its records may point at what the request made
            planned.append(PlannedResource(kind="dns", name=f"{prefix}-zone", provider_mapping="dns.managedZones.create, dns.changes.create",
                                           spec={"zone": request.dns.zone, "records": len(request.dns.records)}))
        return planned

    def estimate_cost(self, planned: list[PlannedResource]) -> CostEstimate:
        return pricing.estimate(planned)

    def estimate_resources(self, resources: list) -> CostEstimate:
        return pricing.estimate([pricing.as_planned(item) for item in resources])

    def takes_grants(self, kind: str, storage_type: str | None = None) -> bool:
        return kind in self.grantable_kinds and (kind != "storage" or storage_type == "object")  # a disk has no policy of its own

    def boot_disk_gb(self, group: ComputeRequest) -> int:
        software = translate.software(group, self.settings)
        return group.boot_disk_gb if isinstance(software, Problem) else software["boot_disk_gb"]

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

    def resize_problem(self, resource, cpu, memory_gb):
        shape = self.compute.shape(resource, cpu, memory_gb)
        return shape if isinstance(shape, Problem) else None

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

    def create_cache(self, spec, planned, ctx, network):
        return self.cache.create(spec, planned, ctx, network)

    def get_cache(self, resource):
        return self.cache.get(resource)

    def delete_cache(self, resource, ctx):
        self.cache.delete(resource, ctx)

    def create_queue(self, spec, planned, ctx):
        return self.queue.create(spec, planned, ctx)

    def get_queue(self, resource):
        return self.queue.get(resource)

    def delete_queue(self, resource, ctx):
        self.queue.delete(resource, ctx)

    def create_secret(self, spec, planned, ctx):
        return self.secret.create(spec, planned, ctx)

    def get_secret(self, resource):
        return self.secret.get(resource)

    def delete_secret(self, resource, ctx):
        self.secret.delete(resource, ctx)

    def create_cluster(self, spec, planned, ctx, network):
        return self.cluster.create(spec, planned, ctx, network)

    def get_cluster(self, resource):
        return self.cluster.get(resource)

    def delete_cluster(self, resource, ctx):
        self.cluster.delete(resource, ctx)

    def create_warehouse(self, spec, planned, ctx):
        return self.warehouse.create(spec, planned, ctx)

    def get_warehouse(self, resource):
        return self.warehouse.get(resource)

    def delete_warehouse(self, resource, ctx):
        self.warehouse.delete(resource, ctx)

    def create_nosql(self, spec, planned, ctx):
        return self.nosql.create(spec, planned, ctx)

    def get_nosql(self, resource):
        return self.nosql.get(resource)

    def delete_nosql(self, resource, ctx):
        self.nosql.delete(resource, ctx)

    def create_monitor(self, spec, planned, ctx, created):
        return self.monitor.create(spec, planned, ctx, created)

    def get_monitor(self, resource):
        return self.monitor.get(resource)

    def delete_monitor(self, resource, ctx):
        self.monitor.delete(resource, ctx)

    def create_encryption_key(self, spec, planned, ctx):
        return self.kms.create(spec, planned, ctx)

    def get_encryption_key(self, resource):
        return self.kms.get(resource)

    def delete_encryption_key(self, resource, ctx):
        self.kms.delete(resource, ctx)

    def create_log_export(self, spec, planned, ctx, created):
        return self.log_export.create(spec, planned, ctx, created)

    def get_log_export(self, resource):
        return self.log_export.get(resource)

    def delete_log_export(self, resource, ctx):
        self.log_export.delete(resource, ctx)

    def create_budget(self, spec, planned, ctx):
        return self.budget.create(spec, planned, ctx)

    def get_budget(self, resource):
        return self.budget.get(resource)

    def delete_budget(self, resource, ctx):
        self.budget.delete(resource, ctx)

    def create_dns(self, spec, planned, ctx, created):
        return self.dns.create(spec, planned, ctx, created)

    def get_dns(self, resource):
        return self.dns.get(resource)

    def delete_dns(self, resource, ctx):
        self.dns.delete(resource, ctx)

    def create_identity(self, spec, planned, ctx):
        return self.identity.create(spec, planned, ctx)

    def get_identity(self, resource):
        return self.identity.get(resource)

    def delete_identity(self, resource, ctx):
        self.identity.delete(resource, ctx)

    def create_job(self, spec, planned, ctx, network, secrets):
        return self.job.create(spec, planned, ctx, network, secrets)

    def get_job(self, resource):
        return self.job.get(resource)

    def delete_job(self, resource, ctx):
        self.job.delete(resource, ctx)

    def create_service(self, spec, planned, ctx, network, secrets):
        return self.run.create(spec, planned, ctx, network, secrets)

    def get_service(self, resource):
        return self.run.get(resource)

    def delete_service(self, resource, ctx):
        self.run.delete(resource, ctx)

    def create_load_balancer(self, spec, planned, ctx, backends, network):
        return self.load_balancer.create(spec, planned, ctx, backends, network)

    def get_load_balancer(self, resource):
        return self.load_balancer.get(resource)

    def delete_load_balancer(self, resource, ctx):
        self.load_balancer.delete(resource, ctx)

    # ---- optional capabilities
    def stop_compute(self, resource, ctx):
        return self.compute.stop(resource, ctx)

    def start_compute(self, resource, ctx):
        return self.compute.start(resource, ctx)

    def stop_database(self, resource, ctx):
        return self.database.stop(resource, ctx)

    def start_database(self, resource, ctx):
        return self.database.start(resource, ctx)

    def grant(self, resource, member, level, ctx) -> list[str]:
        if resource.kind.value == "cluster":
            return self.cluster.grant(resource, member, level)  # a RoleBinding in the request's namespace, not IAM
        if resource.kind.value == "warehouse":
            return self.warehouse.grant(resource, member, level)  # the dataset's own access list
        if resource.kind.value == "compute" and resource.instance_id is None:
            resource = self.compute.get(resource)  # the IAP tunnel grant is bound to the numeric id
        return self.iam.grant(resource, member, level)

    def revoke(self, resource, member, ctx) -> None:
        if resource.kind.value == "cluster":
            self.cluster.revoke(resource, member)
            return
        if resource.kind.value == "warehouse":
            self.warehouse.revoke(resource, member)
            return
        if resource.kind.value == "compute" and resource.instance_id is None:
            resource = self.compute.get(resource)
        self.iam.revoke(resource, member)

    def put_secret_value(self, resource, value, ctx):
        return self.secret.put_value(resource, value, ctx)

    def read_cache_auth(self, resource) -> str:
        return self.cache.read_auth(resource)

    def connect_info(self, resource, resources):
        profile = self.settings.profiles.get(resource.profile) if resource.kind.value == "compute" and resource.profile else None
        return connect.info(resource, resources, self.api.project, drivers=profile is not None and profile.accelerator == "required")

    def busy_cores(self, resource, minutes: int) -> float | None:
        return self.monitoring.busy_cores(resource, minutes)

    def profile_infos(self) -> list[dict]:
        return [{"name": name, "description": profile.description, "accelerator": profile.accelerator} for name, profile in sorted(self.settings.profiles.items())]

    def resource_health(self, resource) -> Health:
        return self.monitoring.health(self.refresh(resource))

    def resource_usage(self, resource, minutes: int) -> Usage:
        return self.monitoring.usage(resource, minutes)
