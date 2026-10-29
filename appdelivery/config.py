"""The operator's configuration: where the infrastructure layer is, the environments and their identities and rules, and
who may call this service. No token or key lives here: a `cloudinfra` token is named by the environment variable that
holds it, and Google credentials come from the host (see google.py)."""
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SERVICE_ACCOUNT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$")
PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
REGISTRY_PREFIX = re.compile(r"^[a-z0-9.-]+(?::[0-9]{1,5})?(?:/[a-z0-9._-]+)*$")  # host, and optionally a path under it
SIGNER_KEY = re.compile(r"^(?:gcpkms://projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/[a-z0-9-]+/keyRings/[A-Za-z0-9_-]+/cryptoKeys/[A-Za-z0-9_-]+(?:/cryptoKeyVersions/[0-9]+)?"
                        r"|/[A-Za-z0-9._/-]+\.pub)$")  # a Cloud KMS key, or the path of a public key file on this host
ENVIRONMENT = r"^[a-z][a-z0-9-]{1,20}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class KeylessSigner(StrictModel):
    """A Sigstore keyless signer: the certificate's identity and the issuer that vouched for it."""
    identity: str = Field(min_length=3, max_length=300)
    issuer: str = Field(pattern=r"^https://[A-Za-z0-9.-]+(?:/[A-Za-z0-9._/-]*)?$")


class InfraSettings(StrictModel):
    url: str = Field(pattern=r"^https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?$", description="The infrastructure layer's base address, without a path.")
    timeout_seconds: int = Field(default=30, ge=1, le=300)


class GcpSettings(StrictModel):
    project_id: str = Field(pattern=PROJECT.pattern, description="The project the infrastructure layer provisions in.")
    default_network_iap: bool = Field(default=False, description="The operator's default network admits 35.235.240.0/20 on port 22.")
    container_profile: str = Field(default="container-host", description="The infrastructure layer's profile for Container-Optimized OS, named in refusals.")


class Environment(StrictModel):
    token_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$", description="The environment variable that holds this environment's cloudinfra token.")
    service_account: str = Field(pattern=SERVICE_ACCOUNT.pattern, description="The identity this environment acts as on machines and secrets.")
    allowed_registries: list[str] = Field(min_length=1, description="Registry hosts, or host/path prefixes, images may come from.")
    require_attestation: bool = False
    attestation_type: str = Field(default="slsaprovenance1", pattern=r"^[a-z0-9]{3,40}$")
    trusted_signers: list[str | KeylessSigner] = Field(default_factory=list)
    require_approval: bool = False

    @field_validator("allowed_registries")
    @classmethod
    def _registries(cls, value):
        for prefix in value:
            if not REGISTRY_PREFIX.match(prefix):
                raise ValueError(f"allowed_registries: {prefix!r} is not a registry host or host/path")
        return value

    @field_validator("trusted_signers")
    @classmethod
    def _signers(cls, value):
        for signer in value:
            if isinstance(signer, str) and not SIGNER_KEY.match(signer):
                raise ValueError(f"trusted_signers: {signer!r} is neither a gcpkms:// key nor the absolute path of a .pub file")
        return value

    @model_validator(mode="after")
    def _attestation(self):
        if self.require_attestation and not self.trusted_signers:
            raise ValueError("require_attestation needs at least one trusted_signers entry")
        return self


class Principal(StrictModel):
    """One caller: an application team, identified by a bearer token kept only as its hash."""
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,30}$")
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    infra_owner: str = Field(pattern=r"^[a-z][a-z0-9-]{1,30}$", description="The cloudinfra principal whose requests this caller may deploy onto.")
    environments: list[str] = Field(min_length=1)
    can_deploy: bool = False
    can_approve: bool = False


class Config(StrictModel):
    database: str = "appdelivery.db"
    cloudinfra: InfraSettings
    gcp: GcpSettings
    environments: dict[str, Environment] = Field(min_length=1)
    principals: list[Principal]

    @field_validator("environments")
    @classmethod
    def _environment_names(cls, value):
        for name in value:
            if not re.match(ENVIRONMENT, name):
                raise ValueError(f"environment name {name!r}: lowercase letters, digits and dashes")
        return value

    @model_validator(mode="after")
    def _consistent(self):
        names = [principal.name for principal in self.principals]
        if len(set(names)) != len(names):
            raise ValueError("principal names must be unique")
        for principal in self.principals:
            unknown = sorted(set(principal.environments) - set(self.environments))
            if unknown:
                raise ValueError(f"principal {principal.name}: unknown environments {', '.join(unknown)}")
        accounts = [environment.service_account for environment in self.environments.values()]
        if len(set(accounts)) != len(accounts):
            raise ValueError("each environment needs its own service_account")
        return self


def load_config(path: str | Path) -> Config:
    return Config.model_validate(yaml.safe_load(Path(path).read_text()) or {})
