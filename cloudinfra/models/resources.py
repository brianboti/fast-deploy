"""What the service created, described without provider vocabulary beyond the provider's own identifier."""
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .errors import ErrorDetail


class ResourceKind(StrEnum):
    compute = "compute"
    storage = "storage"
    network = "network"
    database = "database"


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
    id: str = Field(description="The identifier this service issued (cmp-…, sto-…, net-…, db-…).")
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
    machine_type: str
    cpu: int
    memory_gb: int
    os_image: str
    boot_disk_gb: int
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


AnyResource = Annotated[ComputeResource | StorageResource | NetworkResource | DatabaseResource, Field(discriminator="kind")]
RESOURCE_TYPES = {ResourceKind.compute: ComputeResource, ResourceKind.storage: StorageResource, ResourceKind.network: NetworkResource, ResourceKind.database: DatabaseResource}
ID_PREFIX = {ResourceKind.compute: "cmp", ResourceKind.storage: "sto", ResourceKind.network: "net", ResourceKind.database: "db"}


def load_resource(data: dict) -> ComputeResource | StorageResource | NetworkResource | DatabaseResource:
    return RESOURCE_TYPES[ResourceKind(data["kind"])].model_validate(data)
