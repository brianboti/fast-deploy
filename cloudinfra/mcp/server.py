"""The Cloud Infrastructure MCP server: FastAPI carrying a Streamable HTTP MCP endpoint and a small REST API.

Starts without GitHub, without a repository and without an application: a configuration file with principals and a
provider project is all it needs. `python -m uvicorn cloudinfra.mcp.server:create_app --factory` serves it.
"""
import contextlib
import difflib
import json
import os
import re
from typing import Literal

import pydantic
from fastapi import FastAPI, Request
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .. import __version__
from ..config import load_config
from ..models.errors import ErrorCode, InfrastructureError, problems_from
from ..models.request import InfrastructureRequest
from ..observability import logging as observability
from ..orchestration.provisioner import Orchestrator
from ..orchestration.sweeper import Sweeper
from ..providers import ProviderRegistry
from ..security.auth import Authenticate, as_tool_error, audited, client_address, identity, invoke
from ..security.oauth import SignIn
from ..state.state_manager import ProcessLock, StateManager
from .catalog import CATALOG, render_markdown
from .tools import budget, cache, cluster, compute, data, database, dns, infrastructure, job, loadbalancer, monitor, network, operations, queue, secret, service, storage
from .tools import identity as identity_kind  # the kind's tools; `identity` is the caller's

INSTRUCTIONS = (
    "Cloud Infrastructure MCP: a provider-independent infrastructure service. You ask for what infrastructure is needed (machines by CPU, memory and GPU, "
    "disks and buckets by size, a private network, a managed database, a cache, queues, secrets, a Kubernetes cluster, a managed container service, a managed "
    "job, a load balancer) in a provider-neutral "
    "request; list_resource_kinds says exactly what can be asked for; the service validates it, plans it, provisions it "
    "through the provider adapter and reports resource ids, addresses, endpoints and status. It takes no source code, image, repository, script or "
    "command, and decides nothing about an application. Start with validate_infrastructure_request or plan_infrastructure; provision_infrastructure "
    "answers at once with PROVISIONING and get_infrastructure_status follows it; destroy_infrastructure removes everything a request created. "
    "control_infrastructure stops, starts or extends a request, and manage_infrastructure_access grants people access to what it created; each resource's "
    "connect block says how to reach it. Every result carries status; a FAILED one carries error.code and error.message. Costs are estimates from list prices, "
    "never a bill."
)
HTTP_STATUS = {"NOT_FOUND": 404, "PERMISSION_DENIED": 403, "INVALID_STATE": 409, "PROVIDER_CONFLICT": 409, "QUOTA_EXCEEDED": 429, "PROVIDER_UNAVAILABLE": 502,
               "PROVIDER_ERROR": 502, "PROVIDER_AUTH_FAILED": 502, "PROVIDER_PERMISSION_DENIED": 502, "OPERATION_TIMEOUT": 504, "INTERNAL_ERROR": 500}
_ALLOWED_HOST = re.compile(r"^[A-Za-z0-9.*:\[\]-]+$")


def closest(name, choices):
    near = difflib.get_close_matches(name, choices, n=1, cutoff=0.6)
    stem = name.lower().split("_")[0]
    return near[0] if near else next((choice for choice in choices if len(stem) >= 3 and choice.startswith(stem)), None)


def unknown_arguments(tool, valid, arguments):
    """None when the tool takes every argument given; else one sentence naming the others and what each most likely meant."""
    unknown = [name for name in arguments if name not in valid]
    if not unknown:
        return None
    named = [f"{name} (did you mean {guess}?)" if (guess := closest(name, valid)) else name for name in unknown]
    listed = named[0] if len(named) == 1 else ", ".join(named[:-1]) + " and " + named[-1]
    takes = f"it takes {', '.join(valid)}" if valid else "it takes no arguments"
    return f"{tool} has no argument{'s' if len(unknown) > 1 else ''} {listed}; {takes}. Nothing was done: ask again with those names."


class ControlBody(pydantic.BaseModel):
    """POST /infrastructure/{request_id}/actions"""
    model_config = pydantic.ConfigDict(extra="forbid", strict=True)
    action: Literal["stop", "start", "extend", "keep_awake"]
    hours: int | None = None
    wait_seconds: int = pydantic.Field(default=0, ge=0, le=45)


class AccessBody(pydantic.BaseModel):
    """POST /infrastructure/{request_id}/access"""
    model_config = pydantic.ConfigDict(extra="forbid", strict=True)
    action: Literal["grant", "revoke"]
    member: str
    level: Literal["use", "admin"] = "use"


def read_body(raw: bytes) -> dict:
    """A REST body as data, read inside the call: one that is not JSON is INVALID_REQUEST, recorded like any other refusal."""
    try:
        data = json.loads(raw)
    except ValueError:
        raise InfrastructureError(ErrorCode.INVALID_REQUEST, "The body is not JSON: send one JSON object", {"field": "body"}) from None
    return data if isinstance(data, dict) else {"_": data}


def body_of_model(model, body: dict):
    """A small REST body as its model; a wrong one is INVALID_REQUEST naming the fields it takes."""
    try:
        return model.model_validate(body)
    except pydantic.ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(part) for part in item['loc']) or 'body'}: {item['msg']}" for item in exc.errors())
        raise InfrastructureError(ErrorCode.INVALID_REQUEST, f"The body takes {', '.join(model.model_fields)}. {problems}", {"fields": list(model.model_fields)}) from None


class StrictMCP(FastMCP):
    """Refuses an argument a tool does not take before the tool runs, and says so in the input schema."""

    def __init__(self, *args, state=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.audit_state = state

    async def list_tools(self):
        tools = await super().list_tools()
        for listed in tools:
            listed.inputSchema = {**listed.inputSchema, "additionalProperties": False}
        return tools

    def _refused(self, name, detail):
        if self.audit_state is not None:
            principal = identity.get(None)
            self.audit_state.record_event("tool", name=name, identity=principal.name if principal is not None else None, client=client_address.get(), outcome="error",
                                          duration_ms=0, detail=detail[:500])

    async def call_tool(self, name, arguments):
        tool = self._tool_manager.get_tool(name)
        problem = tool and unknown_arguments(name, list(tool.parameters.get("properties", {})), arguments or {})
        if problem:
            self._refused(name, problem)
            raise ToolError(problem)
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            # The SDK validates the arguments against the tool's schema before the tool runs; that refusal gets the same
            # structured shape as every other, so a caller never has to parse a sentence.
            if not isinstance(exc.__cause__, pydantic.ValidationError):
                raise
            problems = problems_from(exc.__cause__)
            first = problems[0] if problems else None
            error = InfrastructureError(first.code if first else ErrorCode.INVALID_REQUEST, first.message if first else "Invalid arguments",
                                        {"tool": name, "problems": [item.model_dump(mode="json") for item in problems]})
            self._refused(name, f"{error.code.value}: {error.message}")
            return as_tool_error(error.to_dict())


def build_app(config, registry: ProviderRegistry | None = None, state: StateManager | None = None, executor=None) -> FastAPI:
    observability.configure(path=config.log_file)
    state = state or StateManager(config.database)
    registry = registry or ProviderRegistry(config)
    orchestrator = Orchestrator(config, state, registry, executor)
    sweeper = Sweeper(orchestrator, config.sweep_interval_seconds)
    mcp = StrictMCP("Cloud Infrastructure", state=state, instructions=INSTRUCTIONS, stateless_http=True, json_response=True,
                    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=config.allowed_hosts, allowed_origins=[]))
    mcp._mcp_server.version = __version__
    wrap = audited(state)
    for module in (identity_kind, compute, storage, network, database, cache, queue, data, secret, cluster, service, job, loadbalancer, dns, monitor, budget, infrastructure, operations):
        module.tools(mcp, orchestrator, wrap, identity.get)
    registered = {tool.name for tool in mcp._tool_manager.list_tools()}
    missing = {spec.name for spec in CATALOG} ^ registered
    if missing:
        raise RuntimeError(f"The catalog and the registered tools differ: {sorted(missing)}")

    process_lock = ProcessLock(config.database)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        process_lock.acquire()  # one process per state file, or a second would recover the first's work as interrupted
        try:
            orchestrator.recover()  # what a restart interrupted is put where it can be retried or destroyed
            sweeper.start()  # expiry and idle stop; off when sweep_interval_seconds is 0
            try:
                async with mcp.session_manager.run():
                    yield
            finally:
                sweeper.stop()
                # work under way finishes before the lock goes, so the next process never recovers it while it runs; work not
                # yet started is dropped, and its request is recovered at the next start
                orchestrator.executor.shutdown(wait=True, cancel_futures=True)
        finally:
            process_lock.release()

    app = FastAPI(title="Cloud Infrastructure MCP", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.mcp, app.state.orchestrator, app.state.state, app.state.sweeper = mcp, orchestrator, state, sweeper
    sign_in = SignIn(config, state) if config.sign_in else None
    app.state.sign_in = sign_in
    app.add_middleware(Authenticate, config=config, state=state, sign_in=sign_in)

    def rest(name, fn, subject=None):
        result = invoke(state, "rest", name, fn, subject)
        code = (result.get("error") or {}).get("code") if isinstance(result, dict) and result.get("status") == "FAILED" and "error" in result else None
        return JSONResponse(result, status_code=HTTP_STATUS.get(code, 400) if code else 200)

    async def rest_in_worker(name, fn, subject=None):
        """rest() for the async routes: the call runs in the thread pool with the caller's identity, so a provider call, a
        lock or a wait never holds the event loop that serves every other caller."""
        principal, client = identity.get(None), client_address.get(None)

        def call():
            identity.set(principal)
            client_address.set(client)
            return rest(name, fn, subject)
        return await run_in_threadpool(call)

    @app.get("/healthz")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/status")
    def service_status():
        return rest("service_status", lambda: orchestrator.service_status(__version__))

    @app.get("/catalog")
    def catalog():
        return {"tools": [{"name": spec.name, "group": spec.group, "title": spec.title, "purpose": spec.purpose, "hint": spec.hint,
                           "inputs": [{"name": item.name, "required": item.required, "description": item.description} for item in spec.inputs],
                           "output": spec.output, "errors": list(spec.errors), "security": spec.security, "mapping": spec.mapping} for spec in CATALOG],
                "markdown": render_markdown()}

    # The POST routes read the raw body and parse it inside the call, so a bad one is a structured error, not a 422 or a 500.
    @app.post("/infrastructure/validate")
    async def validate(request: Request):
        raw = await request.body()
        return await rest_in_worker("validate", lambda: orchestrator.validate(InfrastructureRequest.model_validate(read_body(raw)), identity.get()))

    @app.post("/infrastructure/plan")
    async def plan(request: Request, check_quota: bool = False):
        raw = await request.body()
        return await rest_in_worker("plan", lambda: orchestrator.plan(InfrastructureRequest.model_validate(read_body(raw)), identity.get(), check_quota))

    @app.post("/infrastructure")
    async def provision(request: Request, wait_seconds: int = 0):
        raw = await request.body()
        return await rest_in_worker("provision", lambda: orchestrator.provision(InfrastructureRequest.model_validate(read_body(raw)), identity.get(), wait_seconds))

    @app.get("/infrastructure")
    def list_requests(status: str | None = None, limit: int = 50, expiring_within_hours: int | None = None):
        return rest("list", lambda: orchestrator.list_requests(identity.get(), status, limit, expiring_within_hours))

    @app.get("/infrastructure/{request_id}")
    def status(request_id: str, wait_seconds: int = 0):
        return rest("status", lambda: orchestrator.status(request_id, identity.get(), refresh=True, wait_seconds=wait_seconds), request_id)

    @app.delete("/infrastructure/{request_id}")
    def destroy(request_id: str, wait_seconds: int = 0):
        return rest("destroy", lambda: orchestrator.destroy(request_id, identity.get(), wait_seconds), request_id)

    @app.post("/infrastructure/{request_id}/actions")
    async def control(request_id: str, request: Request):
        raw = await request.body()

        def run():
            asked = body_of_model(ControlBody, read_body(raw))
            return orchestrator.control(request_id, asked.action, identity.get(), asked.hours, asked.wait_seconds)
        return await rest_in_worker("control", run, request_id)

    @app.post("/infrastructure/{request_id}/access")
    async def access(request_id: str, request: Request):
        raw = await request.body()

        def run():
            asked = body_of_model(AccessBody, read_body(raw))
            return orchestrator.manage_access(request_id, asked.action, asked.member, identity.get(), asked.level)
        return await rest_in_worker("access", run, request_id)

    if sign_in is not None:
        app.router.routes.extend(sign_in.routes())  # before the catch-all MCP mount, which would take every path
    app.mount("/", mcp.streamable_http_app())
    return app


def create_app() -> FastAPI:
    path = os.environ.get("CLOUDINFRA_CONFIG", "cloudinfra.local.yaml")
    return build_app(load_config(path))
