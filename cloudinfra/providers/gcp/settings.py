"""The operator's settings for the Google Cloud adapter: the project, the regions, where machines go by default, and how the
adapter finds its credential on a laptop. No credential is ever in this model."""
import re

from pydantic import BaseModel, ConfigDict, Field

PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
SERVICE_ACCOUNT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$")


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
