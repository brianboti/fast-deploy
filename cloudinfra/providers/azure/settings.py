"""The operator's settings for the Azure adapter: the subscription, the cloud, the regions, and which of its host's
identities the service uses. No credential is ever in this model: the service takes a token from the host it runs on
(auth.py)."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
REGION = re.compile(r"^[a-z][a-z0-9]{3,30}$")  # the contract's own rule for an Azure region (security/validation.py)
ZONE = re.compile(r"^[1-9]$")


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

    def described(self) -> dict:
        """What the service's status says about this provider's account."""
        return {"subscription": self.subscription_id, "cloud": self.cloud}
