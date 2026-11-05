"""The operator's settings for the Azure adapter: the subscription, the cloud, the regions, which of its host's
identities the service uses, the software profiles machines may boot, and the shared pieces a request's network meets
(the default subnet, the Bastion, the flow-log account). No credential is ever in this model: the service takes a token
from the host it runs on (auth.py)."""
import ipaddress
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
REGION = re.compile(r"^[a-z][a-z0-9]{3,30}$")  # the contract's own rule for an Azure region (security/validation.py)
ZONE = re.compile(r"^[1-9]$")
_GROUP = r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[A-Za-z0-9._()-]{1,90}/providers"
# An image a profile names: a Marketplace image (publisher:offer:sku:version) or a Compute Gallery image or version.
IMAGE = re.compile(r"^(?:[A-Za-z0-9._-]{1,128}:[A-Za-z0-9._-]{1,128}:[A-Za-z0-9._-]{1,128}:[A-Za-z0-9._-]{1,64}"
                   rf"|{_GROUP}/Microsoft\.Compute/galleries/[A-Za-z0-9._]{{1,80}}/images/[A-Za-z0-9._-]{{1,80}}(?:/versions/[0-9.]{{1,32}})?)$")
SUBNET = re.compile(rf"^{_GROUP}/Microsoft\.Network/virtualNetworks/[A-Za-z0-9._-]{{2,64}}/subnets/[A-Za-z0-9._-]{{1,80}}$")
BASTION = re.compile(rf"^{_GROUP}/Microsoft\.Network/bastionHosts/[A-Za-z0-9._-]{{1,80}}$")
STORAGE_ACCOUNT = re.compile(rf"^{_GROUP}/Microsoft\.Storage/storageAccounts/[a-z0-9]{{3,24}}$")
REGISTRY = re.compile(rf"^{_GROUP}/Microsoft\.ContainerRegistry/registries/[A-Za-z0-9]{{5,50}}$")
PROFILE_NAME = re.compile(r"^[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$")


class AzureProfile(BaseModel):
    """A software profile: an image the platform team approves, built and pinned outside this service."""
    model_config = ConfigDict(extra="forbid", strict=True)
    image: str = Field(pattern=IMAGE.pattern, description="publisher:offer:sku:version (a Marketplace image), or a Compute Gallery image id, with or "
                       "without /versions/VERSION")
    plan: bool = Field(default=False, description="A Marketplace image sold with a plan (the publisher's own, such as Rocky Linux's): the machine names "
                       "the plan, and the operator accepts its terms once in the subscription.")
    description: str = Field(default="", max_length=200)
    secure_boot: bool = Field(default=True, description="Turn off only for images whose kernel modules are not signed.")
    min_boot_disk_gb: int = Field(default=30, ge=30, le=4095, description="The image's own size or more; smaller boot disks are raised to it.")
    accelerator: Literal["required", "optional", "none"] = Field(
        default="optional", description="required for a GPU image, which carries its drivers; optional for an image without them, which a GPU machine boots "
                                        "with Secure Boot off so that drivers can be installed; none for one that must not get GPUs.")

    @model_validator(mode="after")
    def _plan(self):
        if self.plan and self.image.startswith("/"):
            raise ValueError("plan is for a Marketplace image (publisher:offer:sku:version), not a gallery image")
        return self


class AzureSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    subscription_id: str = Field(pattern=GUID.pattern, description="The subscription the service provisions in.")
    tenant_id: str | None = Field(default=None, pattern=GUID.pattern, description="The Entra ID directory of that subscription. On a laptop the Azure CLI "
                                  "asks for tokens in it; on Azure the host's identity already belongs to it.")
    cloud: Literal["public", "usgovernment", "china"] = Field(default="public", description="Which Azure cloud: its endpoints are in clouds.py.")
    allowed_regions: list[str] = Field(default_factory=lambda: ["eastus2"])
    zones: dict[str, list[str]] = Field(default_factory=dict, description="A region's availability zones (\"1\", \"2\", \"3\"), or [] for a region without "
                                        "them, for regions the adapter's table leaves out or where this subscription differs; every region of a sovereign "
                                        "cloud is listed here.")
    identity_client_id: str | None = Field(default=None, pattern=GUID.pattern, description="The client id of the user-assigned managed identity the service "
                                           "runs as, when its host has several; the host's only identity when omitted.")
    profiles: dict[str, AzureProfile] = Field(default_factory=dict, description="Software profiles a request may name; images are the platform team's, "
                                              "never a caller's.")
    default_accelerator_profile: str | None = Field(default=None, description="The profile GPU groups get when they name none, such as an image with signed drivers.")
    default_subnet: str | None = Field(default=None, pattern=SUBNET.pattern, description="Where machines go when a request has no network block and names "
                                       "no network_id: a subnet's resource id. Azure has no default network, so without one such a request is refused.")
    bastion: str | None = Field(default=None, pattern=BASTION.pattern, description="The operator's Azure Bastion (Standard, with native client support), "
                                "through which members reach machines with Entra sign-in; named in every machine's connect block. Its network is peered "
                                "with the request networks by the platform team.")
    bastion_subnet: str | None = Field(default=None, description="The Bastion's AzureBastionSubnet range, which a request network admits on port 22 when "
                                       "network.iap_ssh is on; without it, no SSH rule is made.")
    deny_egress: bool = Field(default=False, description="Refuse egress from the networks the service makes, except within the network and to Entra "
                              "ID for sign-in; no NAT is made, and nothing on such a network reaches the internet.")
    flow_logs_storage_account: str | None = Field(default=None, pattern=STORAGE_ACCOUNT.pattern, description="A storage account of the operator's in "
                                                  "the request's region that receives the VNet flow logs of every network the service makes; none, no flow log.")
    network_watcher_group: str = Field(default="NetworkWatcherRG", pattern=r"^[A-Za-z0-9._()-]{1,90}$", description="The resource group of the "
                                       "subscription's network watchers (NetworkWatcher_<region>), where flow logs live.")
    prices_file: str | None = Field(default=None, description="A price snapshot `cloud-infra prices --write` made from Azure's Retail Prices API; the "
                                    "adapter's approximate built-in prices when none.")
    registry: str | None = Field(default=None, pattern=REGISTRY.pattern, description="The operator's container registry (Standard or Premium, its role "
                                 "assignment mode AbacRepositoryPermissions), in which a request's registry is a repository prefix of its own; none, and "
                                 "a request may hold no registry. The service needs Container Registry Repository Contributor and Catalog Lister on it.")

    @field_validator("subscription_id", "tenant_id", "identity_client_id")
    @classmethod
    def _lower(cls, value):
        return value.lower() if value else value

    @field_validator("allowed_regions")
    @classmethod
    def _regions(cls, value):
        for region in value:
            if not REGION.fullmatch(region):
                raise ValueError(f"allowed_regions: {region!r} is not an Azure region's programmatic name, such as eastus2")
        return value

    @field_validator("zones")
    @classmethod
    def _zones(cls, value):
        for region, zones in value.items():
            if not REGION.fullmatch(region):
                raise ValueError(f"zones: {region!r} is not an Azure region's programmatic name, such as eastus2")
            if any(not ZONE.fullmatch(zone) for zone in zones) or len(set(zones)) != len(zones):
                raise ValueError(f"zones.{region}: logical zone numbers such as \"1\", \"2\", \"3\", each once")
        return value

    @field_validator("bastion_subnet")
    @classmethod
    def _bastion_subnet(cls, value):
        if value is None:
            return value
        try:
            network = ipaddress.IPv4Network(value, strict=True)
        except ValueError:
            raise ValueError(f"bastion_subnet: {value!r} is not an IPv4 range such as 10.0.1.0/26") from None
        if not network.is_private:
            raise ValueError("bastion_subnet: the Bastion's subnet is a private range")
        return str(network)

    @model_validator(mode="after")
    def _profiles(self):
        for name in self.profiles:
            if not PROFILE_NAME.fullmatch(name):
                raise ValueError(f"profile name {name!r}: lowercase letters, digits and dashes, starting with a letter")
        if self.default_accelerator_profile and self.default_accelerator_profile not in self.profiles:
            raise ValueError(f"default_accelerator_profile {self.default_accelerator_profile!r} is not one of the profiles")
        if self.default_accelerator_profile and self.profiles[self.default_accelerator_profile].accelerator == "none":
            raise ValueError("default_accelerator_profile names a profile that refuses accelerators")
        if self.bastion and not self.bastion_subnet:
            raise ValueError("bastion: give bastion_subnet too, the range its machines are reached from on port 22")
        return self

    def described(self) -> dict:
        """What the service's status says about this provider's account."""
        return {"subscription": self.subscription_id, "cloud": self.cloud}
