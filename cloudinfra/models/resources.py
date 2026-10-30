"""What the service created, described without provider vocabulary beyond the provider's own identifier."""
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .errors import ErrorDetail


class ResourceKind(StrEnum):
    encryption_key = "encryption_key"
    identity = "identity"
    compute = "compute"
    storage = "storage"
    network = "network"
    database = "database"
    cache = "cache"
    queue = "queue"
    warehouse = "warehouse"
    nosql = "nosql"
    secret = "secret"  # noqa: S105 - the kind's name, not a credential
    cluster = "cluster"
    service = "service"
    job = "job"
    load_balancer = "load_balancer"
    dns = "dns"
    monitor = "monitor"
    budget = "budget"
    log_export = "log_export"


class ResourceStatus(StrEnum):
    PENDING = "PENDING"
    CREATING = "CREATING"
    READY = "READY"
    STOPPED = "STOPPED"
    UPDATING = "UPDATING"
    DELETING = "DELETING"
    DELETED = "DELETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class Grant(BaseModel):
    """One role this service granted to one member on one resource."""
    model_config = ConfigDict(extra="forbid")
    member: str
    role: str


class ConnectInfo(BaseModel):
    """How to reach a resource, written by the provider adapter from what exists; computed on every answer, never stored."""
    model_config = ConfigDict(extra="forbid")
    commands: dict[str, str] = Field(default_factory=dict, description="Commands to run, by purpose (ssh, port_forward, password, credentials).")
    prerequisites: list[str] = Field(default_factory=list, description="What must already be true, such as a project-level role the platform team grants once.")
    notes: list[str] = Field(default_factory=list)


class Resource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="The identifier this service issued (cmp-…, sto-…, net-…, db-…, cac-…, que-…, sec-…, clu-…, lb-…).")
    kind: ResourceKind
    name: str
    request_id: str
    provider: str
    provider_id: str | None = Field(default=None, description="The provider's own identifier (a self link, a connection name, a bucket name).")
    region: str
    zone: str | None = None
    status: ResourceStatus = ResourceStatus.PENDING
    labels: dict[str, str] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    error: ErrorDetail | None = None
    access: list[Grant] = Field(default_factory=list, description="Roles this service granted on the resource.")
    connect: ConnectInfo | None = Field(default=None, description="How to reach the resource.")


class ComputeResource(Resource):
    kind: Literal[ResourceKind.compute] = ResourceKind.compute
    group: str = "vm"
    machine_type: str
    cpu: int
    memory_gb: int
    os_image: str
    boot_disk_gb: int
    accelerator: str | None = Field(default=None, description="The GPUs on the machine (nvidia-l4 x1), when any.")
    internal_ip: str | None = None
    public_ip: str | None = None
    attached_disks: list[str] = Field(default_factory=list, description="Storage resource ids attached to this machine.")
    network: str | None = Field(default=None, description="The network resource (net-…) the machine is in; null in the operator's default network.")
    profile: str | None = Field(default=None, description="The software profile the machine was made from.")
    secure_boot: bool | None = None
    autostop_idle_minutes: int | None = Field(default=None, description="Idle minutes after which the service stops the machine; null never.")
    max_run_hours: int | None = None
    started_at: str | None = Field(default=None, description="When the machine last started; idle stop waits at least the idle window after it.")
    stop_reason: str | None = Field(default=None, description="Why the service stopped it: requested, expired or idle.")
    instance_id: str | None = Field(default=None, description="The provider's numeric id, which grants that outlive a name are bound to.")


class StorageResource(Resource):
    kind: Literal[ResourceKind.storage] = ResourceKind.storage
    type: Literal["persistent", "object"]
    size_gb: int
    performance: str | None = None
    attached_to: str | None = Field(default=None, description="The compute resource id a disk is attached to.")
    url: str | None = Field(default=None, description="A bucket's URL (gs://…).")
    snapshot_schedule: str | None = Field(default=None, description="The disk's daily snapshot schedule, when it has one.")
    snapshot_keep_days: int | None = None


class FirewallRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    direction: Literal["ingress", "egress"] = "ingress"
    ports: list[int]
    sources: list[str]
    provider_id: str | None = None


class NetworkResource(Resource):
    kind: Literal[ResourceKind.network] = ResourceKind.network
    subnet: str | None = None
    cidr: str | None = None
    public_access: bool = False
    firewall_rules: list[FirewallRule] = Field(default_factory=list)
    private_services_range: str | None = Field(default=None, description="The range reserved for managed services (databases) on this network.")
    nat_router: str | None = Field(default=None, description="The router carrying the network's NAT, when it has outbound internet.")


class DatabaseResource(Resource):
    kind: Literal[ResourceKind.database] = ResourceKind.database
    engine: str
    version: str
    tier: str
    storage_gb: int
    high_availability: bool = False
    public_access: bool = False
    host: str | None = None
    port: int
    database: str
    username: str
    connection_name: str | None = None
    network: str | None = Field(default=None, description="The network resource (net-…) a private database is on.")
    password_secret: str | None = Field(default=None, description="The secret resource (sec-…) that holds the generated password.")


class CacheResource(Resource):
    kind: Literal[ResourceKind.cache] = ResourceKind.cache
    engine: str = "redis"
    version: str | None = None
    tier: str
    memory_gb: int
    high_availability: bool = False
    host: str | None = None
    port: int = 6379
    network: str | None = Field(default=None, description="The network resource (net-…) the cache is on.")
    auth: str = Field(default="secret", description="Where the AUTH string is: copied by the service into the secret named by auth_secret.")
    auth_secret: str | None = Field(default=None, description="The secret resource (sec-…) that holds the AUTH string.")
    server_ca_cert: str | None = Field(default=None, description="The server certificate a TLS client verifies against; public, not a secret.")


class QueueResource(Resource):
    kind: Literal[ResourceKind.queue] = ResourceKind.queue
    topic: str = Field(description="The topic's full name at the provider.")
    subscription: str | None = Field(default=None, description="The pull subscription's full name, when one was created.")
    retention_hours: int


class WarehouseResource(Resource):
    """An analytics dataset; its tables are the Application Layer's."""
    kind: Literal[ResourceKind.warehouse] = ResourceKind.warehouse
    dataset: str = Field(description="The dataset's id at the provider.")
    location: str
    table_expiration_days: int | None = None


class NoSqlResource(Resource):
    """A document database; its documents are the Application Layer's."""
    kind: Literal[ResourceKind.nosql] = ResourceKind.nosql
    database: str = Field(description="The database's id at the provider.")
    location: str


class SecretResource(Resource):
    kind: Literal[ResourceKind.secret] = ResourceKind.secret
    resource_name: str = Field(description="The secret's full name at the provider; versions are read and written with the caller's own identity.")
    versions: int = Field(default=0, description="Versions this service wrote: 1 when a value was generated.")
    purpose: str | None = Field(default=None, description="database_password or cache_auth when the service made the secret for a request's own credential.")


class FrontDoor(BaseModel):
    """A cluster's front door: the public address, the Gateway in front of it, and the certificate for its hostnames."""
    address: str | None = Field(default=None, description="The public IP address; the operator's DNS points the hostnames at it.")
    hostnames: list[str] = Field(default_factory=list)
    gateway: str = Field(default="cloudinfra-gateway/public", description="namespace/name of the Gateway a workload's HTTPRoute names as its parent.")
    certificate: str | None = Field(default=None, description="The managed certificate's state (PROVISIONING until DNS points at the address, then ACTIVE); none for plain HTTP.")
    address_name: str
    certificate_name: str | None = None
    security_policy: str | None = Field(default=None, description="The web application firewall's policy, which each exposed workload's backend names.")


class ClusterResource(Resource):
    kind: Literal[ResourceKind.cluster] = ResourceKind.cluster
    location: str
    version: str | None = None
    endpoint: str | None = Field(default=None, description="The control plane's address; credentials come from the provider's IAM, never from this service.")
    ca_certificate: str | None = Field(default=None, description="The control plane's CA certificate (base64 PEM) a client verifies against; public, not a secret.")
    namespace: str | None = Field(default=None, description="The request's namespace in the cluster, where its members' access is granted.")
    node_count: int
    machine_type: str
    node_disk_gb: int | None = Field(default=None, description="Each node's disk, in GB; priced in estimates.")
    node_pool: str | None = None
    network: str | None = None
    node_service_account: str | None = Field(default=None, description="The account the nodes run as, made for this cluster; none on a cluster made before.")
    registries: list[str] = Field(default_factory=list, description="Artifact Registry repositories the nodes may pull images from.")
    public: FrontDoor | None = Field(default=None, description="The front door, when the request asked for one.")


class ServiceResource(Resource):
    """A managed container service: the shell this service made, and what serves on it now."""
    kind: Literal[ResourceKind.service] = ResourceKind.service
    url: str | None = Field(default=None, description="The service's address.")
    runtime_service_account: str | None = Field(default=None, description="The account its instances run as, made for it.")
    cpu: int
    memory_mb: int
    min_instances: int = 0
    max_instances: int
    ingress: str
    public: bool = False
    network: str | None = None
    secrets: list[str] = Field(default_factory=list, description="The request's secrets its runtime account may read (provider names).")
    revision: str | None = Field(default=None, description="The revision serving all traffic now, when one does.")


class IdentityResource(Resource):
    """A workload identity: an account with no key, holding the request's grants at its level."""
    kind: Literal[ResourceKind.identity] = ResourceKind.identity
    email: str
    level: str = "use"
    kubernetes_service_account: str | None = Field(default=None, description="namespace/name of the Kubernetes service account that acts as it in the "
                                                                             "request's clusters, when the request has one.")


class JobResource(Resource):
    """A managed job: the shell this service made, and when it runs by itself."""
    kind: Literal[ResourceKind.job] = ResourceKind.job
    runtime_service_account: str | None = Field(default=None, description="The account its tasks run as, made for it.")
    cpu: int
    memory_mb: int
    tasks: int = 1
    parallelism: int = 0
    max_retries: int = 3
    task_timeout_seconds: int = 600
    network: str | None = None
    secrets: list[str] = Field(default_factory=list, description="The request's secrets its runtime account may read (provider names).")
    schedule: dict[str, str] | None = Field(default=None, description="cron and time_zone, when it runs by itself.")
    scheduler: str | None = Field(default=None, description="The scheduler job that runs it (provider name).")
    scheduler_service_account: str | None = Field(default=None, description="The account the scheduler runs it as, allowed to run this job only.")


class DnsResource(Resource):
    """A public DNS zone and the record sets this service made in it."""
    kind: Literal[ResourceKind.dns] = ResourceKind.dns
    dns_name: str = Field(description="The zone's domain.")
    name_servers: list[str] = Field(default_factory=list, description="Where the operator points the domain, once, at its registrar.")
    records: list[dict[str, Any]] = Field(default_factory=list, description="name, type, ttl and values of each record set made.")


class MonitorResource(Resource):
    """Uptime checks on the request's public endpoints, an alert for each, and the addresses alerted."""
    kind: Literal[ResourceKind.monitor] = ResourceKind.monitor
    notify: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list, description="The notification channels made (provider names).")
    checks: list[dict[str, Any]] = Field(default_factory=list, description="name, url and period of each uptime check made.")
    policies: list[str] = Field(default_factory=list, description="The alert policies made (provider names).")


class BudgetResource(Resource):
    """A monthly spending budget on one request's costs, and the addresses alerted."""
    kind: Literal[ResourceKind.budget] = ResourceKind.budget
    monthly_amount: int
    thresholds: list[int] = Field(default_factory=list)
    forecast: bool = True
    notify: list[str] = Field(default_factory=list)
    watches: str = Field(description="The request whose costs it counts.")
    billing_account: str | None = None
    budget: str | None = Field(default=None, description="The budget's provider name.")
    channels: list[str] = Field(default_factory=list, description="The notification channels made (provider names).")


class EncryptionKeyResource(Resource):
    """A customer-managed key, what it encrypts, and the service agents allowed to use it."""
    kind: Literal[ResourceKind.encryption_key] = ResourceKind.encryption_key
    key_ring: str
    rotation_days: int
    destroy_after_days: int
    encrypts: list[str] = Field(default_factory=list, description="What the request encrypts with it: storage (disks), bucket, compute (boot disks), database.")
    agents: list[str] = Field(default_factory=list, description="The provider's service agents allowed to encrypt and decrypt with it.")


class LogExportResource(Resource):
    """A log sink: what it selects, where it writes, and the identity it writes as."""
    kind: Literal[ResourceKind.log_export] = ResourceKind.log_export
    to: str
    destination: str | None = Field(default=None, description="Where the provider writes, in its own form.")
    destination_name: str | None = Field(default=None, description="The bucket's or dataset's name.")
    filter: str | None = Field(default=None, description="The log filter that selects the request's entries.")
    min_severity: str | None = None
    writer_identity: str | None = Field(default=None, description="The identity the provider writes as, granted on the destination only.")


class LoadBalancerResource(Resource):
    kind: Literal[ResourceKind.load_balancer] = ResourceKind.load_balancer
    type: Literal["http", "tcp"]
    address: str | None = Field(default=None, description="The public address.")
    port: int
    backend_group: str
    backends: list[str] = Field(default_factory=list, description="The compute resource ids behind it.")
    health_check_path: str | None = None
    hostnames: list[str] = Field(default_factory=list, description="Names served over HTTPS on 443, with a provider-managed certificate.")
    certificate: str | None = Field(default=None, description="The managed certificate's state (PROVISIONING until DNS points at the address, then ACTIVE).")
    components: list[dict[str, Any]] = Field(default_factory=list, description="The provider pieces in creation order (kind, name, url), for teardown in reverse.")
    security_policy: str | None = Field(default=None, description="The web application firewall's policy on its backend, when it has one.")


AnyResource = Annotated[EncryptionKeyResource
                       | IdentityResource | ComputeResource | StorageResource | NetworkResource | DatabaseResource | CacheResource | QueueResource | WarehouseResource | NoSqlResource
                       | SecretResource | ClusterResource | ServiceResource
                       | JobResource | LoadBalancerResource | DnsResource | MonitorResource | BudgetResource | LogExportResource, Field(discriminator="kind")]
RESOURCE_TYPES = {ResourceKind.encryption_key: EncryptionKeyResource, ResourceKind.identity: IdentityResource, ResourceKind.compute: ComputeResource, ResourceKind.storage: StorageResource,
                  ResourceKind.network: NetworkResource, ResourceKind.database: DatabaseResource,
                  ResourceKind.cache: CacheResource, ResourceKind.queue: QueueResource, ResourceKind.warehouse: WarehouseResource, ResourceKind.nosql: NoSqlResource,
                  ResourceKind.secret: SecretResource, ResourceKind.cluster: ClusterResource,
                  ResourceKind.service: ServiceResource, ResourceKind.job: JobResource, ResourceKind.load_balancer: LoadBalancerResource,
                  ResourceKind.dns: DnsResource, ResourceKind.monitor: MonitorResource, ResourceKind.budget: BudgetResource, ResourceKind.log_export: LogExportResource}
ID_PREFIX = {ResourceKind.encryption_key: "key",
             ResourceKind.identity: "idn", ResourceKind.compute: "cmp", ResourceKind.storage: "sto", ResourceKind.network: "net", ResourceKind.database: "db", ResourceKind.cache: "cac",
             ResourceKind.queue: "que", ResourceKind.warehouse: "whs", ResourceKind.nosql: "nsq", ResourceKind.secret: "sec", ResourceKind.cluster: "clu", ResourceKind.service: "svc",
             ResourceKind.job: "job",
             ResourceKind.load_balancer: "lb", ResourceKind.dns: "dns", ResourceKind.monitor: "mon", ResourceKind.budget: "bud", ResourceKind.log_export: "log"}


def load_resource(data: dict):
    return RESOURCE_TYPES[ResourceKind(data["kind"])].model_validate(data)
