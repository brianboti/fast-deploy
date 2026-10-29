"""What the service created, described without provider vocabulary beyond the provider's own identifier."""
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .errors import ErrorDetail


class ResourceKind(StrEnum):
    compute = "compute"
    storage = "storage"
    network = "network"
    database = "database"
    cache = "cache"
    queue = "queue"
    secret = "secret"  # noqa: S105 - the kind's name, not a credential
    cluster = "cluster"
    load_balancer = "load_balancer"


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


class LoadBalancerResource(Resource):
    kind: Literal[ResourceKind.load_balancer] = ResourceKind.load_balancer
    type: Literal["http", "tcp"]
    address: str | None = Field(default=None, description="The public address.")
    port: int
    backend_group: str
    backends: list[str] = Field(default_factory=list, description="The compute resource ids behind it.")
    health_check_path: str | None = None
    components: list[dict[str, Any]] = Field(default_factory=list, description="The provider pieces in creation order (kind, name, url), for teardown in reverse.")


AnyResource = Annotated[ComputeResource | StorageResource | NetworkResource | DatabaseResource | CacheResource | QueueResource | SecretResource | ClusterResource | LoadBalancerResource,
                        Field(discriminator="kind")]
RESOURCE_TYPES = {ResourceKind.compute: ComputeResource, ResourceKind.storage: StorageResource, ResourceKind.network: NetworkResource, ResourceKind.database: DatabaseResource,
                  ResourceKind.cache: CacheResource, ResourceKind.queue: QueueResource, ResourceKind.secret: SecretResource, ResourceKind.cluster: ClusterResource,
                  ResourceKind.load_balancer: LoadBalancerResource}
ID_PREFIX = {ResourceKind.compute: "cmp", ResourceKind.storage: "sto", ResourceKind.network: "net", ResourceKind.database: "db", ResourceKind.cache: "cac",
             ResourceKind.queue: "que", ResourceKind.secret: "sec", ResourceKind.cluster: "clu", ResourceKind.load_balancer: "lb"}


def load_resource(data: dict):
    return RESOURCE_TYPES[ResourceKind(data["kind"])].model_validate(data)
