"""The provider-neutral infrastructure contract: what the Application Layer asks for.

Every field is a size, a count, a name, a version or a switch. There is no repository, image, command, script, key or
provider-specific structure, so a request can describe "one Ubuntu machine with 4 CPUs and 16 GB" and nothing about any
application. Strict models: no unknown field, no type coercion. Nine kinds: network, storage, compute (one group or
several), database, cache, queue, secret, cluster and load_balancer.
"""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..security import validation
from .errors import ErrorCode, InfrastructureError

OsFamily = Literal["ubuntu", "debian", "rocky"]
StorageType = Literal["persistent", "object"]
StoragePerformance = Literal["standard", "balanced", "ssd"]
DatabaseEngine = Literal["postgresql", "mysql"]
DatabaseSize = Literal["small", "medium", "large"]
AcceleratorType = Literal["t4", "l4", "a100", "h100"]
AccessLevel = Literal["use", "admin"]
PROFILE = r"^[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$"  # a software profile's name, defined by the operator
GPU_MIN_BOOT_DISK_GB = 40  # drivers and a CUDA toolkit; Google's GPU guide specifies at least 40 GiB
GPU_DEFAULT_BOOT_DISK_GB = 50
RESERVED_SECRETS = {"db-password": "database", "cache-auth": "cache"}  # secrets the service makes for a request's own credentials
LoadBalancerType = Literal["http", "tcp"]
GROUP = r"^[a-z](?:[a-z0-9-]{0,10}[a-z0-9])?$"  # a group's, queue's or secret's short name: up to 12 characters, part of resource names
# The kinds, in the order they are created; destruction is the reverse. Every top-level block of a request is one of these.
RESOURCE_KINDS = ("network", "storage", "compute", "database", "cache", "queue", "secret", "cluster", "load_balancer")
MAX_QUEUES = 8
MAX_SECRETS = 16
MAX_CLUSTERS = 4
HOSTNAME = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
REGISTRY = re.compile(r"^[a-z0-9-]+-docker\.pkg\.dev/[a-z][a-z0-9-]{4,28}[a-z0-9]/[a-z0-9][a-z0-9._-]{0,62}$")  # an Artifact Registry repository


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AcceleratorRequest(StrictModel):
    """GPUs on each machine of a group. The driver and the toolkit are the Application Layer's to install."""
    type: AcceleratorType = Field(description="t4, l4, a100 or h100.")
    count: int = Field(default=1, ge=1, le=8, description="GPUs per machine; the provider offers fixed counts per type.")


class AutostopRequest(StrictModel):
    """Stop a machine that has been idle: its busiest CPU sample in the window used fewer cores than the operator's threshold."""
    idle_minutes: int = Field(ge=15, le=1440, description="The idle window, in minutes; the machine stops after this long without work.")


class LifetimeRequest(StrictModel):
    """How long a request lives. Warnings come 48 and 2 hours before the end; at the end it stops (keeping its data for the
    operator's retention period, then destroyed) or is destroyed."""
    hours: int = Field(ge=1, le=8760, description="Hours from creation; it can be extended up to the operator's maximum.")
    on_expiry: Literal["stop", "destroy"] = Field(default="stop", description="stop (default) keeps disks and databases for the retention period; destroy deletes everything.")


class AccessRequest(StrictModel):
    """Someone to grant access to what the request creates: login on machines, reading secrets, using buckets and queues."""
    member: str = Field(description="user:, group: or serviceAccount: and an email address in a domain the operator allows.")
    level: AccessLevel = Field(default="use", description="use (log in, read and write data) or admin (administrator login, manage secret versions and objects).")

    @field_validator("member")
    @classmethod
    def _member(cls, value):
        return validation.check_member(value)


class ComputeRequest(StrictModel):
    """Virtual machines of one shape: a group. A request may carry one group or a list of them."""
    group: str | None = Field(default=None, pattern=GROUP, description="The group's name, part of every machine name (<name>-<group>-<n>); vm when omitted. "
                                                                        "Required and unique when a request has several groups.")
    count: int = Field(default=1, ge=1, le=100, description="How many identical machines.")
    cpu: int = Field(ge=1, le=224, description="Virtual CPUs per machine.")
    memory_gb: int = Field(ge=1, le=1872, description="Memory per machine, in GB.")
    os: OsFamily | None = Field(default=None, description="Operating system family (ubuntu, debian, rocky); the provider picks its current long-term image. "
                                                          "Ubuntu when neither os nor profile is given.")
    profile: str | None = Field(default=None, pattern=PROFILE, description="A software profile the operator defines, such as a hardened base image or a GPU image "
                                                                          "with drivers; list_resource_kinds names them. Use it instead of os.")
    boot_disk_gb: int = Field(default=20, ge=10, le=2000, description="Boot disk size, in GB. Machines with GPUs default to 50 and need at least 40.")
    public_ip: bool = Field(default=False, description="Give each machine a public address. With a network block, network.public_access must be true.")
    network_id: str | None = Field(default=None, description="Place the machines in a network this service created earlier (net-…), instead of the request's own "
                                                             "network block or the operator's default network.")
    accelerator: AcceleratorRequest | None = Field(default=None, description="GPUs per machine; the machine family follows the GPU type.")
    autostop: AutostopRequest | None = Field(default=None, description="Stop machines that have been idle this long; the operator may set a default.")
    max_run_hours: int | None = Field(default=None, ge=1, le=2880, description="Stop each machine after it has run this many hours since its last start (at most 120 days).")

    @field_validator("network_id")
    @classmethod
    def _network_id(cls, value):
        return None if value is None else validation.check_identifier(value, "compute.network_id")

    @model_validator(mode="after")
    def _software_and_disk(self):
        if self.profile is not None and self.os is not None:
            raise InfrastructureError(ErrorCode.INVALID_COMPUTE_CONFIGURATION, "Give profile or os, not both: a profile names its own image",
                                      {"field": "compute.profile"})
        if self.accelerator is not None:
            if "boot_disk_gb" not in self.model_fields_set:
                self.boot_disk_gb = GPU_DEFAULT_BOOT_DISK_GB
                self.__pydantic_fields_set__.discard("boot_disk_gb")  # a default, not a size the caller asked for
            elif self.boot_disk_gb < GPU_MIN_BOOT_DISK_GB:
                raise InfrastructureError(ErrorCode.INVALID_COMPUTE_CONFIGURATION,
                                          f"A machine with GPUs needs a boot disk of at least {GPU_MIN_BOOT_DISK_GB} GB for its drivers and toolkit; "
                                          f"{self.boot_disk_gb} asked", {"field": "compute.boot_disk_gb"})
        return self

    @property
    def group_name(self) -> str:
        return self.group or "vm"

    @property
    def os_family(self) -> str:
        return self.os or "ubuntu"


class StorageRequest(StrictModel):
    """Block storage (one disk per machine, or one disk alone) or an object bucket."""
    type: StorageType = Field(default="persistent", description="persistent: block disks attached to the machines; object: a bucket.")
    size_gb: int = Field(default=100, ge=1, le=64000,
                         description="Size of each disk in GB; for a bucket, the expected capacity used only in the cost estimate.")
    performance: StoragePerformance = Field(default="balanced", description="Disk class: standard (HDD), balanced or ssd.")
    group: str | None = Field(default=None, pattern=GROUP, description="Attach one disk to each machine of this compute group only; every machine when omitted.")


class DatabaseRequest(StrictModel):
    """One managed relational database instance with one database and one user."""
    engine: DatabaseEngine = Field(default="postgresql")
    version: str | None = Field(default=None, pattern=r"^[0-9]{1,2}(?:\.[0-9]{1,2})?$", description="Major version (16, 8.0); the provider's default when omitted.")
    size: DatabaseSize = Field(default="small", description="small, medium or large; the provider maps the tier.")
    storage_gb: int = Field(default=10, ge=10, le=10000)
    high_availability: bool = Field(default=False, description="A standby in a second zone.")
    public_access: bool = Field(default=False, description="false (default): a private address on the request's network, which must then be present.")
    database_name: str = Field(default="app", pattern=r"^[a-z][a-z0-9_]{0,62}$")
    username: str = Field(default="app", pattern=r"^[a-z][a-z0-9_]{0,31}$")
    network_id: str | None = Field(default=None, description="For a private database: a network this service created earlier (net-…), instead of a network block in the same request.")

    @field_validator("network_id")
    @classmethod
    def _network_id(cls, value):
        return None if value is None else validation.check_identifier(value, "database.network_id")


class CacheRequest(StrictModel):
    """A managed in-memory cache on the request's network; always private. The provider issues its AUTH string, which the
    Application Layer reads with its own identity."""
    engine: Literal["redis"] = "redis"
    memory_gb: int = Field(default=1, ge=1, le=300)
    high_availability: bool = Field(default=False, description="A replica in a second zone with automatic failover.")
    version: str | None = Field(default=None, pattern=r"^[0-9]+\.[0-9]+$", description="Engine version (7.2); the provider's default when omitted.")
    network_id: str | None = Field(default=None, description="A network this service created earlier (net-…), instead of a network block in the same request.")

    @field_validator("network_id")
    @classmethod
    def _network_id(cls, value):
        return None if value is None else validation.check_identifier(value, "cache.network_id")


class QueueRequest(StrictModel):
    """A message topic with, by default, one pull subscription."""
    name: str = Field(pattern=GROUP, description="The queue's short name, part of the topic's name (<name>-<queue>).")
    retention_hours: int = Field(default=168, ge=1, le=744, description="How long the subscription keeps unacknowledged messages (default 7 days).")
    subscription: bool = Field(default=True, description="Create one pull subscription of the same name.")
    ack_deadline_seconds: int = Field(default=20, ge=10, le=600)


class SecretRequest(StrictModel):
    """A secret container. Values never pass through this service unless it generates one itself."""
    name: str = Field(pattern=GROUP, description="The secret's short name, part of its resource name (<name>-<secret>).")
    generate_value: bool = Field(default=False, description="Write a generated random value as the first version; the value is never returned or logged.")


class PublicRequest(StrictModel):
    """A front door for the cluster's workloads, made and owned by this service: a reserved public address and one
    Gateway in front of it that accepts routes from the request's namespace only. With hostnames it serves HTTPS with a
    Google-managed certificate for them and redirects HTTP; without, plain HTTP on the address."""
    hostnames: list[str] = Field(default_factory=list, max_length=5, description="DNS names the operator points at the address, in domains the operator allows; "
                                                                                "the certificate covers them. Lowercase, no wildcard.")

    @field_validator("hostnames")
    @classmethod
    def _hostnames(cls, value):
        for hostname in value:
            if not HOSTNAME.fullmatch(hostname):
                raise ValueError(f"cluster.public.hostnames: {hostname!r} is not a lowercase DNS name (no wildcard)")
        if len(set(value)) != len(value):
            raise ValueError("cluster.public.hostnames: each name once")
        return value


class ClusterRequest(StrictModel):
    """A managed Kubernetes cluster with one node pool of identical nodes, and one namespace named after the request where
    the request's members are granted access (use: view, admin: edit). Fetching credentials needs the provider's
    project-level cluster viewer role; what runs in the cluster is the Application Layer's. A request may carry one
    cluster or a list of them."""
    name: str | None = Field(default=None, pattern=GROUP, description="The cluster's name, part of its provider name (<request>-<name>); cluster when omitted. "
                                                                      "Required and unique when a request has several clusters.")
    node_count: int = Field(default=1, ge=1, le=100, description="Nodes in the pool.")
    node_cpu: int = Field(default=2, ge=1, le=96, description="Virtual CPUs per node.")
    node_memory_gb: int = Field(default=8, ge=1, le=640, description="Memory per node, in GB.")
    node_disk_gb: int = Field(default=50, ge=10, le=2000)
    kubernetes_version: str | None = Field(default=None, pattern=r"^[0-9]+\.[0-9]+(?:\.[0-9]+(?:-[a-z]+\.[0-9]+)?)?$",
                                           description="A version the provider offers (1.31); the provider's default channel when omitted.")
    private_nodes: bool = Field(default=False, description="Nodes without public addresses (the Application Layer then needs the provider's NAT for egress).")
    network_id: str | None = Field(default=None, description="A network this service created earlier (net-…); the operator's default network when omitted.")
    public: PublicRequest | None = Field(default=None, description="A front door: a public address and a Gateway for the request's workloads.")
    registries: list[str] = Field(default_factory=list, max_length=10, description="Artifact Registry repositories (LOCATION-docker.pkg.dev/PROJECT/REPO) the nodes "
                                                                                 "may pull images from; the operator lists the ones a request may name.")

    @field_validator("network_id")
    @classmethod
    def _network_id(cls, value):
        return None if value is None else validation.check_identifier(value, "cluster.network_id")

    @field_validator("registries")
    @classmethod
    def _registries(cls, value):
        for registry in value:
            if not REGISTRY.fullmatch(registry):
                raise ValueError(f"cluster.registries: {registry!r} is not LOCATION-docker.pkg.dev/PROJECT/REPO")
        if len(set(value)) != len(value):
            raise ValueError("cluster.registries: each repository once")
        return value

    @property
    def cluster_name(self) -> str:
        return self.name or "cluster"


class LoadBalancerRequest(StrictModel):
    """A public load balancer in front of one compute group."""
    type: LoadBalancerType = Field(default="http", description="http: an application load balancer with a health check on a path; tcp: a passthrough network load balancer.")
    port: int = Field(default=80, ge=1, le=65535, description="The port the machines serve on; the public port is the same.")
    backend_group: str | None = Field(default=None, pattern=GROUP, description="The compute group behind it; the request's only group when omitted.")
    health_check_path: str = Field(default="/", pattern=r"^/[A-Za-z0-9/_.-]*$", max_length=200, description="The path an http health check requests.")
    backends: list[str] | None = Field(default=None, description="Machines this service created earlier (cmp-…), instead of a compute group of the same request.")

    @field_validator("backends")
    @classmethod
    def _backends(cls, value):
        if value is not None:
            if not 1 <= len(value) <= 100:
                raise ValueError("load_balancer.backends takes 1 to 100 machine ids")
            for item in value:
                validation.check_identifier(item, "load_balancer.backends")
        return value


class NetworkRequest(StrictModel):
    """A private network with one subnet in the request's region and the firewall rules the switches imply."""
    public_access: bool = Field(default=False, description="Allow public addresses and open allowed_ports from allowed_sources.")
    cidr: str = Field(default="10.10.0.0/24", description="The subnet's private IPv4 range.")
    allowed_ports: list[int] = Field(default_factory=lambda: [80, 443], description="Ports opened from allowed_sources when public_access is true.")
    allowed_sources: list[str] = Field(default_factory=lambda: ["0.0.0.0/0"], description="IPv4 ranges allowed to reach allowed_ports.")
    iap_ssh: bool = Field(default=True, description="Allow SSH on port 22 from the provider's identity-aware proxy range only; never from the internet.")

    @field_validator("cidr")
    @classmethod
    def _cidr(cls, value):
        return validation.check_cidr(value, private=True)

    @field_validator("allowed_ports")
    @classmethod
    def _ports(cls, value):
        return validation.check_ports(value)

    @field_validator("allowed_sources")
    @classmethod
    def _sources(cls, value):
        return validation.check_sources(value)


class InfrastructureRequest(StrictModel):
    """What the Application Layer wants: a provider, a region, and at least one resource block."""
    provider: str = Field(default="gcp", pattern=r"^[a-z][a-z0-9-]{0,15}$", description="The cloud provider adapter: gcp today; aws, azure, jetstream when their adapters exist.")
    region: str = Field(description="The provider's region (us-central1).")
    zone: str | None = Field(default=None, description="A zone in the region; the provider picks one when omitted.")
    name: str | None = Field(default=None, description="A short name used as the prefix of every resource name; infra-<8 hex> when omitted.")
    labels: dict[str, str] = Field(default_factory=dict, description="Labels applied to every resource, next to the service's own ownership labels.")
    compute: ComputeRequest | list[ComputeRequest] | None = Field(default=None, description="One group of identical machines, or a list of groups.")
    storage: StorageRequest | None = None
    database: DatabaseRequest | None = None
    network: NetworkRequest | None = None
    cache: CacheRequest | None = None
    queue: QueueRequest | list[QueueRequest] | None = Field(default=None, description="One topic, or a list of them.")
    secret: SecretRequest | list[SecretRequest] | None = Field(default=None, description="One secret container, or a list of them.")
    cluster: ClusterRequest | list[ClusterRequest] | None = Field(default=None, description="One cluster, or a list of them.")
    load_balancer: LoadBalancerRequest | None = None
    lifetime: LifetimeRequest | None = Field(default=None, description="How long the request lives; the operator's default applies when omitted, and there is none unless the operator sets one.")
    access: list[AccessRequest] = Field(default_factory=list, max_length=20, description="People, groups or service accounts to grant access to what is created; "
                                                                                          "the requester is added when the operator maps it to an identity.")

    @field_validator("region")
    @classmethod
    def _region(cls, value):
        return validation.check_region(value)

    @field_validator("zone")
    @classmethod
    def _zone(cls, value):
        if value is not None and not validation.ZONE.fullmatch(value):
            raise ValueError("zone must be lowercase letters, digits and dashes")
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value):
        return None if value is None else validation.check_name(value, "name")

    @field_validator("labels")
    @classmethod
    def _labels(cls, value):
        return validation.check_labels(value)

    @model_validator(mode="after")
    def _consistent(self):
        for kind in ("compute", "queue", "secret", "cluster"):
            if getattr(self, kind) == []:
                raise ValueError(f"{kind}: a list cannot be empty")
        if not any(getattr(self, kind) for kind in RESOURCE_KINDS):
            raise ValueError("A request needs at least one of " + ", ".join(RESOURCE_KINDS))
        members = [item.member for item in self.access]
        if len(set(members)) != len(members):
            raise InfrastructureError(ErrorCode.INVALID_ACCESS, "access: each member once", {"field": "access"})
        for secret in self.secrets():
            owner = RESERVED_SECRETS.get(secret.name)
            if owner and getattr(self, owner) is not None:
                raise ValueError(f"secret: {secret.name} is the name of the secret the service makes for the request's {owner}; choose another")
        groups = self.compute_groups()
        if len(groups) > 1:
            names = [group.group for group in groups]
            if any(name is None for name in names):
                raise ValueError("compute: every group of a list needs a group name")
            if len(set(names)) != len(names):
                raise ValueError("compute: group names must be unique")
        clusters = self.clusters()
        if len(clusters) > MAX_CLUSTERS:
            raise ValueError(f"cluster: at most {MAX_CLUSTERS} per request")
        if len(clusters) > 1:
            names = [cluster.name for cluster in clusters]
            if any(name is None for name in names):
                raise ValueError("cluster: every cluster of a list needs a name")
            if len(set(names)) != len(names):
                raise ValueError("cluster: names must be unique")
        for kind, items, limit in (("queue", self.queues(), MAX_QUEUES), ("secret", self.secrets(), MAX_SECRETS)):
            names = [item.name for item in items]
            if len(names) > limit:
                raise ValueError(f"{kind}: at most {limit} per request")
            if len(set(names)) != len(names):
                raise ValueError(f"{kind}: names must be unique")
        if self.storage and self.storage.group and self.storage.group not in {group.group_name for group in groups}:
            raise ValueError(f"storage.group names {self.storage.group!r}, which is not a compute group of this request")
        if self.load_balancer and self.load_balancer.backends:
            if groups or self.load_balancer.backend_group:
                raise ValueError("load_balancer.backends names existing machines; a compute group in the same request is one or the other")
        elif self.load_balancer:
            if not groups:
                raise ValueError("load_balancer needs a compute group in the same request to balance, or backends naming machines created earlier")
            if self.load_balancer.backend_group is None and len(groups) > 1:
                raise ValueError("load_balancer.backend_group must name the group when the request has several")
            if self.load_balancer.backend_group and self.load_balancer.backend_group not in {group.group_name for group in groups}:
                raise ValueError(f"load_balancer.backend_group names {self.load_balancer.backend_group!r}, which is not a compute group of this request")
        return self

    def kinds(self) -> list[str]:
        return [kind for kind in RESOURCE_KINDS if getattr(self, kind)]

    def compute_groups(self) -> list[ComputeRequest]:
        return [] if self.compute is None else (self.compute if isinstance(self.compute, list) else [self.compute])

    def queues(self) -> list[QueueRequest]:
        return [] if self.queue is None else (self.queue if isinstance(self.queue, list) else [self.queue])

    def secrets(self) -> list[SecretRequest]:
        return [] if self.secret is None else (self.secret if isinstance(self.secret, list) else [self.secret])

    def clusters(self) -> list[ClusterRequest]:
        return [] if self.cluster is None else (self.cluster if isinstance(self.cluster, list) else [self.cluster])

    def cluster_named(self, name: str | None) -> ClusterRequest | None:
        clusters = self.clusters()
        if name is None:  # a plan made before clusters had names
            return clusters[0] if len(clusters) == 1 else None
        return next((cluster for cluster in clusters if cluster.cluster_name == name), None)

    def group(self, name: str | None) -> ComputeRequest | None:
        groups = self.compute_groups()
        if name is None:
            return groups[0] if len(groups) == 1 else None
        return next((group for group in groups if group.group_name == name), None)
