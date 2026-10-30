"""The operator's configuration: where the infrastructure layer is, the environments and their identities and rules, and
who may call this service. No token or key lives here: a `cloudinfra` token is named by the environment variable that
holds it, and Google credentials come from the host (see google.py)."""
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SERVICE_ACCOUNT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$")
PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
REGISTRY_PREFIX = re.compile(r"^[a-z0-9.-]+(?::[0-9]{1,5})?(?:/[a-z0-9._-]+)*$")  # host, and optionally a path under it
SIGNER_KEY = re.compile(r"^(?:gcpkms://projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/[a-z0-9-]+/keyRings/[A-Za-z0-9_-]+/cryptoKeys/[A-Za-z0-9_-]+(?:/cryptoKeyVersions/[0-9]+)?"
                        r"|/[A-Za-z0-9._/-]+\.pub)$")  # a Cloud KMS key, or the path of a public key file on this host
ENVIRONMENT = r"^[a-z][a-z0-9-]{1,20}$"
# A Cloud Build 2nd-gen repository: a GitHub (or GitLab, Bitbucket) repository the platform team linked to a connection in
# the trusted project. Cloud Build fetches the source through it; no team or CI job holds a credential for it.
CONNECTED_REPOSITORY = re.compile(r"^projects/(?P<project>[a-z][a-z0-9-]{4,28}[a-z0-9])/locations/(?P<region>[a-z]+-[a-z]+[0-9]+)/connections/[A-Za-z0-9_-]{1,63}"
                                  r"/repositories/[A-Za-z0-9._-]{1,100}$")
# An Artifact Registry Docker repository, LOCATION-docker.pkg.dev/PROJECT/REPOSITORY: where a build pushes.
DOCKER_REPOSITORY = re.compile(r"^(?P<location>[a-z0-9-]+)-docker\.pkg\.dev/(?P<project>[a-z][a-z0-9-]{4,28}[a-z0-9])/[a-z0-9][a-z0-9._-]{0,62}$")
# The image of the one build step: a registry path with a tag or a digest (a digest is the reviewed way).
BUILDER_IMAGE = re.compile(r"^[a-z0-9.-]+(?:/[a-z0-9._-]+)+(?::[A-Za-z0-9_.-]{1,128})?(?:@sha256:[a-f0-9]{64})?$")
SOURCE_NAME = r"^[a-z][a-z0-9-]{1,40}$"


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


class BuildSettings(StrictModel):
    """Building in this environment: Cloud Build in the trusted project, as a build account of its own that may push to the
    team's registry and write logs, and nothing else (docs/research/RESEARCH-2026-10-30-trusted-build.md)."""
    service_account: str = Field(pattern=SERVICE_ACCOUNT.pattern, description="The user-managed account every build of this environment runs as.")
    machine_type: Literal["E2_MEDIUM", "E2_STANDARD_2", "E2_HIGHCPU_8", "E2_HIGHCPU_32"] = "E2_STANDARD_2"
    timeout_seconds: int = Field(default=1200, ge=120, le=14400, description="Cloud Build stops a build after this long (TIMEOUT).")
    max_concurrent_builds: int = Field(default=3, ge=1, le=30, description="Builds one team may have queued or working in this environment at once; Cloud Build's "
                                                                           "default pool holds 10 to 30 per project, shared by every team and the platform's own builds.")
    max_builds_per_day: int = Field(default=50, ge=1, le=5000, description="Builds one team may order in this environment in 24 hours: a cost ceiling, since a build "
                                                                           "runs a team's Dockerfile on a paid machine for up to timeout_seconds.")
    # The docker builder by the digest Cloud Build reported in the provenance of the run of 2026-10-30; Google publishes no
    # versioned tags of it. The same pin as cloudbuild.yaml (a test ties them).
    builder: str = Field(default="gcr.io/cloud-builders/docker@sha256:f772ab459889c67835bd959c2b9e9351f2c0752c4f989710ecda20c229f0733e", pattern=BUILDER_IMAGE.pattern,
                         description="The image of the one docker build step, by digest.")


class BuildSource(StrictModel):
    """A source repository a team may build from, linked by the platform team to Cloud Build once."""
    repository: str = Field(pattern=CONNECTED_REPOSITORY.pattern, description="projects/P/locations/R/connections/C/repositories/NAME, in the trusted project.")
    infra_owner: str = Field(pattern=r"^[a-z][a-z0-9-]{1,30}$", description="The team (cloudinfra owner) whose principals may build from it.")
    registry: str = Field(pattern=DOCKER_REPOSITORY.pattern, description="The team's Artifact Registry repository its images are pushed to.")

    @property
    def project(self) -> str:
        return CONNECTED_REPOSITORY.match(self.repository).group("project")

    @property
    def region(self) -> str:
        """Cloud Build runs a 2nd-gen repository's build in the repository's own region (global is refused)."""
        return CONNECTED_REPOSITORY.match(self.repository).group("region")


class WorkloadLimits(StrictModel):
    """What one released container may use on a machine or a cluster: the bounds every container runs under, so one
    workload cannot starve its host (the review of 2026-10-30)."""
    cpu_millicores: int = Field(default=1000, ge=100, le=64000, description="CPU limit in thousandths of a core (1000 is one core).")
    memory_mb: int = Field(default=1024, ge=64, le=512000)
    pids: int = Field(default=512, ge=16, le=65536, description="Processes the container may run at once.")


class Environment(StrictModel):
    token_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$", description="The environment variable that holds this environment's cloudinfra token.")
    service_account: str = Field(pattern=SERVICE_ACCOUNT.pattern, description="The identity this environment acts as on machines and secrets.")
    pull_service_account: str = Field(pattern=SERVICE_ACCOUNT.pattern,
                                      description="The account a machine pulls images as: it holds roles/artifactregistry.reader on allowed_registries and nothing "
                                                  "else. Its short-lived token is the only credential a machine ever receives; the environment's own never leaves "
                                                  "this service (the review of 2026-10-30).")
    allowed_registries: list[str] = Field(min_length=1, description="Registry hosts, or host/path prefixes, images may come from.")
    workload: WorkloadLimits = Field(default_factory=WorkloadLimits, description="The CPU, memory and process bounds every released container runs under.")
    require_attestation: bool = False
    attestation_type: str = Field(default="slsaprovenance1", pattern=r"^[a-z0-9]{3,40}$")
    trusted_signers: list[str | KeylessSigner] = Field(default_factory=list)
    require_approval: bool = False
    require_provenance: bool = Field(default=False, description="A release's image must carry Cloud Build's SLSA v1 provenance, signed by Google, naming the build "
                                     "this service recorded for it; read as service_account, which needs roles/containeranalysis.occurrences.viewer.")
    build: BuildSettings | None = Field(default=None, description="Building images in this environment; none when it has no block.")

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
        if self.build is not None and self.build.service_account == self.service_account:
            raise ValueError("build.service_account must differ from service_account: the account that runs releases only reads images")
        if self.pull_service_account in (self.service_account, self.build.service_account if self.build else None):
            raise ValueError("pull_service_account must be an account of its own: a machine receives its token, so it may read images and nothing else")
        return self

    def allows(self, repository: str) -> bool:
        """host/path is under one of allowed_registries, whole path parts only."""
        return any(repository == prefix or repository.startswith(prefix.rstrip("/") + "/") for prefix in self.allowed_registries)


class Principal(StrictModel):
    """One caller: an application team, identified by a bearer token kept only as its hash."""
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,30}$")
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    infra_owner: str = Field(pattern=r"^[a-z][a-z0-9-]{1,30}$", description="The cloudinfra principal whose requests this caller may deploy onto.")
    environments: list[str] = Field(min_length=1)
    can_deploy: bool = False
    can_approve: bool = False
    can_build: bool = False


CLAUDE_CALLBACKS = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")


class SignInSettings(StrictModel):
    """Sign-in for Claude's hosted connector (security/oauth.py), which cannot send a fixed token: a person signs in once
    on the service's own page with their principal's token. Off unless this block is set."""
    url: str = Field(description="The service's public HTTPS address as Claude reaches it, such as https://delivery.example.com; the connector's URL is this plus /mcp.")
    callbacks: list[str] = Field(default_factory=lambda: list(CLAUDE_CALLBACKS), min_length=1, description="The only addresses a sign-in may return to; Claude's by default.")
    access_token_minutes: int = Field(default=60, ge=5, le=1440)
    refresh_token_days: int = Field(default=30, ge=1, le=90)

    @field_validator("url")
    @classmethod
    def _url(cls, value):
        parsed = urlparse(value)
        local = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
        if not (parsed.scheme == "https" or local) or not parsed.hostname or parsed.path.strip("/") or parsed.query or parsed.fragment:
            raise ValueError("sign_in.url must be the service's https address with no path, such as https://delivery.example.com")
        return value.rstrip("/")

    @field_validator("callbacks")
    @classmethod
    def _callbacks(cls, value):
        if any(urlparse(uri).scheme != "https" for uri in value):
            raise ValueError("sign_in.callbacks must be https addresses")
        return value


def host_allowed(host: str, allowed_hosts: list[str]) -> bool:
    """The MCP SDK's own Host header rule: an exact entry, or HOST:* for the host with any port."""
    return host in allowed_hosts or any(entry.endswith(":*") and host.startswith(entry[:-2] + ":") for entry in allowed_hosts)


class Config(StrictModel):
    database: str = "appdelivery.db"
    log_file: str | None = None
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1:*", "localhost:*"], description="Host headers the MCP endpoint answers (DNS rebinding protection).")
    rate_limit_per_minute: int = Field(default=120, ge=1)
    auth_failures_per_minute: int = Field(default=20, ge=1)
    registrations_per_hour: int = Field(default=10, ge=1, description="OAuth client registrations one address may make in an hour, with sign-in on.")
    max_request_bytes: int = Field(default=65_536, ge=4096, le=4 * 1024 * 1024, description="Larger request bodies are refused with 413 before they are read.")
    workers: int = Field(default=4, ge=1, le=32, description="Rollouts, rollbacks and removals run in this many background workers.")
    sign_in: SignInSettings | None = None
    cloudinfra: InfraSettings
    gcp: GcpSettings
    environments: dict[str, Environment] = Field(min_length=1)
    sources: dict[str, BuildSource] = Field(default_factory=dict, description="Repositories teams may build from, by a name the build request uses.")
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
        for name, source in self.sources.items():
            if not re.match(SOURCE_NAME, name):
                raise ValueError(f"source name {name!r}: lowercase letters, digits and dashes")
            if source.project != self.gcp.project_id:
                raise ValueError(f"source {name}: its repository is in {source.project}; builds run only in the trusted project {self.gcp.project_id}")
            if DOCKER_REPOSITORY.match(source.registry).group("project") != self.gcp.project_id:
                raise ValueError(f"source {name}: its registry must be in the trusted project {self.gcp.project_id}")
        if self.sign_in and not host_allowed(urlparse(self.sign_in.url).netloc, self.allowed_hosts):
            raise ValueError(f"sign_in.url's host {urlparse(self.sign_in.url).netloc} must be in allowed_hosts, or Claude's calls to /mcp are refused")
        return self


def load_config(path: str | Path) -> Config:
    return Config.model_validate(yaml.safe_load(Path(path).read_text()) or {})
