"""Who may call the service: bearer tokens kept as hashes, per-identity and per-address budgets, one audit event per call.

The identity comes from the credential, never from a parameter. The same shape as the infrastructure layer's, written
again here because this package imports nothing from it.
"""
import contextvars
import functools
import hashlib
import hmac
import json
import threading
import time

import pydantic
from mcp.types import CallToolResult, TextContent
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .errors import DeliveryError, ErrorCode, problems_from
from .events import bound, event

identity = contextvars.ContextVar("appdelivery_principal")
client_address = contextvars.ContextVar("appdelivery_client", default="unknown")


def find_principal(config, token: str):
    """The configured principal whose token this is, or None; hashes compared in constant time."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    return next((principal for principal in config.principals if token and hmac.compare_digest(digest, principal.token_sha256)), None)


class RateLimiter:
    """Fixed-window counter per key, in process: the service runs as one process, like its state."""

    def __init__(self, limit, window_seconds=60, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window_seconds, clock
        self.lock = threading.Lock()
        self.windows = {}

    def allow(self, key):
        now = self.clock()
        with self.lock:
            start, count = self.windows.get(key, (now, 0))
            if now - start >= self.window:
                start, count = now, 0
            if count >= self.limit:
                return False, max(1, int(self.window - (now - start)) + 1)
            self.windows[key] = (start, count + 1)
            if len(self.windows) > 10_000:
                self.windows = {key: value for key, value in self.windows.items() if now - value[0] < self.window}
            return True, 0


class Authenticate:
    """ASGI gate: /healthz is public; everything else needs the bearer token of a configured principal."""

    def __init__(self, app, config, state=None):
        self.app, self.config, self.state = app, config, state
        self.identities = RateLimiter(config.rate_limit_per_minute)
        self.failures = RateLimiter(config.auth_failures_per_minute)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)
        client = (scope.get("client") or ("unknown", 0))[0]
        address = client_address.set(client)
        try:
            headers = dict(scope["headers"])
            scheme, _, token = headers.get(b"authorization", b"").decode("latin1").partition(" ")
            principal = find_principal(self.config, token.strip()) if scheme.lower() == "bearer" else None
            if principal is None:
                allowed, retry_after = self.failures.allow(client)
                if not allowed:
                    if self.state is not None:
                        self.state.record_event("auth_burst", client=client, outcome="throttled", detail=scope["path"])
                    return await JSONResponse({"status": "FAILED", "error": {"code": "PERMISSION_DENIED", "message": "Too many failed authentication attempts"}},
                                              status_code=429, headers={"Retry-After": str(retry_after)})(scope, receive, send)
                return await JSONResponse({"status": "FAILED", "error": {"code": "PERMISSION_DENIED", "message": "Authentication required"}},
                                          status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            allowed, retry_after = self.identities.allow(principal.name)
            if not allowed:
                return await JSONResponse({"status": "FAILED", "error": {"code": "PERMISSION_DENIED", "message": "Rate limit exceeded for this identity; retry later"}},
                                          status_code=429, headers={"Retry-After": str(retry_after)})(scope, receive, send)
            context = identity.set(principal)
            try:
                await self.app(scope, receive, send)
            finally:
                identity.reset(context)
        finally:
            client_address.reset(address)


def invoke(state, kind: str, name: str, fn, subject=None):
    """Run one call for the authenticated identity: structured errors out, one audit event in, with outcome and duration."""
    started = time.monotonic()
    principal = identity.get(None)
    who = principal.name if principal is not None else None

    def note(outcome, detail=None):
        if state is not None:
            state.record_event(kind, name=name, identity=who, client=client_address.get(), release_id=subject, outcome=outcome,
                               duration_ms=int((time.monotonic() - started) * 1000), detail=detail)
    with bound(identity=who):
        try:
            result = fn()
        except DeliveryError as exc:
            note("error", f"{exc.code.value}: {exc.message}"[:500])
            return exc.to_dict()
        except pydantic.ValidationError as exc:
            problems = problems_from(exc)
            first = problems[0] if problems else None
            error = DeliveryError(first.code if first else ErrorCode.INVALID_RELEASE, first.message if first else "Invalid release",
                                  {"problems": [item.model_dump(mode="json") for item in problems]})
            note("error", f"{error.code.value}: {error.message}"[:500])
            return error.to_dict()
        except Exception as exc:
            error = DeliveryError(ErrorCode.INTERNAL_ERROR, f"The service stopped on an unexpected {type(exc).__name__}; nothing was retried. Check the release before asking again",
                                  {"exception": type(exc).__name__})
            event("internal_error", exception=type(exc).__name__, tool=name)
            note("error", f"INTERNAL_ERROR: {type(exc).__name__}")
            return error.to_dict()
    note("ok")
    return result.model_dump(mode="json") if hasattr(result, "model_dump") else result


def failed(result) -> bool:
    return isinstance(result, dict) and result.get("status") == "FAILED" and bool(result.get("error"))


def as_tool_error(result: dict) -> CallToolResult:
    """A FAILED result as the MCP specification wants an execution error: isError, the structured error kept, and the same
    JSON as text, so a client that reads either gets the code and the message."""
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, indent=1))], structuredContent=result, isError=True)


def audited(state):
    """Decorator for MCP tools: the call runs in the thread pool with the caller's identity and is recorded; a FAILED result
    is returned as an execution error with its structured payload."""
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            subject = kwargs.get("release_id")
            principal, client = identity.get(None), client_address.get(None)

            def call():
                identity.set(principal)
                client_address.set(client)
                return invoke(state, "tool", fn.__name__, lambda: fn(*args, **kwargs), subject if isinstance(subject, str) else None)
            result = await run_in_threadpool(call)
            return as_tool_error(result) if failed(result) else result
        return wrapper
    return decorate
