"""The delivery service's server: FastAPI carrying a Streamable HTTP MCP endpoint and a small REST API, the same shape as
the infrastructure layer's (docs/DELIVERY-SPEC.md section 6). `uvicorn appdelivery.server:create_app --factory` serves
it, with APPDELIVERY_CONFIG naming the configuration."""
import contextlib
import difflib
import json
import os

import pydantic
from fastapi import FastAPI, Request
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import RequestBodyLimitMiddleware, TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from . import __version__, events, tools
from .auth import Authenticate, as_tool_error, audited, check_pepper, client_address, identity, invoke
from .catalog import CATALOG, render_markdown
from .config import load_config
from .errors import DeliveryError, ErrorCode, problems_from
from .models import BuildRequest, ReleaseRequest
from .oauth import SignIn
from .releases import Delivery
from .state import ProcessLock, StateManager

INSTRUCTIONS = (
    "Application delivery: builds an image in the trusted Google Cloud project from a commit of a linked source (build_image; images are never built or "
    "uploaded anywhere else), then runs it on machines, a Kubernetes cluster, a Cloud Run service or a job the Cloud Infrastructure service already "
    "created. You name the image by the digest a build reported (host/path@sha256:…; any other is refused), the infrastructure request and a group "
    "of its machines or its cluster with a workload name and replicas, the port, the health path, and where each environment value comes from "
    "(a literal, an endpoint of the request, or a secret of the request). A release takes no source, Dockerfile, build or command. The request must name this "
    "environment's service account in its access list as admin. Start with plan_release; deploy_release rolls out one machine at a time, or through "
    "Kubernetes on a cluster, and puts the previous release back if it fails; get_release follows it; rollback_release and remove_release act on the "
    "release running on a target. Every result carries status; a FAILED one carries error.code and error.message."
)
HTTP_STATUS = {"NOT_FOUND": 404, "PERMISSION_DENIED": 403, "INVALID_STATE": 409, "NOT_GRANTED": 409, "NOT_BUILT_HERE": 409, "BUILD_LIMIT_EXCEEDED": 429, "LIMIT_EXCEEDED": 429,
               "BUILD_UNAVAILABLE": 502, "INFRA_UNAVAILABLE": 502, "INFRA_AUTH_FAILED": 502,
               "GOOGLE_AUTH_FAILED": 502, "SECRET_UNAVAILABLE": 502, "MACHINE_UNREACHABLE": 502, "CLUSTER_UNREACHABLE": 502, "SERVICE_UNREACHABLE": 502, "INTERNAL_ERROR": 500}


def unknown_arguments(tool, valid, arguments):
    """None when the tool takes every argument given; else one sentence naming the others and what each most likely meant."""
    unknown = [name for name in arguments if name not in valid]
    if not unknown:
        return None

    def guess(name):
        near = difflib.get_close_matches(name, valid, n=1, cutoff=0.6)
        return f"{name} (did you mean {near[0]}?)" if near else name
    named = [guess(name) for name in unknown]
    listed = named[0] if len(named) == 1 else ", ".join(named[:-1]) + " and " + named[-1]
    takes = f"it takes {', '.join(valid)}" if valid else "it takes no arguments"
    return f"{tool} has no argument{'s' if len(unknown) > 1 else ''} {listed}; {takes}. Nothing was done: ask again with those names."


class StrictMCP(FastMCP):
    """Refuses an argument a tool does not take before the tool runs, and says so in the input schema; a schema refusal
    gets the same structured shape as every other error."""

    def __init__(self, *args, state=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.audit_state = state

    async def list_tools(self):
        listed = await super().list_tools()
        for tool in listed:
            tool.inputSchema = {**tool.inputSchema, "additionalProperties": False}
        return listed

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
            if not isinstance(exc.__cause__, pydantic.ValidationError):
                raise
            kind = "build" if name == "build_image" else "release"
            problems = problems_from(exc.__cause__, kind)
            first = problems[0] if problems else None
            error = DeliveryError(first.code if first else ErrorCode.INVALID_RELEASE, first.message if first else "Invalid arguments",
                                  {"tool": name, "problems": [item.model_dump(mode="json") for item in problems]})
            self._refused(name, f"{error.code.value}: {error.message}")
            return as_tool_error(error.to_dict())


def read_body(raw: bytes, code: ErrorCode = ErrorCode.INVALID_RELEASE) -> dict:
    try:
        data = json.loads(raw)
    except ValueError:
        raise DeliveryError(code, "The body is not JSON: send one JSON object", {"field": "body"}) from None
    return data if isinstance(data, dict) else {"_": data}


def build_app(config, delivery: Delivery | None = None, state: StateManager | None = None, executor=None) -> FastAPI:
    check_pepper(config)  # keyed token hashes need their pepper before anything starts
    os.umask(0o077)  # the state file, its lock and journal, the log and gcloud's files are the service's alone
    events.configure(path=config.log_file)
    state = state or (delivery.state if delivery is not None else StateManager(config.database))
    delivery = delivery or Delivery(config, state, executor=executor)
    mcp = StrictMCP("Application Delivery", state=state, instructions=INSTRUCTIONS, stateless_http=True, json_response=True,
                    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=config.allowed_hosts, allowed_origins=[]))
    mcp._mcp_server.version = __version__
    tools.register(mcp, delivery, audited(state), identity.get, __version__)
    registered = {tool.name for tool in mcp._tool_manager.list_tools()}
    if registered != {spec.name for spec in CATALOG}:
        raise RuntimeError(f"The catalog and the registered tools differ: {sorted(registered ^ {spec.name for spec in CATALOG})}")
    process_lock = ProcessLock(config.database) if config.database != ":memory:" else None

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if process_lock is not None:
            process_lock.acquire()  # one process per state file, or a second would recover the first's work as interrupted
        try:
            delivery.recover()
            delivery.builder.resume()
            try:
                async with mcp.session_manager.run():
                    yield
            finally:
                # work under way finishes before the lock goes; work not yet started is dropped and recovered at the next start.
                # Builds go on in Cloud Build: their followers stop at once and resume at the next start
                delivery.builder.stop()
                delivery.executor.shutdown(wait=True, cancel_futures=True)
        finally:
            if process_lock is not None:
                process_lock.release()

    app = FastAPI(title="Application Delivery", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.mcp, app.state.delivery, app.state.state = mcp, delivery, state
    sign_in = SignIn(config, state) if config.sign_in else None
    app.state.sign_in = sign_in
    app.add_middleware(Authenticate, config=config, state=state, sign_in=sign_in)
    app.add_middleware(RequestBodyLimitMiddleware, max_body_size=config.max_request_bytes)  # outermost: a large body is refused before anything reads it

    def rest(name, fn, subject=None):
        result = invoke(state, "rest", name, fn, subject)
        code = (result.get("error") or {}).get("code") if isinstance(result, dict) and result.get("status") == "FAILED" and result.get("error") else None
        return JSONResponse(result, status_code=HTTP_STATUS.get(code, 400) if code else 200)

    async def in_worker(name, fn, subject=None):
        """rest() off the event loop, with the caller's identity: a read of the infrastructure layer or a wait never holds it."""
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
        return rest("service_status", lambda: delivery.service_status(__version__))

    @app.get("/catalog")
    def catalog():
        return {"tools": [{"name": spec.name, "title": spec.title, "purpose": spec.purpose, "hint": spec.hint, "errors": list(spec.errors),
                           "inputs": [{"name": item.name, "required": item.required, "description": item.description} for item in spec.inputs]} for spec in CATALOG],
                "markdown": render_markdown()}

    @app.post("/builds")
    async def build(request: Request, wait_seconds: int = 0):
        raw = await request.body()
        return await in_worker("build", lambda: delivery.build(BuildRequest.model_validate(read_body(raw, ErrorCode.INVALID_BUILD)), identity.get(), wait_seconds))

    @app.get("/builds")
    def list_builds(environment: str | None = None, source: str | None = None, limit: int = 50):
        def run():
            builds = delivery.list_builds(identity.get(), environment, source, limit)
            return {"count": len(builds), "builds": [item.model_dump(mode="json") for item in builds]}
        return rest("list_builds", run)

    @app.get("/builds/{build_id}")
    async def get_build(build_id: str):
        return await in_worker("get_build", lambda: delivery.get_build(build_id, identity.get()), build_id)

    @app.post("/releases/plan")
    async def plan(request: Request):
        raw = await request.body()
        return await in_worker("plan", lambda: delivery.plan(ReleaseRequest.model_validate(read_body(raw)), identity.get()))

    @app.post("/releases")
    async def deploy(request: Request, wait_seconds: int = 0):
        raw = await request.body()
        return await in_worker("deploy", lambda: delivery.deploy(ReleaseRequest.model_validate(read_body(raw)), identity.get(), wait_seconds))

    @app.get("/releases")
    def list_releases(request_id: str | None = None, environment: str | None = None, limit: int = 50):
        def run():
            releases = delivery.list_releases(identity.get(), request_id, environment, limit)
            return {"count": len(releases), "releases": [item.model_dump(mode="json") for item in releases]}
        return rest("list", run)

    @app.get("/releases/{release_id}")
    def get_release(release_id: str):
        return rest("get", lambda: delivery.get(release_id, identity.get()), release_id)

    @app.post("/releases/{release_id}/approve")
    async def approve(release_id: str, wait_seconds: int = 0):
        return await in_worker("approve", lambda: delivery.approve(release_id, identity.get(), wait_seconds), release_id)

    @app.post("/releases/{release_id}/rollback")
    async def rollback(release_id: str, wait_seconds: int = 0):
        return await in_worker("rollback", lambda: delivery.rollback(release_id, identity.get(), wait_seconds), release_id)

    @app.post("/releases/{release_id}/runs")
    async def start_run(release_id: str, wait_seconds: int = 0):
        return await in_worker("start_run", lambda: delivery.start_run(release_id, identity.get(), wait_seconds), release_id)

    @app.get("/releases/{release_id}/runs/{execution}")
    async def get_run(release_id: str, execution: str):
        return await in_worker("get_run", lambda: delivery.get_run(release_id, execution, identity.get()), release_id)

    @app.delete("/releases/{release_id}")
    async def remove(release_id: str, wait_seconds: int = 0):
        return await in_worker("remove", lambda: delivery.remove(release_id, identity.get(), wait_seconds), release_id)

    @app.get("/running")
    async def running(request_id: str, live: bool = False):
        return await in_worker("running", lambda: delivery.running(request_id, identity.get(), live))

    if sign_in is not None:
        app.router.routes.extend(sign_in.routes())  # before the catch-all MCP mount, which would take every path
    app.mount("/", mcp.streamable_http_app())
    return app


def create_app() -> FastAPI:
    return build_app(load_config(os.environ.get("APPDELIVERY_CONFIG", "appdelivery.local.yaml")))
