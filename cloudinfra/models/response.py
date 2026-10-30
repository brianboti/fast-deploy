"""What the service answers: plans, provisioning results, status, health, usage and cost, in one shape for every provider."""
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .errors import ErrorDetail
from .request import InfrastructureRequest
from .resources import (
    BudgetResource,
    CacheResource,
    ClusterResource,
    ComputeResource,
    DatabaseResource,
    DnsResource,
    IdentityResource,
    JobResource,
    LoadBalancerResource,
    LogExportResource,
    MonitorResource,
    NetworkResource,
    NoSqlResource,
    QueueResource,
    SecretResource,
    ServiceResource,
    StorageResource,
    WarehouseResource,
)


class RequestStatus(StrEnum):
    VALIDATED = "VALIDATED"
    PLANNED = "PLANNED"
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    FAILED = "FAILED"
    DESTROYING = "DESTROYING"
    DESTROYED = "DESTROYED"
    DESTROY_FAILED = "DESTROY_FAILED"


class OperationStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperationRecord(Model):
    operation_id: str
    request_id: str
    kind: str = Field(description="provision, destroy, cleanup, or the primitive (delete_compute, resize_compute, attach_storage, configure_firewall, …).")
    resource_id: str | None = None
    status: OperationStatus
    identity: str | None = None
    started_at: str
    finished_at: str | None = None
    error: ErrorDetail | None = None
    detail: str | None = None


class Endpoints(Model):
    public_ips: list[str] = Field(default_factory=list)
    internal_ips: list[str] = Field(default_factory=list)
    database_host: str | None = None
    database_port: int | None = None
    bucket_urls: list[str] = Field(default_factory=list)
    load_balancer_address: str | None = None
    cache_host: str | None = None
    cache_port: int | None = None
    cluster_endpoint: str | None = None
    queue_topics: list[str] = Field(default_factory=list)
    secret_names: list[str] = Field(default_factory=list)


class Resources(Model):
    identity: list[IdentityResource] = Field(default_factory=list)
    network: list[NetworkResource] = Field(default_factory=list)
    storage: list[StorageResource] = Field(default_factory=list)
    compute: list[ComputeResource] = Field(default_factory=list)
    database: list[DatabaseResource] = Field(default_factory=list)
    cache: list[CacheResource] = Field(default_factory=list)
    queue: list[QueueResource] = Field(default_factory=list)
    warehouse: list[WarehouseResource] = Field(default_factory=list)
    nosql: list[NoSqlResource] = Field(default_factory=list)
    secret: list[SecretResource] = Field(default_factory=list)
    cluster: list[ClusterResource] = Field(default_factory=list)
    service: list[ServiceResource] = Field(default_factory=list)
    job: list[JobResource] = Field(default_factory=list)
    load_balancer: list[LoadBalancerResource] = Field(default_factory=list)
    dns: list[DnsResource] = Field(default_factory=list)
    monitor: list[MonitorResource] = Field(default_factory=list)
    budget: list[BudgetResource] = Field(default_factory=list)
    log_export: list[LogExportResource] = Field(default_factory=list)

    def all(self) -> list:
        return [*self.network, *self.storage, *self.compute, *self.database, *self.cache, *self.queue, *self.secret, *self.cluster, *self.load_balancer]


class Credentials(Model):
    """Where a request's generated credentials are: secrets the request owns, readable by its access members. The password
    itself appears only if the operator turns that on, once, in the response of the call that created the database."""
    database_password_secret: str | None = Field(default=None, description="The secret resource (sec-…) holding the database password.")
    cache_auth_secret: str | None = Field(default=None, description="The secret resource (sec-…) holding the cache's AUTH string.")
    database_password: str | None = Field(default=None, description="Only when the operator sets return_database_password; never stored or logged by this service.")
    delivered: str = "through the secrets named here, which the request's access members can read; see each secret's connect block"


class CostLine(Model):
    resource: str
    kind: str
    monthly_usd: float
    basis: str
    stopped_usd: float | None = Field(default=None, description="What it costs a month while stopped; null where the kind cannot stop.")


class CostEstimate(Model):
    monthly_usd: float
    stopped_monthly_usd: float | None = Field(default=None, description="What the request costs a month while its machines and databases are stopped.")
    currency: str = "USD"
    lines: list[CostLine] = Field(default_factory=list)
    source: str
    verified: str | None = Field(default=None, description="When the list prices were last checked against the provider's pricing pages; null means approximate.")
    note: str


class PlannedResource(Model):
    kind: str
    name: str
    index: int = 0
    provider_mapping: str = Field(description="The provider operation this becomes (compute.instances.insert).")
    spec: dict[str, Any] = Field(default_factory=dict, description="The provider-specific shape chosen, for review before provisioning.")
    monthly_usd: float | None = None


class PlanResponse(Model):
    request_id: str
    name: str
    provider: str
    region: str
    zone: str | None
    status: RequestStatus = RequestStatus.PLANNED
    planned: list[PlannedResource]
    estimate: CostEstimate
    warnings: list[str] = Field(default_factory=list)
    quota: dict[str, Any] | None = Field(default=None, description="Headroom the provider reported for the region, when it was asked.")


class ValidationResponse(Model):
    valid: bool
    provider: str | None = None
    region: str | None = None
    request: InfrastructureRequest | None = None
    problems: list[ErrorDetail] = Field(default_factory=list)


class AccessGrant(Model):
    """One member of a request's access list and what became of its grants."""
    member: str
    level: Literal["use", "admin"]
    status: Literal["PENDING", "GRANTED", "PARTIAL", "FAILED", "REVOKED", "RECORDED"] = Field(
        description="RECORDED: nothing in the request has an access policy to grant on, such as a request of disks only.")
    resources: list[str] = Field(default_factory=list, description="The resources granted on.")
    error: ErrorDetail | None = None
    updated_at: str | None = None


class Lifecycle(Model):
    """Time on a request: when it expires, why it is stopped, and what keeps billing while it is."""
    expires_at: str | None = None
    on_expiry: Literal["stop", "destroy"] | None = None
    expires_in_hours: float | None = None
    max_expires_at: str | None = Field(default=None, description="The latest an extension can reach, from the operator's maximum lifetime.")
    stopped_at: str | None = None
    stop_reason: str | None = Field(default=None, description="requested, expired or idle.")
    retain_until: str | None = Field(default=None, description="For a request stopped at expiry: when it is destroyed.")
    awake_until: str | None = Field(default=None, description="Idle stop is suspended until then.")
    warnings_sent: list[str] = Field(default_factory=list)
    still_billing: list[str] = Field(default_factory=list, description="While stopped: what keeps costing money.")


class InfrastructureResponse(Model):
    request_id: str
    name: str
    provider: str
    region: str
    zone: str | None = None
    status: RequestStatus
    owner: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    resources: Resources = Field(default_factory=Resources)
    endpoints: Endpoints = Field(default_factory=Endpoints)
    credentials: Credentials | None = None
    lifecycle: Lifecycle | None = None
    access: list[AccessGrant] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    operations: list[OperationRecord] = Field(default_factory=list)
    estimated_monthly_cost_usd: float | None = None
    error: ErrorDetail | None = None
    created_at: str
    updated_at: str


class Health(Model):
    resource_id: str
    kind: str
    status: str
    healthy: bool | None = Field(default=None, description="null when the provider's state does not answer the question.")
    detail: dict[str, Any] = Field(default_factory=dict)


class Usage(Model):
    resource_id: str
    kind: str
    window_minutes: int
    metrics: dict[str, float | None] = Field(default_factory=dict, description="Utilisation as fractions of 1; null where nothing was recorded.")
    note: str


class ResourceResponse(Model):
    """One resource after a primitive call: its state now, and the operation that changes it when one is still running."""
    status: str
    request_id: str
    resource: (IdentityResource | ComputeResource | StorageResource | NetworkResource | DatabaseResource | CacheResource | QueueResource | WarehouseResource | NoSqlResource | SecretResource
               | ClusterResource | ServiceResource
               | JobResource | LoadBalancerResource | DnsResource
               | MonitorResource | BudgetResource | LogExportResource) = Field(discriminator="kind")
    operation: OperationRecord | None = None


class ListResponse(Model):
    requests: list[InfrastructureResponse]
    count: int


class KindField(Model):
    name: str
    type: str
    required: bool = False
    default: Any = None
    description: str | None = None


class ResourceKindInfo(Model):
    kind: str
    description: str
    several_per_request: bool = Field(description="Whether the block may be a list.")
    fields: list[KindField]
    providers: list[str] = Field(description="The configured providers whose adapters implement this kind.")


class ProfileInfo(Model):
    name: str
    description: str = ""
    accelerator: Literal["required", "optional", "none"] = "optional"


class ResourceKindsResponse(Model):
    kinds: list[ResourceKindInfo]
    providers: list[str]
    profiles: dict[str, list[ProfileInfo]] = Field(default_factory=dict, description="The software profiles each configured provider offers for compute.")
    note: str
