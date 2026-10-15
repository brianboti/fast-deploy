import contextlib
import contextvars
import datetime
import functools
import hashlib
import hmac
import logging
import os
import threading
import time
from typing import Any

import httpx
import pydantic
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from . import __version__
from .cloud import Observer
from .config import LiveConfig, load_config
from .controller import Controller, Store
from .cost import PRICING
from .github import GitHub, explain
from .oauth import PUBLIC_PATHS, Provider, mount
from .scheduler import Scheduler

log = logging.getLogger("labdeploy")

identity = contextvars.ContextVar("identity")
client_address = contextvars.ContextVar("client_address", default="unknown")
LOGGERS = ("uvicorn", "uvicorn.error", "labdeploy")  # not uvicorn.access: request lines carry sign-in links and query strings


class UpstreamError(ValueError):
    """GitHub could not serve the request; explained in plain words and reported as a gateway failure on REST."""


class UnexpectedError(RuntimeError):
    """A failure nobody foresaw, still reported as a sentence with a next action rather than a bare exception text."""


def unexpected(exc):
    """What Claude and the operator read when something unforeseen fails (docs/ROADMAP.md 12.3)."""
    return (f"The controller stopped on an unexpected {type(exc).__name__}: {str(exc)[:300] or 'no detail'}. It retried nothing. Check the "
            "state of what was asked (get_deployment_status, get_onboarding_status or list_applications) before asking again, and tell the "
            "operator if it repeats: list_events and the controller log have the details.")


def plain_validation(error):
    """A validation failure as one sentence a person can act on: which field, and what it must be."""
    problems = [f"{'.'.join(str(part) for part in item.get('loc', ())) or 'the file'}: {item.get('msg')}" for item in error.errors()]
    what = "platform.yaml" if error.title == "AppSpec" else error.title
    return f"{what} is not valid: {'; '.join(problems)}"


def invoke(store, kind, name, fn, subject=None, record=True):
    """Run one operation for the authenticated identity: plain GitHub errors, and one audit event with outcome and duration."""
    started = time.monotonic()
    principal = identity.get(None)
    who = principal.name if principal is not None else None

    def note(outcome, detail=None):
        if record:
            store.record_event(kind, name=name, identity=who, client=client_address.get(), application=subject, outcome=outcome,
                               duration_ms=int((time.monotonic() - started) * 1000), detail=detail)
    try:
        result = fn()
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        message = explain(exc)
        note("error", message)
        raise UpstreamError(message) from None
    except pydantic.ValidationError as exc:
        message = plain_validation(exc)
        note("error", message)
        raise ValueError(message) from None
    except ValueError as exc:  # the controller's own refusals: already a sentence that says what to do
        note("error", str(exc))
        raise
    except Exception as exc:
        message = unexpected(exc)
        note("error", message)
        raise UnexpectedError(message) from exc
    note("ok")
    return result


def audited(store, record=True):
    """Decorator for MCP tools: every call is recorded with its identity, subject, outcome and duration.

    The MCP SDK calls synchronous tools on the event loop, so a waiting status call would stall every other caller;
    the wrapper is asynchronous and runs the tool in the thread pool with the caller's identity carried along.
    """
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            subject = kwargs.get("application") or kwargs.get("plan_id")
            principal, client = identity.get(None), client_address.get(None)

            def call():
                identity.set(principal)
                client_address.set(client)
                return invoke(store, "tool", fn.__name__, lambda: fn(*args, **kwargs), subject, record)
            return await run_in_threadpool(call)
        wrapper.lab_deploy_audited = True  # tests/test_phase12.py refuses any registered tool without it
        return wrapper
    return decorate


def attach_log_file(path):
    """A rotating log file for the controller's own output; idempotent across restarts of the factory."""
    from logging.handlers import RotatingFileHandler
    handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for name in LOGGERS:
        logger = logging.getLogger(name)
        if not any(getattr(h, "baseFilename", None) == handler.baseFilename for h in logger.handlers):
            logger.addHandler(handler)
        if logger.level > logging.INFO or logger.level == logging.NOTSET:
            logger.setLevel(logging.INFO)

# Safety hints let the client ask the human before risky calls; every tool acts on a closed, registered set of resources.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
CREATES_RECORD = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
CHANGES_CLOUD = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)

INSTRUCTIONS = (
    "Lab Deploy releases approved lab applications to GCP staging through a reviewed GitHub Actions workflow. "
    "Order of operations: list_applications, optionally analyze_repository, create_deployment_plan, deploy_staging with "
    "that plan id, then get_deployment_status. A plan is bound to one commit and one cloud target and expires after 15 "
    "minutes; create a new plan instead of retrying a rejected or expired one. 'dispatched' means GitHub accepted the "
    "job, not that the release succeeded: report the workflow state and the individual checks from get_deployment_status, "
    "and never describe a check as passed unless its value is 'passed'. Requests for other clouds, production, public access, "
    "GPUs or resources above the lab's limits are refused by policy; relay the refusal instead of working around it. "
    "Databases, buckets, topics and migrations are supported: the app declares them under needs in platform.yaml through a "
    "reviewed pull request on its repository, then propose_infrastructure provisions them once its pull request is merged. "
    "For a problem with a live app, read get_service_health and get_recent_logs; rollback_staging moves traffic back to the "
    "previous release in seconds but does not undo data changes. "
    "Use estimate_cost to explain cost bounds and findings before deploying, and get_cost_report for measured right-sizing; "
    "present bounds as bounds, never as a bill forecast, and say which costs are excluded. "
    "To deploy a repository that is not registered yet, call onboard_application with its link: it validates the repository against "
    "the contract, registers it, opens and merges its pull requests when the policy passes, prepares its GitHub environment and "
    "releases the first commit, continuing on its own in the background; report the step and what it waits for, use "
    "get_onboarding_status to follow it, and get_deployment_status with the plan id it returns when complete."
)


class RateLimiter:
    """Fixed-window counter per key. In-process by design: the controller runs as one instance, like its plan store."""

    def __init__(self, limit, window_seconds=60, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window_seconds, clock
        self.lock = threading.Lock()
        self.windows = {}

    def allow(self, key):
        """Return (allowed, retry_after_seconds)."""
        now = self.clock()
        with self.lock:
            start, count = self.windows.get(key, (now, 0))
            if now - start >= self.window:
                start, count = now, 0
            if count >= self.limit:
                return False, max(1, int(self.window - (now - start)) + 1)
            self.windows[key] = (start, count + 1)
            if len(self.windows) > 10_000:
                self.windows = {k: v for k, v in self.windows.items() if now - v[0] < self.window}
            return True, 0


class Limits:
    """One set of budgets shared by the request gate and the OAuth sign-in page."""

    def __init__(self, config):
        self.identities = RateLimiter(config.rate_limit_per_minute)
        self.failures = RateLimiter(config.auth_failures_per_minute)
        self.public = RateLimiter(config.rate_limit_per_minute)  # OAuth endpoints reached before any token exists


class Authenticate:
    def __init__(self, app, config, limits=None, oauth=None, store=None):
        self.app, self.config, self.oauth, self.store = app, config, oauth, store
        self.limits = limits or Limits(config)
        # RFC 9728: the challenge tells Claude where to find the authorization server when sign-in is enabled.
        self.challenge = "Bearer" if oauth is None else f'Bearer resource_metadata="{oauth.metadata_url}"'

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)
        refresh = getattr(self.config, "refresh", None)
        if callable(refresh):
            refresh()  # a changed configuration file applies to this request; an invalid one is ignored and reported
        client = (scope.get("client") or ("unknown", 0))[0]
        address = client_address.set(client)
        try:
            return await self.gate(scope, receive, send, client)
        finally:
            client_address.reset(address)

    async def gate(self, scope, receive, send, client):
        if self.oauth is not None and (scope["path"] in PUBLIC_PATHS or scope["path"].startswith("/.well-known/")):
            allowed, retry_after = self.limits.public.allow(client)
            if not allowed:
                return await JSONResponse({"error": "Too many requests"}, status_code=429, headers={"Retry-After": str(retry_after)})(scope, receive, send)
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        authorization = headers.get(b"authorization", b"").decode("latin1")
        scheme, _, token = authorization.partition(" ")
        # RFC 9110 authentication schemes are case-insensitive; the token itself is compared by hash.
        token = token.strip() if scheme.lower() == "bearer" else ""
        digest = hashlib.sha256(token.encode()).hexdigest()
        principal = next((p for p in self.config.principals if token and hmac.compare_digest(digest, p.token_sha256)), None)
        if principal is None and token and self.oauth is not None:
            principal = await run_in_threadpool(self.oauth.authenticate, token)
        if principal is None:
            # Failed logins are budgeted per client address to slow token guessing; the proxy should limit as well.
            allowed, retry_after = self.limits.failures.allow(client)
            if not allowed:
                if self.store is not None:
                    self.store.record_event("auth_burst", client=client, outcome="throttled", detail=scope["path"])
                return await JSONResponse({"error": "Too many failed authentication attempts"}, status_code=429,
                                          headers={"Retry-After": str(retry_after)})(scope, receive, send)
            return await JSONResponse({"error": "Authentication required"}, status_code=401, headers={"WWW-Authenticate": self.challenge})(scope, receive, send)
        allowed, retry_after = self.limits.identities.allow(principal.name)
        if not allowed:
            return await JSONResponse({"error": "Rate limit exceeded for this identity; retry later"}, status_code=429,
                                      headers={"Retry-After": str(retry_after)})(scope, receive, send)
        ctx = identity.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            identity.reset(ctx)


def build_app(config, github=None, config_path=None, observer_factory=Observer, background=False):
    config = config if isinstance(config, LiveConfig) else LiveConfig(config, config_path)
    store = Store(config.database)
    adapter = github or GitHub()
    observer = observer_factory(config) if config.observer else None
    controller = Controller(config, adapter, store, observer)
    started = time.time()
    mcp = FastMCP("Lab Deploy", instructions=INSTRUCTIONS, stateless_http=True, json_response=True,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=config.allowed_hosts, allowed_origins=[]))
    mcp._mcp_server.version = __version__  # the handshake reports Lab Deploy's version, not the SDK's
    plain_errors = audited(store)
    quiet = audited(store, record=False)

    def rest(name, fn, subject=None):
        return invoke(store, "rest", name, fn, subject)

    @mcp.tool(title="List applications", annotations=READ_ONLY, structured_output=True)
    @plain_errors
    def list_applications() -> dict[str, Any]:
        """Applications permitted for the caller, each with the live release this controller recorded and the commits main holds beyond it."""
        return controller.applications(identity.get())

    @mcp.tool(title="Analyze repository", annotations=READ_ONLY, structured_output=True)
    @plain_errors
    def analyze_repository(application: str) -> dict[str, Any]:
        """Inspect approved repository metadata; return a draft with explicit assumptions."""
        return controller.inspect(identity.get(), application)

    @mcp.tool(title="Create deployment plan", annotations=CREATES_RECORD, structured_output=True)
    @plain_errors
    def create_deployment_plan(application: str, cloud: str = "gcp", environment: str = "staging", commit_sha: str | None = None) -> dict[str, Any]:
        """Validate committed platform.yaml and pin an expiring plan to a Git commit: the tip of main, or any older commit already on main (commit_sha)."""
        return controller.plan(identity.get(), application, cloud, environment, commit_sha)

    @mcp.tool(title="Propose configuration (draft PR)", annotations=CREATES_RECORD, structured_output=True)
    @plain_errors
    def propose_configuration(application: str | None = None, repository: str | None = None, path: str | None = None) -> dict[str, Any]:
        """Open a draft pull request (trial edit) with the files docs/DEVELOPER-CONTRACT.md asks for that the service lacks (Dockerfile, .dockerignore,
        requirements-dev.txt, a hash-locked requirements.lock, a health test, platform.yaml with reviewed: false, CI, SECURITY.md,
        Dependabot), never one that exists. Give a registered `application`, or a `repository` link and its `path` before onboarding.
        The platform never merges it."""
        return controller.propose_config(identity.get(), application, repository, path)

    @mcp.tool(title="Update app workflows (pull request)", annotations=CREATES_RECORD, structured_output=True)
    @plain_errors
    def update_app_workflows(application: str, platform_commit: str | None = None) -> dict[str, Any]:
        """Regenerate the app's three workflow files against the platform's current (or given) commit and open a pull request. Never merges."""
        return controller.update_workflows(identity.get(), application, platform_commit)

    @mcp.tool(title="Propose infrastructure (pull request)", annotations=CREATES_RECORD, structured_output=True)
    @plain_errors
    def propose_infrastructure(application: str) -> dict[str, Any]:
        """Render the app's Terraform root (service, registry, identities, trust, alerts) and open a pull request on the platform repository; the pipeline applies it on merge. Never merges."""
        return controller.propose_infrastructure(identity.get(), application)

    @mcp.tool(title="Onboard a repository (one sentence)", annotations=CHANGES_CLOUD, structured_output=True)
    @plain_errors
    def onboard_application(repository: str, application: str | None = None, auto_deploy: bool = False, path: str | None = None) -> dict[str, Any]:
        """From a repository link to a running service, for a repository that follows docs/DEVELOPER-CONTRACT.md: validate it, register it,
        open and (when the policy and checks pass) merge its infrastructure and workflow pull requests, wait for the pipeline, create the
        GitHub environment with its variables, and release the first commit. `path`: the service's folder when it is not the repository
        root (a monorepo, e.g. services/api); call once per service. Resumable: call again to continue, or after fixing what blocked it.
        Changes cloud resources."""
        return controller.onboard(identity.get(), repository, application, auto_deploy, path)

    @mcp.tool(title="Check a repository", annotations=READ_ONLY, structured_output=True)
    @plain_errors
    def check_repository(repository: str, path: str | None = None) -> dict[str, Any]:
        """Check a GitHub repository (or one service folder of it) against docs/DEVELOPER-CONTRACT.md before deploying: each required
        and recommended item, whether it holds, why it matters, and which missing files the platform can draft. Reads only."""
        return controller.check_repository(identity.get(), repository, path)

    @mcp.tool(title="Get onboarding status", annotations=READ_ONLY, structured_output=True)
    @plain_errors
    def get_onboarding_status(application: str) -> dict[str, Any]:
        """Where a repository's onboarding stands: the step in progress, what it waits for, or what blocked it and how to fix it."""
        return controller.onboarding_status(identity.get(), application)

    @mcp.tool(title="Deploy to staging", annotations=CHANGES_CLOUD, structured_output=True)
    @plain_errors
    def deploy_staging(plan_id: str) -> dict[str, Any]:
        """Dispatch the approved workflow for an existing plan. Changes cloud resources when enabled."""
        return controller.deploy(identity.get(), plan_id)

    @mcp.tool(title="Deploy the latest commit", annotations=CHANGES_CLOUD, structured_output=True)
    @plain_errors
    def deploy_latest(application: str) -> dict[str, Any]:
        """Plan the tip of main and dispatch it in one call when every policy passes. Changes cloud resources when enabled."""
        return controller.deploy_latest(identity.get(), application)

    @mcp.tool(title="Roll back to the previous revision", annotations=CHANGES_CLOUD, structured_output=True)
    @plain_errors
    def rollback_staging(application: str) -> dict[str, Any]:
        """Move traffic back to the split that served before the live release, in seconds and without a build. Changes cloud resources."""
        return controller.rollback(identity.get(), application)

    @mcp.tool(title="Cancel a running deployment", annotations=CHANGES_CLOUD, structured_output=True)
    @plain_errors
    def cancel_deployment(plan_id: str) -> dict[str, Any]:
        """Cancel the GitHub run of a dispatched plan. The recovery workflow restores traffic if the release had started changing it."""
        return controller.cancel(identity.get(), plan_id)

    @mcp.tool(title="Compare main with the live release", annotations=READ_ONLY, structured_output=True)
    @plain_errors
    def compare_with_live(application: str) -> dict[str, Any]:
        """Commits and files on main since the live release, what kinds of change they are, and the cost bounds before and after."""
        return controller.compare_with_live(identity.get(), application)

    @mcp.tool(title="Get service health (live)", annotations=READ_ONLY, structured_output=True)
    @plain_errors
    def get_service_health(application: str, minutes: int = 60) -> dict[str, Any]:
        """Live view from Cloud Run and Cloud Monitoring: readiness, serving revisions with their plan and commit, requests, errors, latency,
        instances, memory, and whether it matches what this controller recorded."""
        return controller.service_health(identity.get(), application, minutes)

    @mcp.tool(title="Get recent logs (live)", annotations=READ_ONLY, structured_output=True)
    @plain_errors
    def get_recent_logs(application: str, minutes: int = 30, severity: str = "ERROR", limit: int = 50) -> dict[str, Any]:
        """The newest log lines of the service at or above a severity (DEFAULT, INFO, WARNING, ERROR, CRITICAL), newest first."""
        return controller.recent_logs(identity.get(), application, minutes, severity, limit)

    @mcp.tool(title="Get deployment status", annotations=READ_ONLY, structured_output=True)
    @plain_errors
    def get_deployment_status(plan_id: str, wait_seconds: int = 0) -> dict[str, Any]:
        """Actual GitHub workflow status with every step, the release receipt, recovery, and a next action. Dispatched does not mean deployed. wait_seconds (up to 45) waits for a change."""
        return controller.status(identity.get(), plan_id, wait_seconds)

    @mcp.tool(title="List plans", annotations=READ_ONLY, structured_output=True)
    @plain_errors
    def list_plans() -> dict[str, Any]:
        """List this caller's own deployment plans, newest first, with their current state."""
        return controller.plans(identity.get())

    @mcp.tool(title="Estimate cost", annotations=READ_ONLY, structured_output=True)
    @plain_errors
    def estimate_cost(application: str, requests_per_month: int | None = None, average_request_seconds: float | None = None) -> dict[str, Any]:
        """Cost bounds from list prices for the committed platform.yaml, plus findings; optional traffic scenario."""
        return controller.cost(identity.get(), application, requests_per_month, average_request_seconds)

    @mcp.tool(title="Propose right-sizing (pull request)", annotations=CREATES_RECORD, structured_output=True)
    @plain_errors
    def propose_rightsizing(application: str) -> dict[str, Any]:
        """Turn the latest measured right-sizing proposals into a pull request against platform.yaml, with the savings and cost bounds. Never merges."""
        return controller.propose_rightsizing(identity.get(), application)

    @mcp.tool(title="Get cost report", annotations=READ_ONLY, structured_output=True)
    @plain_errors
    def get_cost_report(application: str) -> dict[str, Any]:
        """Latest measured right-sizing report from the rightsizing workflow; never estimated."""
        return controller.cost_report(identity.get(), application)

    @mcp.tool(title="List events", annotations=READ_ONLY, structured_output=True)
    @quiet
    def list_events(limit: int = 50, application: str | None = None) -> dict[str, Any]:
        """Recent audit events: tool and REST calls, sign-ins, throttling, token reuse. Deployers see every identity; read-only identities see their own."""
        principal = identity.get()
        return {"events": store.recent_events(min(max(int(limit), 1), 500), identity=None if principal.can_deploy else principal.name, application=application)}

    def spending_guards():
        caps = getattr(config, "cloud_run_spend_caps", None) or {}
        projects = sorted({application.target.project_id for application in config.applications.values()})
        return {"cloud_run_spend_caps": {project: caps.get(project) for project in projects},
                "missing": [project for project in projects if not caps.get(project)],
                "note": "Native Cloud Run spend caps the operator recorded (docs/SETUP.md 7i); the platform's own cap workflow also stops databases."}

    @mcp.tool(title="Get controller status", annotations=READ_ONLY, structured_output=True)
    @plain_errors
    def get_controller_status() -> dict[str, Any]:
        """Controller version, dispatch and sign-in switches, registered apps, GitHub credential expiry, and pricing freshness."""
        status = getattr(adapter, "credential_status", None)
        credential = status() if callable(status) else {"kind": "unknown", "expires_at": None, "days_left": None, "warning": None}
        return {"version": __version__, "live_dispatch": config.live_dispatch, "oauth_sign_in": config.oauth is not None, "paused": config.paused,
                "observer": config.observer.service_account if config.observer else None,
                "auto_deploy": sorted(k for k, a in config.applications.items() if a.auto_deploy),
                "background": scheduler["thread"].snapshot() if scheduler["thread"] is not None else None,
                "paused_applications": sorted(k for k, a in config.applications.items() if a.paused),
                "configuration": {"path": getattr(config, "path", None), "reloaded_at": getattr(config, "reloaded_at", None), "error": getattr(config, "error", None)},
                "applications": sorted(config.applications), "github_credential": credential,
                "spending_guards": spending_guards(), "infrastructure_drift": controller.drift_status(),
                "pricing_verified": PRICING["verified"], "started_at": datetime.datetime.fromtimestamp(started, datetime.UTC).isoformat(timespec="seconds"),
                "uptime_seconds": int(time.time() - started)}

    scheduler = {"thread": None}

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if background:
            scheduler["thread"] = Scheduler(controller, config)
            scheduler["thread"].start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            if scheduler["thread"] is not None:
                scheduler["thread"].stop()

    app = FastAPI(title="Lab Deploy", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.mcp = mcp  # the tool list, for the description checksums (labdeploy/toolpin.py) and the wrapper test
    limits = Limits(config)
    oauth = Provider(config, store) if config.oauth else None
    app.add_middleware(Authenticate, config=config, limits=limits, oauth=oauth, store=store)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.exception_handler(UpstreamError)
    async def upstream_failed(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=502)

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(UnexpectedError)
    async def unexpected_failure(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=500)

    @app.exception_handler(httpx.HTTPStatusError)
    async def github_refused(request, exc):
        return JSONResponse({"error": explain(exc)}, status_code=502)

    @app.exception_handler(httpx.RequestError)
    async def github_unreachable(request, exc):
        return JSONResponse({"error": explain(exc)}, status_code=503)

    @app.get("/applications")
    def applications():
        return rest("applications", lambda: controller.applications(identity.get()))

    @app.post("/applications/{application}/plans")
    def plan(application: str):
        return rest("create_plan", lambda: controller.plan(identity.get(), application), application)

    @app.post("/plans/{plan_id}/deploy")
    def deploy(plan_id: str):
        return rest("deploy", lambda: controller.deploy(identity.get(), plan_id), plan_id)

    @app.get("/plans")
    def plans():
        return rest("plans", lambda: controller.plans(identity.get()))

    @app.get("/applications/{application}/cost")
    def cost(application: str, requests_per_month: int | None = None, average_request_seconds: float | None = None):
        return rest("cost", lambda: controller.cost(identity.get(), application, requests_per_month, average_request_seconds), application)

    @app.get("/applications/{application}/cost-report")
    def cost_report(application: str):
        return rest("cost_report", lambda: controller.cost_report(identity.get(), application), application)

    @app.get("/plans/{plan_id}")
    def status(plan_id: str, wait_seconds: int = 0):
        return rest("status", lambda: controller.status(identity.get(), plan_id, wait_seconds), plan_id)

    @app.get("/events")
    def events(limit: int = 50, application: str | None = None):
        principal = identity.get()
        return {"events": store.recent_events(min(max(limit, 1), 500), identity=None if principal.can_deploy else principal.name, application=application)}

    if oauth is not None:
        mount(app, oauth, limits.failures)  # must precede the catch-all MCP mount
    app.mount("/", mcp.streamable_http_app())
    return app


def create_app():
    path = os.environ.get("LAB_CONFIG", "config.local.yaml")
    config = load_config(path)
    if config.log_file:
        attach_log_file(config.log_file)
    app = build_app(config, config_path=path, background=True)
    try:
        credential = GitHub().credential_status()
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
        log.warning("GitHub credential check failed at startup: %s", explain(exc) or exc)
    else:
        if credential["warning"]:
            log.warning("%s", credential["warning"])
    return app

