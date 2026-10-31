"""The operator's settings for the Google Cloud adapter: the project, the regions, where machines go by default, and how the
adapter finds its credential on a laptop. No credential is ever in this model."""
import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
SERVICE_ACCOUNT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$")


# An image the operator names for a profile: a family (the newest image in it) or one pinned image.
IMAGE = re.compile(r"^projects/[a-z][a-z0-9-]{4,61}[a-z0-9]/global/images/(?:family/)?[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?$")
PROFILE_NAME = re.compile(r"^[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$")
KMS_KEY = re.compile(r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/([a-z0-9-]+)/keyRings/[A-Za-z0-9_-]{1,63}/cryptoKeys/[A-Za-z0-9_-]{1,63}$")
POLICY_NAME = re.compile(r"^[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?$")


class GcpProfile(BaseModel):
    """A software profile: an image the platform team approves, built and pinned outside this service."""
    model_config = ConfigDict(extra="forbid", strict=True)
    image: str = Field(pattern=IMAGE.pattern, description="projects/PROJECT/global/images/family/FAMILY, or a pinned image name")
    description: str = Field(default="", max_length=200)
    secure_boot: bool = Field(default=True, description="Turn off only for images whose kernel modules are not signed.")
    min_boot_disk_gb: int = Field(default=10, ge=10, le=2000, description="The image's own size or more; smaller boot disks are raised to it.")
    accelerator: Literal["required", "optional", "none"] = Field(
        default="optional", description="required for a GPU image, which carries its drivers; optional for an image without them, which a GPU machine boots "
                                        "with Secure Boot off so that drivers can be installed; none for one that must not get GPUs.")


class GcpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project_id: str = Field(pattern=PROJECT.pattern)
    allowed_regions: list[str] = Field(default_factory=lambda: ["us-central1"])
    # Where compute goes when a request has no network block: an existing network and, for a custom-mode network, its subnet.
    default_network: str = "default"
    default_subnet: str | None = None
    # On a laptop: the service account to impersonate for every provider call, so the operator's own identity is never used
    # directly. On a Google Cloud machine the attached service account is used and this is ignored.
    impersonate_service_account: str | None = Field(default=None, pattern=SERVICE_ACCOUNT.pattern)
    # On a laptop with a person's sign-in: the project Google counts and bills the API calls against (X-Goog-User-Project).
    # Without one, Google counts them against gcloud's own shared project, whose per-minute limits are soon reached (a
    # grant failed that way on 2026-10-29). Needs serviceusage.services.use on that project; a service account needs none.
    quota_project: str | None = Field(default=None, pattern=PROJECT.pattern)
    # Zones for regions the adapter does not know; the adapter's own table covers the common ones.
    zones: dict[str, list[str]] = Field(default_factory=dict)
    # Software profiles a request may name; images are the platform team's, never a caller's.
    profiles: dict[str, GcpProfile] = Field(default_factory=dict)
    # The profile GPU groups get when they name none, such as an image with signed drivers.
    default_accelerator_profile: str | None = None
    # A cluster's nodes run as an account made for it (docs/DELIVERY-SPEC.md, phase 3); off, they run as the project's
    # Compute Engine default account, as before, and a request may name no registries.
    cluster_node_accounts: bool = True
    # The Artifact Registry repositories (LOCATION-docker.pkg.dev/PROJECT/REPO) a request may let its clusters pull from.
    registries: list[str] = Field(default_factory=list)
    # DNS domains a cluster's front door may serve (a hostname is the domain or ends in .domain); none, and a front door
    # serves plain HTTP on its address only.
    public_domains: list[str] = Field(default_factory=list)
    # Whether a request may make a managed container service callable without signing in (Cloud Run's invoker check off).
    allow_public_services: bool = False
    # The defaults the CIS benchmarks and Google's hardening guides expect (security assessment of 2026-10-30, release S2).
    # Flow logs on every subnet and logging on every firewall rule the service makes; billed by volume in Cloud Logging.
    network_logs: bool = True
    # Refuse egress from the networks the service makes, except to private ranges and to Google's APIs at their private
    # addresses (private.googleapis.com, through a DNS response policy); nothing on such a network reaches the internet.
    deny_egress: bool = False
    # The ranges that may reach a cluster's control plane (authorized networks); empty leaves the endpoint open to any
    # address, still behind Google's IAM. List where cloudinfra and appdelivery run, or they cannot reach their clusters.
    cluster_authorized_networks: list[str] = Field(default_factory=list, max_length=50)
    # Binary Authorization on clusters (enforced) and Cloud Run (the project's default policy): the project's policy, the
    # operator's, decides which images may run, such as only those built and attested in the trusted project.
    binary_authorization: bool = True
    # Customer-managed encryption keys (Cloud KMS) by region, for what requests in it make: disks, databases, buckets,
    # secrets, registries, and a cluster's node disks and Kubernetes Secrets. Each service's own agent needs
    # roles/cloudkms.cryptoKeyEncrypterDecrypter on the key (docs/SETUP.md).
    kms_keys: dict[str, str] = Field(default_factory=dict)
    # A Cloud Armor policy the operator made, attached to every application load balancer the service makes; one place
    # for the WAF rules and rate limits a security team keeps.
    cloud_armor_policy: str | None = Field(default=None, pattern=POLICY_NAME.pattern)

    @field_validator("cluster_authorized_networks")
    @classmethod
    def _authorized(cls, value):
        for cidr in value:
            try:
                if "/" not in cidr:
                    raise ValueError(cidr)
                ipaddress.IPv4Network(cidr, strict=True)
            except ValueError:
                raise ValueError(f"cluster_authorized_networks: {cidr!r} is not an IPv4 range such as 203.0.113.0/24") from None
        return value

    @field_validator("kms_keys")
    @classmethod
    def _keys(cls, value):
        for region, key in value.items():
            found = KMS_KEY.fullmatch(key)
            if not found:
                raise ValueError(f"kms_keys.{region}: {key!r} is not projects/PROJECT/locations/REGION/keyRings/RING/cryptoKeys/KEY")
            if found.group(1) != region:
                raise ValueError(f"kms_keys.{region}: the key is in {found.group(1)}; a key encrypts only resources in its own region")
        return value

    @model_validator(mode="after")
    def _profiles(self):
        for name in self.profiles:
            if not PROFILE_NAME.fullmatch(name):
                raise ValueError(f"profile name {name!r}: lowercase letters, digits and dashes, starting with a letter")
        if self.default_accelerator_profile and self.default_accelerator_profile not in self.profiles:
            raise ValueError(f"default_accelerator_profile {self.default_accelerator_profile!r} is not one of the profiles")
        if self.default_accelerator_profile and self.profiles[self.default_accelerator_profile].accelerator == "none":
            raise ValueError("default_accelerator_profile names a profile that refuses accelerators")
        return self
