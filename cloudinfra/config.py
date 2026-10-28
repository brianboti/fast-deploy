"""The operator's configuration of the infrastructure service: who may call it, what it may create, and the provider.

The provider project, the allowed regions, the limits and the credentials are the operator's; a request never names them.
Credentials are not in this file either: the Google adapter takes a token from the host it runs on (see providers/gcp/auth.py).
"""
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .providers.gcp.settings import GcpSettings


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Principal(StrictModel):
    """One caller of the service: an Application Layer system, identified by a bearer token kept only as its hash."""
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,30}$")
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    can_provision: bool = False
    can_destroy: bool = False
    # own: this identity sees and acts on the requests it made; all: every request (an operator identity).
    scope: str = Field(default="own", pattern=r"^(own|all)$")
    max_monthly_cost_usd: float | None = Field(default=None, ge=0, description="A tighter ceiling than limits.max_monthly_cost_usd for this caller.")


class Limits(StrictModel):
    """Cost protection: what one request may ask for. The validator refuses anything above these before any provider call."""
    max_instances_per_request: int = Field(default=10, ge=1, le=100)
    max_cpu_per_instance: int = Field(default=32, ge=1, le=96)
    max_memory_gb_per_instance: int = Field(default=128, ge=1, le=640)
    max_storage_gb_per_request: int = Field(default=2000, ge=1, le=64000)
    max_database_storage_gb: int = Field(default=1000, ge=10, le=10000)
    max_monthly_cost_usd: float = Field(default=500.0, ge=0, description="Refuse a request whose estimated monthly cost from list prices is higher.")
    max_active_requests_per_principal: int = Field(default=20, ge=1, le=1000)
    operation_timeout_seconds: int = Field(default=900, ge=60, le=3600)


class Providers(StrictModel):
    """One optional settings block per adapter; a provider the operator leaves out is refused with UNSUPPORTED_PROVIDER."""
    gcp: GcpSettings | None = None


class Config(StrictModel):
    database: str = "cloudinfra.db"
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1:*", "localhost:*"])
    rate_limit_per_minute: int = Field(default=120, ge=1)
    auth_failures_per_minute: int = Field(default=20, ge=1)
    log_file: str | None = None
    workers: int = Field(default=4, ge=1, le=32, description="Provisioning and destruction run in this many background workers.")
    cleanup_on_failure: bool = Field(default=True, description="Delete what a failed request created; false keeps it for inspection and reports it.")
    labels: dict[str, str] = Field(default_factory=dict, description="The operator's labels on every resource (cost center, environment).")
    limits: Limits = Field(default_factory=Limits)
    providers: Providers
    principals: list[Principal]

    @model_validator(mode="after")
    def _consistent(self):
        names = [principal.name for principal in self.principals]
        if len(set(names)) != len(names):
            raise ValueError("principal names must be unique")
        if not any((self.providers.gcp,)):
            raise ValueError("providers: configure at least one provider (gcp)")
        return self

    def configured_providers(self) -> list[str]:
        return [name for name in ("gcp",) if getattr(self.providers, name) is not None]


def load_config(path: str | Path) -> Config:
    return Config.model_validate(yaml.safe_load(Path(path).read_text()) or {})
