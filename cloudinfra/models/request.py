"""The provider-neutral infrastructure contract: what the Application Layer asks for.

Every field is a size, a count, a name, a version or a switch. There is no repository, image, command, script, key or
provider-specific structure, so a request can describe "one Ubuntu machine with 4 CPUs and 16 GB" and nothing about any
application. Strict models: no unknown field, no type coercion.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..security import validation

OsFamily = Literal["ubuntu", "debian", "rocky"]
StorageType = Literal["persistent", "object"]
StoragePerformance = Literal["standard", "balanced", "ssd"]
DatabaseEngine = Literal["postgresql", "mysql"]
DatabaseSize = Literal["small", "medium", "large"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ComputeRequest(StrictModel):
    """Virtual machines of one shape."""
    count: int = Field(default=1, ge=1, le=100, description="How many identical machines.")
    cpu: int = Field(ge=1, le=96, description="Virtual CPUs per machine.")
    memory_gb: int = Field(ge=1, le=640, description="Memory per machine, in GB.")
    os: OsFamily = Field(default="ubuntu", description="Operating system family; the provider picks its current long-term image.")
    boot_disk_gb: int = Field(default=20, ge=10, le=2000, description="Boot disk size, in GB.")
    public_ip: bool = Field(default=False, description="Give each machine a public address. With a network block, network.public_access must be true.")
    network_id: str | None = Field(default=None, description="Place the machines in a network this service created earlier (net-…), instead of the request's own "
                                                             "network block or the operator's default network.")

    @field_validator("network_id")
    @classmethod
    def _network_id(cls, value):
        return None if value is None else validation.check_identifier(value, "compute.network_id")


class StorageRequest(StrictModel):
    """Block storage (one disk per machine, or one disk alone) or an object bucket."""
    type: StorageType = Field(default="persistent", description="persistent: block disks attached to the machines; object: a bucket.")
    size_gb: int = Field(default=100, ge=1, le=64000,
                         description="Size of each disk in GB; for a bucket, the expected capacity used only in the cost estimate.")
    performance: StoragePerformance = Field(default="balanced", description="Disk class: standard (HDD), balanced or ssd.")


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
    """What the Application Layer wants: a provider, a region, and at least one of compute, storage, network, database."""
    provider: str = Field(default="gcp", pattern=r"^[a-z][a-z0-9-]{0,15}$", description="The cloud provider adapter: gcp today; aws, azure, jetstream when their adapters exist.")
    region: str = Field(description="The provider's region (us-central1).")
    zone: str | None = Field(default=None, description="A zone in the region; the provider picks one when omitted.")
    name: str | None = Field(default=None, description="A short name used as the prefix of every resource name; infra-<8 hex> when omitted.")
    labels: dict[str, str] = Field(default_factory=dict, description="Labels applied to every resource, next to the service's own ownership labels.")
    compute: ComputeRequest | None = None
    storage: StorageRequest | None = None
    database: DatabaseRequest | None = None
    network: NetworkRequest | None = None

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
    def _something_requested(self):
        if not any((self.compute, self.storage, self.database, self.network)):
            raise ValueError("A request needs at least one of compute, storage, database or network")
        return self

    def kinds(self) -> list[str]:
        return [kind for kind in ("network", "storage", "compute", "database") if getattr(self, kind) is not None]
