import contextlib
import contextvars
import hashlib
import hmac
import os
import threading
import time
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse

from .config import load_config
from .controller import Controller, Store
from .github import GitHub

identity = contextvars.ContextVar("identity")

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
    "and never describe a check as passed unless its value is 'passed'. Requests for other clouds, production, databases, "
    "GPUs or larger resources are refused by policy; relay the refusal instead of working around it. "
    "Use estimate_cost to explain cost bounds and findings before deploying, and get_cost_report for measured right-sizing; "
    "present bounds as bounds, never as a bill forecast, and say which costs are excluded."
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


class Authenticate:
    def __init__(self, app, config):
        self.app, self.config = app, config
        self.identities = RateLimiter(config.rate_limit_per_minute)
        self.failures = RateLimiter(config.auth_failures_per_minute)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        authorization = headers.get(b"authorization", b"").decode("latin1")
        scheme, _, token = authorization.partition(" ")
        # RFC 9110 authentication schemes are case-insensitive; the token itself is compared by hash.
        token = token.strip() if scheme.lower() == "bearer" else ""
        digest = hashlib.sha256(token.encode()).hexdigest()
        principal = next((p for p in self.config.principals if token and hmac.compare_digest(digest, p.token_sha256)), None)
        if principal is None:
            # Failed logins are budgeted per client address to slow token guessing; the proxy should limit as well.
            client = (scope.get("client") or ("unknown", 0))[0]
            allowed, retry_after = self.failures.allow(client)
            if not allowed:
                return await JSONResponse({"error": "Too many failed authentication attempts"}, status_code=429,
                                          headers={"Retry-After": str(retry_after)})(scope, receive, send)
            return await JSONResponse({"error": "Authentication required"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        allowed, retry_after = self.identities.allow(principal.name)
        if not allowed:
            return await JSONResponse({"error": "Rate limit exceeded for this identity; retry later"}, status_code=429,
                                      headers={"Retry-After": str(retry_after)})(scope, receive, send)
        ctx = identity.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            identity.reset(ctx)


def build_app(config, github=None):
    controller = Controller(config, github or GitHub(), Store(config.database))
    mcp = FastMCP("Lab Deploy", instructions=INSTRUCTIONS, stateless_http=True, json_response=True,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=config.allowed_hosts, allowed_origins=[]))

    @mcp.tool(title="List applications", annotations=READ_ONLY, structured_output=True)
    def list_applications() -> dict[str, Any]:
        """List only applications permitted for the authenticated caller."""
        return {k: config.applications[k].model_dump() for k in identity.get().applications}

    @mcp.tool(title="Analyze repository", annotations=READ_ONLY, structured_output=True)
    def analyze_repository(application: str) -> dict[str, Any]:
        """Inspect approved repository metadata; return a draft with explicit assumptions."""
        return controller.inspect(identity.get(), application)

    @mcp.tool(title="Create deployment plan", annotations=CREATES_RECORD, structured_output=True)
    def create_deployment_plan(application: str, cloud: str = "gcp", environment: str = "staging") -> dict[str, Any]:
        """Validate committed platform.yaml and pin an expiring plan to a Git commit."""
        return controller.plan(identity.get(), application, cloud, environment)

    @mcp.tool(title="Propose configuration (draft PR)", annotations=CREATES_RECORD, structured_output=True)
    def propose_configuration(application: str) -> dict[str, Any]:
        """Create a draft config PR for a new app. Never merges or replaces existing platform.yaml."""
        return controller.propose_config(identity.get(), application)

    @mcp.tool(title="Deploy to staging", annotations=CHANGES_CLOUD, structured_output=True)
    def deploy_staging(plan_id: str) -> dict[str, Any]:
        """Dispatch the approved workflow for an existing plan. Changes cloud resources when enabled."""
        return controller.deploy(identity.get(), plan_id)

    @mcp.tool(title="Get deployment status", annotations=READ_ONLY, structured_output=True)
    def get_deployment_status(plan_id: str) -> dict[str, Any]:
        """Read actual GitHub workflow status. Dispatched does not mean deployed."""
        return controller.status(identity.get(), plan_id)

    @mcp.tool(title="List plans", annotations=READ_ONLY, structured_output=True)
    def list_plans() -> dict[str, Any]:
        """List this caller's own deployment plans, newest first, with their current state."""
        return controller.plans(identity.get())

    @mcp.tool(title="Estimate cost", annotations=READ_ONLY, structured_output=True)
    def estimate_cost(application: str, requests_per_month: int | None = None, average_request_seconds: float | None = None) -> dict[str, Any]:
        """Cost bounds from list prices for the committed platform.yaml, plus findings; optional traffic scenario."""
        return controller.cost(identity.get(), application, requests_per_month, average_request_seconds)

    @mcp.tool(title="Get cost report", annotations=READ_ONLY, structured_output=True)
    def get_cost_report(application: str) -> dict[str, Any]:
        """Latest measured right-sizing report from the rightsizing workflow; never estimated."""
        return controller.cost_report(identity.get(), application)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="Lab Deploy", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(Authenticate, config=config)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/applications")
    def applications():
        return list_applications()

    @app.post("/applications/{application}/plans")
    def plan(application: str):
        return controller.plan(identity.get(), application)

    @app.post("/plans/{plan_id}/deploy")
    def deploy(plan_id: str):
        return controller.deploy(identity.get(), plan_id)

    @app.get("/plans")
    def plans():
        return controller.plans(identity.get())

    @app.get("/applications/{application}/cost")
    def cost(application: str, requests_per_month: int | None = None, average_request_seconds: float | None = None):
        return controller.cost(identity.get(), application, requests_per_month, average_request_seconds)

    @app.get("/applications/{application}/cost-report")
    def cost_report(application: str):
        return controller.cost_report(identity.get(), application)

    @app.get("/plans/{plan_id}")
    def status(plan_id: str):
        return controller.status(identity.get(), plan_id)

    app.mount("/", mcp.streamable_http_app())
    return app


def create_app():
    return build_app(load_config(os.environ.get("LAB_CONFIG", "config.local.yaml")))

