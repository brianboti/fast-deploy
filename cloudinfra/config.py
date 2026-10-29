"""The operator's configuration of the infrastructure service: who may call it, what it may create, and the provider.

The provider project, the allowed regions, the limits and the credentials are the operator's; a request never names them.
Credentials are not in this file either: the Google adapter takes a token from the host it runs on (see providers/gcp/auth.py).
"""
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .providers.gcp.settings import GcpSettings
from .security.validation import MEMBER, normal_member


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
    # The identity granted access to what this caller creates, when access.grant_requester is on: a person for a person's
    # token, or the service account an Application Layer system runs as.
    identity: str | None = Field(default=None, pattern=MEMBER.pattern)

    @field_validator("identity")
    @classmethod
    def _identity(cls, value):
        return None if value is None else normal_member(value)


class Limits(StrictModel):
    """Cost protection: what one request may ask for. The validator refuses anything above these before any provider call."""
    max_instances_per_request: int = Field(default=10, ge=1, le=100)
    max_cpu_per_instance: int = Field(default=32, ge=1, le=96)
    max_memory_gb_per_instance: int = Field(default=128, ge=1, le=640)
    max_storage_gb_per_request: int = Field(default=2000, ge=1, le=64000)
    max_database_storage_gb: int = Field(default=1000, ge=10, le=10000)
    max_accelerators_per_instance: int = Field(default=8, ge=0, le=16, description="0 refuses every accelerator request.")
    max_cache_memory_gb: int = Field(default=64, ge=1, le=300)
    max_cluster_nodes: int = Field(default=20, ge=1, le=100)
    max_monthly_cost_usd: float = Field(default=500.0, ge=0, description="Refuse a request whose estimated monthly cost from list prices is higher.")
    max_active_requests_per_principal: int = Field(default=20, ge=1, le=1000)
    operation_timeout_seconds: int = Field(default=900, ge=60, le=3600)
    # Time. Nothing expires or stops by itself unless a request asks or the operator sets a default here.
    default_lifetime_hours: int | None = Field(default=None, ge=1, le=8760, description="Applied to requests that set no lifetime; none by default.")
    max_lifetime_hours: int | None = Field(default=None, ge=1, le=8760, description="The longest a request may live, counted from creation, extensions included; "
                                                                                 "with no default lifetime, a request that sets none lives this long.")
    expiry_retention_days: int = Field(default=7, ge=0, le=90, description="How long a request stopped at expiry keeps its disks and databases before it is destroyed.")
    default_autostop_idle_minutes: int | None = Field(default=None, ge=15, le=1440, description="Idle stop for compute groups that ask for none; none by default.")
    default_autostop_scope: Literal["accelerators", "all"] = Field(default="accelerators", description="Which groups the default idle stop applies to.")
    idle_cpu_cores: float = Field(default=0.5, gt=0, le=16, description="A machine is idle when its busiest CPU sample in the window used fewer cores than this.")
    max_keep_awake_hours: int = Field(default=72, ge=1, le=720)

    @model_validator(mode="after")
    def _lifetimes(self):
        if self.default_lifetime_hours and self.max_lifetime_hours and self.default_lifetime_hours > self.max_lifetime_hours:
            raise ValueError("limits.default_lifetime_hours cannot exceed limits.max_lifetime_hours")
        return self


class AccessSettings(StrictModel):
    """Who may be granted access to what the service creates. The service grants only on resources it created, through
    each resource's own IAM policy, and never changes project IAM."""
    allowed_domains: list[str] = Field(default_factory=list, description="Domains a member must belong to, such as the company's; empty allows only principals' identities.")
    grant_requester: bool = Field(default=True, description="Grant the requesting principal's identity on what it creates.")
    max_members: int = Field(default=10, ge=0, le=50, description="At most this many members per request, the requester included.")


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
    access: AccessSettings = Field(default_factory=AccessSettings)
    sweep_interval_seconds: int = Field(default=300, ge=0, le=86400, description="How often the sweeper checks expiry and idleness; 0 turns it off, and with "
                                                                                 "it every lifetime and idle stop, which requests then may not ask for.")
    return_database_password: bool = Field(default=False, description="Also return a generated database password once in the creating response; "
                                                                       "it is always written to a secret the request owns.")
    providers: Providers
    principals: list[Principal]

    @model_validator(mode="after")
    def _consistent(self):
        names = [principal.name for principal in self.principals]
        if len(set(names)) != len(names):
            raise ValueError("principal names must be unique")
        if not any((self.providers.gcp,)):
            raise ValueError("providers: configure at least one provider (gcp)")
        timed = [name for name in ("default_lifetime_hours", "max_lifetime_hours", "default_autostop_idle_minutes") if getattr(self.limits, name)]
        if timed and self.sweep_interval_seconds == 0:
            raise ValueError(f"limits.{timed[0]} needs the sweeper, which sweep_interval_seconds: 0 turns off")
        return self

    def configured_providers(self) -> list[str]:
        return [name for name in ("gcp",) if getattr(self.providers, name) is not None]


def load_config(path: str | Path) -> Config:
    return Config.model_validate(yaml.safe_load(Path(path).read_text()) or {})
