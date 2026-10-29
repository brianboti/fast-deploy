"""The operator's settings for the Google Cloud adapter: the project, the regions, where machines go by default, and how the
adapter finds its credential on a laptop. No credential is ever in this model."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
SERVICE_ACCOUNT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$")


# An image the operator names for a profile: a family (the newest image in it) or one pinned image.
IMAGE = re.compile(r"^projects/[a-z][a-z0-9-]{4,61}[a-z0-9]/global/images/(?:family/)?[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?$")
PROFILE_NAME = re.compile(r"^[a-z](?:[a-z0-9-]{0,30}[a-z0-9])?$")


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
    # Zones for regions the adapter does not know; the adapter's own table covers the common ones.
    zones: dict[str, list[str]] = Field(default_factory=dict)
    # Software profiles a request may name; images are the platform team's, never a caller's.
    profiles: dict[str, GcpProfile] = Field(default_factory=dict)
    # The profile GPU groups get when they name none, such as an image with signed drivers.
    default_accelerator_profile: str | None = None

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
