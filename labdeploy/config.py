import re
from pathlib import Path

import yaml
from pydantic import Field, model_validator

from .models import StrictModel, UniqueLoader
from .target import Target


class Application(StrictModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    branch: str = Field(default="main", pattern=r"^[A-Za-z0-9_/-]+$")
    service: str = Field(pattern=r"^[a-z][a-z0-9-]{0,29}$")
    workflow: str = "deploy-gcp.yml"
    target: Target
    # Refuse plans whose worst-case monthly compute (every allowed instance busy all month) exceeds this many USD.
    cost_ceiling_usd: float | int | None = None

    @model_validator(mode="after")
    def workflow_fixed(self):
        if self.service != self.target.service:
            raise ValueError("Service must match the configured cloud target")
        if self.branch != "main":
            raise ValueError("MVP workflow and identity trust are restricted to main")
        if self.workflow != "deploy-gcp.yml":
            raise ValueError("Only the reviewed deploy-gcp.yml workflow is supported")
        if self.cost_ceiling_usd is not None and self.cost_ceiling_usd <= 0:
            raise ValueError("cost_ceiling_usd must be a positive amount")
        return self


class Principal(StrictModel):
    name: str
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    applications: list[str]
    can_deploy: bool = False


class Config(StrictModel):
    applications: dict[str, Application]
    principals: list[Principal]
    live_dispatch: bool = False
    database: str = "deployments.db"
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1:*", "localhost:*", "testserver"])
    # Per-identity request budget and per-client budget for failed logins; the MCP specification requires both kinds of limit.
    rate_limit_per_minute: int = Field(default=120, ge=1, le=100_000)
    auth_failures_per_minute: int = Field(default=20, ge=1, le=10_000)

    @model_validator(mode="after")
    def unique_identity(self):
        if len({p.token_sha256 for p in self.principals}) != len(self.principals):
            raise ValueError("Duplicate principal tokens")
        if len({p.name for p in self.principals}) != len(self.principals):
            raise ValueError("Duplicate principal names")
        for p in self.principals:
            if set(p.applications) - self.applications.keys():
                raise ValueError("Principal references unknown application")
        for host in self.allowed_hosts:
            # The MCP transport matches Host exactly or as "host:*"; a wildcard entry would silently match nothing.
            if not re.fullmatch(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:(\d{1,5}|\*))?", host):
                raise ValueError("allowed_hosts entries must be exact hostnames with an optional port or ':*'; wildcards are not supported")
        return self


def load_config(path: str) -> Config:
    return Config.model_validate(yaml.load(Path(path).read_text(), Loader=UniqueLoader))  # noqa: S506 - SafeLoader subclass

