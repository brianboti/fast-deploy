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
    auth: str = Field(default="provider-issued", description="How the AUTH string is obtained: the Application Layer reads it from the provider with its own identity.")


class QueueResource(Resource):
    kind: Literal[ResourceKind.queue] = ResourceKind.queue
    topic: str = Field(description="The topic's full name at the provider.")
    subscription: str | None = Field(default=None, description="The pull subscription's full name, when one was created.")
    retention_hours: int


class SecretResource(Resource):
    kind: Literal[ResourceKind.secret] = ResourceKind.secret
    resource_name: str = Field(description="The secret's full name at the provider; versions are read and written with the caller's own identity.")
    versions: int = Field(default=0, description="Versions this service wrote: 1 when a value was generated.")


class ClusterResource(Resource):
    kind: Literal[ResourceKind.cluster] = ResourceKind.cluster
    location: str
    version: str | None = None
    endpoint: str | None = Field(default=None, description="The control plane's address; credentials come from the provider's IAM, never from this service.")
    node_count: int
    machine_type: str
    node_pool: str | None = None
    network: str | None = None


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
