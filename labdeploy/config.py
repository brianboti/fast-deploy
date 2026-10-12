import re
import time
from pathlib import Path
from urllib.parse import urlparse

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
    paused: bool = False  # refuse new plans and dispatches for this app without touching anything else
    auto_deploy: bool = False  # release the tip of main on its own when it is not the live release; every guardrail still applies
    # Per-app infrastructure settings the renderer writes into the app's Terraform root (alert thresholds, image retention);
    # keys and ranges are those of labdeploy.infra.DEFAULT_SETTINGS. The generated root is never the source of truth.
    infra: dict[str, int | bool] = Field(default_factory=dict)

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
        from .infra import DEFAULT_SETTINGS, SETTING_RANGES
        for key, value in self.infra.items():
            if key not in DEFAULT_SETTINGS or type(value) is not type(DEFAULT_SETTINGS[key]):
                raise ValueError(f"infra.{key} is not a known setting of the right type ({', '.join(DEFAULT_SETTINGS)})")
            if key in SETTING_RANGES and not SETTING_RANGES[key][0] <= value <= SETTING_RANGES[key][1]:
                raise ValueError(f"infra.{key} must be between {SETTING_RANGES[key][0]} and {SETTING_RANGES[key][1]}")
        return self


class Principal(StrictModel):
    name: str
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    applications: list[str]
    can_deploy: bool = False


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


class ObserverIdentity(StrictModel):
    """A viewer-only cloud identity the controller impersonates (laptop) or carries (machine) for live health and logs."""
    service_account: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$")
    cache_seconds: int = Field(default=2700, ge=60, le=3500)


class Config(StrictModel):
    applications: dict[str, Application]
    principals: list[Principal]
    platform: Platform | None = None
    observer: ObserverIdentity | None = None
    oauth: OAuth | None = None
    paused: bool = False  # the kill switch: no new plans or dispatches for any app; reads keep working
    auto_deploy_interval_seconds: int = Field(default=300, ge=60, le=3600)
    notify_webhook_url: str | None = None  # Slack or Discord incoming webhook for the daily watchdog's findings
    backup_dir: str | None = None  # nightly SQLite backups; default: <database>.backups next to the database
    backup_keep_days: int = Field(default=14, ge=1, le=365)
    # A private, versioned bucket the nightly backup also uploads to (the database copy and this configuration).
    backup_bucket: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")
    log_file: str | None = None  # rotating controller log (requests, warnings); the audit trail itself lives in the database
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
        if self.oauth is not None:
            url = urlparse(self.oauth.public_url)
            if url.netloc not in self.allowed_hosts and f"{url.hostname}:*" not in self.allowed_hosts:
                raise ValueError(f"allowed_hosts must list the oauth public host ({url.netloc} or {url.hostname}:*) so the MCP transport accepts it")
        return self


def load_config(path: str) -> Config:
    return Config.model_validate(yaml.load(Path(path).read_text(), Loader=UniqueLoader))  # noqa: S506 - SafeLoader subclass


class LiveConfig:
    """The configuration, re-read when its file changes, so pauses, principals and registry edits apply without a restart.

    Attribute reads pass through to the current Config. An invalid new file is reported and ignored; the previous
    configuration stays in force. The database path and the OAuth block are read once at start and need a restart.
    """

    def __init__(self, config, path=None):
        self.current, self.path, self.error, self.reloaded_at = config, path, None, None
        self.mtime = self._mtime()

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

