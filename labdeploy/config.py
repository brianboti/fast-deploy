import re
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import Field, model_validator

from .models import StrictModel, UniqueLoader
from .target import Target

# A service's folder inside its repository: relative, forward slashes, no hidden or parent segments, at most eight deep.
SERVICE_PATH = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*){0,7}$")
KINDS = ("deploy", "recover", "rightsize")


class Application(StrictModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    branch: str = Field(default="main", pattern=r"^[A-Za-z0-9_/-]+$")
    service: str = Field(pattern=r"^[a-z][a-z0-9-]{0,29}$")
    workflow: str = "deploy-gcp.yml"
    # The folder holding the service's Dockerfile, platform.yaml, requirement files and tests; empty for the repository's
    # root (docs/ROADMAP.md D23). One repository may hold several services, each registered with its own path.
    path: str = ""
    # The GitHub environment its workflows run in. The first application of a repository keeps the plain names (staging,
    # deploy-gcp.yml); each further application of the same repository gets names suffixed with its service
    # (staging-<service>, deploy-gcp-<service>.yml), so their variables, identities, schedules and queues never mix (D24).
    environment: str = "staging"
    # Where the contract lives and where the pipeline runs (docs/ROADMAP.md D31, D33). repository: platform.yaml and the
    # three workflows are in the application repository (the default). platform: the contract is apps/<id>/platform.yaml
    # in the platform repository and the pipeline is the platform repository's own deploy.yml, recover.yml and
    # rightsize.yml, run in the environment staging-<service> there; nothing is added to the application repository.
    contract: Literal["repository", "platform"] = "repository"
    pipeline_repository: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    target: Target
    # Refuse plans whose worst-case monthly compute (every allowed instance busy all month) exceeds this many USD.
    cost_ceiling_usd: float | int | None = None
    paused: bool = False  # refuse new plans and dispatches for this app without touching anything else
    auto_deploy: bool = False  # release the tip of main on its own when it is not the live release; every guardrail still applies
    # Whether onboarding merges this application's own pull requests itself; None follows onboarding.auto_merge (docs/ROADMAP.md D29).
    auto_merge: bool | None = None
    # Per-app infrastructure settings the renderer writes into the app's Terraform root (alert thresholds, image retention);
    # keys and ranges are those of labdeploy.infra.DEFAULT_SETTINGS. The generated root is never the source of truth.
    infra: dict[str, int | bool] = Field(default_factory=dict)
    # Google accounts that may open the app's pages when platform.yaml says exposure: lab-members (user:, group: or domain:),
    # on top of onboarding.default_viewers. The operator edits this; developers cannot grant themselves.
    viewers: list[str] = Field(default_factory=list)
    # required: every release carries signed provenance (GitHub's for a public repository, Cosign with the app's Cloud KMS
    # key otherwise). unavailable: the operator's exception for staging or an emergency; a fresh build only, never a reuse.
    provenance: Literal["required", "unavailable"] = "required"
    # How long the release watches a promoted revision's own logs before calling it done (docs/ROADMAP.md D22); a crash or
    # 5xx answers restore the previous traffic. 0 turns the bake off for this application.
    bake_seconds: int = Field(default=300, ge=0, le=900)

    @model_validator(mode="after")
    def workflow_fixed(self):
        if self.service != self.target.service:
            raise ValueError("Service must match the configured cloud target")
        if self.branch != "main":
            raise ValueError("MVP workflow and identity trust are restricted to main")
        if self.path and (not SERVICE_PATH.fullmatch(self.path) or ".." in self.path.split("/")):
            raise ValueError("path is the service's folder inside the repository, such as services/api: relative, no hidden or parent folders")
        if self.contract == "platform":
            if not self.pipeline_repository or self.workflow != "deploy.yml" or self.environment != f"staging-{self.service}":
                raise ValueError("A contract kept in the platform repository runs the platform's own pipeline: pipeline_repository set, "
                                 f"workflow deploy.yml and environment staging-{self.service}")
        else:
            if self.pipeline_repository:
                raise ValueError("pipeline_repository applies to contract: platform only")
            plain = self.workflow == "deploy-gcp.yml" and self.environment == "staging"
            suffixed = self.workflow == f"deploy-gcp-{self.service}.yml" and self.environment == f"staging-{self.service}"
            if not (plain or suffixed):
                raise ValueError("The workflow and environment are deploy-gcp.yml with staging, or, for a further application of the same repository, "
                                 f"deploy-gcp-{self.service}.yml with staging-{self.service}")
        if self.cost_ceiling_usd is not None and self.cost_ceiling_usd <= 0:
            raise ValueError("cost_ceiling_usd must be a positive amount")
        check_viewers(self.viewers)
        from .infra import DEFAULT_SETTINGS, SETTING_RANGES
        for key, value in self.infra.items():
            if key not in DEFAULT_SETTINGS or type(value) is not type(DEFAULT_SETTINGS[key]):
                raise ValueError(f"infra.{key} is not a known setting of the right type ({', '.join(DEFAULT_SETTINGS)})")
            if key in SETTING_RANGES and not SETTING_RANGES[key][0] <= value <= SETTING_RANGES[key][1]:
                raise ValueError(f"infra.{key} must be between {SETTING_RANGES[key][0]} and {SETTING_RANGES[key][1]}")
        return self


    @property
    def suffix(self):
        """'' for the first application of a repository, '-<service>' for a further one (D24); the platform pipeline's
        environments are always suffixed, since every application shares the platform repository."""
        return "" if self.workflow == "deploy-gcp.yml" else f"-{self.service}"

    @property
    def workflows(self):
        """The three workflow files that release, recover and right-size this application, by kind: its own in its
        repository, or the platform repository's (contract: platform)."""
        if self.contract == "platform":
            return {kind: f"{kind}.yml" for kind in KINDS}
        return {kind: f"{kind}-gcp{self.suffix}.yml" for kind in KINDS}

    @property
    def pipeline_repo(self):
        """The repository whose workflows run for this application: the platform's for contract: platform, else its own."""
        return self.pipeline_repository or self.repository

    @property
    def central(self):
        return self.contract == "platform"

    def repo_path(self, name):
        """A file of this service, as a path from the repository's root."""
        return f"{self.path}/{name}" if self.path else name


class Principal(StrictModel):
    name: str
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    applications: list[str]  # application ids, or ["*"] for every application including the ones onboarding registers later
    can_deploy: bool = False
    # Requester and approver (docs/ROADMAP.md D29): false means the pull requests opened for this identity's requests wait for
    # another operator to review and merge them. True keeps the one-operator behaviour: they merge once the policy passes.
    may_merge_own_requests: bool = True


VIEWER = re.compile(r"^(user|group|domain):[A-Za-z0-9._%+-]+(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})?$")


def check_viewers(viewers):
    for viewer in viewers:
        if not VIEWER.fullmatch(viewer):
            raise ValueError(f"Viewer {viewer!r} must be user:EMAIL, group:EMAIL or domain:DOMAIN")
    return viewers


class Onboarding(StrictModel):
    """Where one-sentence onboarding puts a new application and what it may do for it (docs/CLAUDE-CHAT.md, "Deploy a repository")."""

    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    project_number: str = Field(pattern=r"^[0-9]{6,20}$")  # the Workload Identity provider path carries the number, not the id
    region: str = Field(default="us-central1", pattern=r"^[a-z]+-[a-z]+[0-9]$")
    state_bucket: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")  # default: <project_id>-tfstate
    bootstrap_image: str | None = None  # digest-pinned image for the first revision; default: the platform's reference image
    # The GitHub environment is named by the platform: staging for a repository's first application, staging-<service> for a
    # further one (D24); the workflows and the deploy identity's trust both expect exactly that name.
    environment: Literal["staging"] = "staging"
    auto_merge: bool = True  # merge the application's own pull requests once the policy and the checks have passed
    billing_export_table: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
    # Pages for people (docs/ROADMAP.md Phase 8): the lab's accounts that may open every lab-members app, and the project's
    # IAP OAuth client id (not secret), which the release probes use as their token audience behind the proxy.
    default_viewers: list[str] = Field(default_factory=list)
    iap_client_id: str | None = Field(default=None, pattern=r"^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$")

    @model_validator(mode="after")
    def viewers_well_formed(self):
        check_viewers(self.default_viewers)
        return self


# Where Claude's hosted apps and Claude Code return after the browser sign-in. Loopback callbacks match on any port.
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
LOOPBACK_CALLBACKS = ("http://localhost/callback", "http://127.0.0.1/callback")


class OAuth(StrictModel):
    """Browser sign-in for Claude's hosted connector: the controller is its own OAuth 2.1 authorization server."""
    public_url: str  # the controller's external origin, e.g. https://deploy.example.edu; the MCP resource is public_url + /mcp
    redirect_uris: list[str] = Field(default_factory=lambda: [CLAUDE_CALLBACK, *LOOPBACK_CALLBACKS])
    access_token_seconds: int = Field(default=3600, ge=300, le=86_400)
    refresh_token_days: int = Field(default=30, ge=1, le=365)

    @model_validator(mode="after")
    def origin_only(self):
        url = urlparse(self.public_url)
        if url.scheme != "https" and url.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError("oauth.public_url must use https (http is accepted only for localhost)")
        if not url.hostname or url.path not in ("", "/") or url.params or url.query or url.fragment:
            raise ValueError("oauth.public_url must be the controller's origin only, such as https://deploy.example.edu")
        self.public_url = self.public_url.rstrip("/")
        for uri in self.redirect_uris:
            parsed = urlparse(uri)
            if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")):
                raise ValueError("oauth.redirect_uris must be https URLs or loopback http URLs")
        return self


class Platform(StrictModel):
    """Where this platform's own code lives, so app workflows can be regenerated against a pinned commit of it."""
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")  # default: the tip of the platform repository's main branch
    branch: str = Field(default="main", pattern=r"^[A-Za-z0-9_/-]+$")
    # Apps pinned to an older platform commit than this are refused at dispatch until their workflows are regenerated.
    minimum_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    # Names the repository had before a rename (GitHub keeps redirecting them): app workflows that still pin the platform
    # under one are read as pinned until they are regenerated under the new name, so no release stops while they move.
    former_repositories: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def former_names(self):
        for name in self.former_repositories:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name) or name.lower() == self.repository.lower():
                raise ValueError(f"platform.former_repositories: {name!r} must be another owner/name than platform.repository")
        return self


class ObserverIdentity(StrictModel):
    """A viewer-only cloud identity the controller impersonates (laptop) or carries (machine) for live health and logs."""
    service_account: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$")
    cache_seconds: int = Field(default=2700, ge=60, le=3500)


class ApplicationSettings(StrictModel):
    """The operator's switches for an application registered by onboarding (whose entry lives in the database): the same
    fields as an Application's, laid over the registered entry, so nothing has to be copied into the file."""
    cost_ceiling_usd: float | int | None = None
    paused: bool | None = None
    auto_deploy: bool | None = None
    auto_merge: bool | None = None  # overrides onboarding.auto_merge for this application
    infra: dict[str, int | bool] | None = None
    viewers: list[str] | None = None
    provenance: Literal["required", "unavailable"] | None = None
    bake_seconds: int | None = Field(default=None, ge=0, le=900)


PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


class Config(StrictModel):
    applications: dict[str, Application]
    principals: list[Principal]
    application_settings: dict[str, ApplicationSettings] = Field(default_factory=dict)
    platform: Platform | None = None
    observer: ObserverIdentity | None = None
    oauth: OAuth | None = None
    onboarding: Onboarding | None = None
    paused: bool = False  # the kill switch: no new plans or dispatches for any app; reads keep working
    auto_deploy_interval_seconds: int = Field(default=300, ge=60, le=3600)
    notify_webhook_url: str | None = None  # Slack or Discord incoming webhook for the daily watchdog's findings
    backup_dir: str | None = None  # nightly SQLite backups; default: <database>.backups next to the database
    backup_keep_days: int = Field(default=14, ge=1, le=365)
    # A private, versioned bucket the nightly backup also uploads to (the database copy and this configuration).
    backup_bucket: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")
    log_file: str | None = None  # rotating controller log (requests, warnings); the audit trail itself lives in the database
    live_dispatch: bool = False
    # Google's native budget spend caps for Cloud Run that the operator has created (docs/SETUP.md 7i), by project id, in
    # USD a month. A record only: the cap lives in Cloud Billing; plans and the status name it, and warn where none exists.
    cloud_run_spend_caps: dict[str, float | int] = Field(default_factory=dict)
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
            if set(p.applications) - {"*"} - self.applications.keys():
                raise ValueError("Principal references unknown application")
        for project, amount in self.cloud_run_spend_caps.items():
            if not PROJECT_ID.fullmatch(project) or amount <= 0:
                raise ValueError("cloud_run_spend_caps maps a project id to a positive monthly amount in USD")
        for host in self.allowed_hosts:
            # The MCP transport matches Host exactly or as "host:*"; a wildcard entry would silently match nothing.
            if not re.fullmatch(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:(\d{1,5}|\*))?", host):
                raise ValueError("allowed_hosts entries must be exact hostnames with an optional port or ':*'; wildcards are not supported")
        if self.oauth is not None:
            url = urlparse(self.oauth.public_url)
            if url.netloc not in self.allowed_hosts and f"{url.hostname}:*" not in self.allowed_hosts:
                raise ValueError(f"allowed_hosts must list the oauth public host ({url.netloc} or {url.hostname}:*) so the MCP transport accepts it")
        return self


def load_config(path: str) -> Config:
    return Config.model_validate(yaml.load(Path(path).read_text(), Loader=UniqueLoader))  # noqa: S506 - SafeLoader subclass


class LiveConfig:
    """The configuration, re-read when its file changes, so pauses, principals and application entries apply without a restart.

    Rate limits, the observer identity, OAuth settings, allowed hosts, the log file and the database path are read once at
    start; changing them needs a restart.

    Attribute reads pass through to the current Config. An invalid new file is reported and ignored; the previous
    configuration stays in force. The database path and the OAuth block are read once at start and need a restart.
    """

    def __init__(self, config, path=None):
        self.current, self.path, self.error, self.reloaded_at = config, path, None, None
        self.registry = {}  # applications registered by onboarding (the controller's database), merged over the file's
        self.mtime = self._mtime()

    @property
    def applications(self):
        """The file's applications plus the registered ones; a registered id never hides one the operator wrote by hand.
        `application_settings` lays the operator's switches over a registered entry (validated like a whole entry)."""
        registered = {}
        for app_id, application in self.registry.items():
            settings = self.current.application_settings.get(app_id)
            if settings is not None:
                application = Application.model_validate({**application.model_dump(), **settings.model_dump(exclude_none=True)})
            registered[app_id] = application
        return {**registered, **self.current.applications}

    def _mtime(self):
        try:
            return Path(self.path).stat().st_mtime_ns if self.path else None
        except OSError:
            return None

    def refresh(self):
        if not self.path:
            return False
        mtime = self._mtime()
        if mtime is None or mtime == self.mtime:
            return False
        self.mtime = mtime
        try:
            candidate = load_config(self.path)
        except Exception as exc:  # noqa: BLE001 - any parse or validation failure keeps the old configuration
            self.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            return False
        notes = []
        if candidate.database != self.current.database:
            notes.append("database path changes need a restart")
        if candidate.oauth != self.current.oauth:
            notes.append("oauth changes need a restart")
        self.current, self.error, self.reloaded_at = candidate, "; ".join(notes) or None, time.time()
        return True

    def __getattr__(self, name):
        return getattr(self.current, name)

