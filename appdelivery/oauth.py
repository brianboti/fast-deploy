"""Sign-in for Claude's hosted connector: the service as its own small OAuth 2.1 authorization server.

claude.ai adds a remote MCP server only through OAuth or with no authentication at all; it cannot send a fixed bearer
token. So when `sign_in` is configured, the service signs a caller in on its own page: the person pastes their principal's
token there once, never into the chat, and Claude receives a short-lived access token and a rotating refresh token for that
principal. Every later call passes the same gate as the principal's own token, with the same rights. Static tokens keep
working for systems, the REST routes and Claude Code.

Nothing here contacts a third party. Clients are public (PKCE, no secret needed) and may only return to the configured
callbacks; sign-in links, codes and tokens are kept as SHA-256 hashes; a refresh token works once, and using one twice ends
that sign-in. A sign-in ends too when its principal is removed from the configuration or given a new token. The same shape
as the infrastructure layer's, written again here because this package imports nothing from it.
"""
import contextlib
import hashlib
import html
import json
import secrets
import sqlite3
import time
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_resource_metadata_url, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from .auth import RateLimiter, find_principal

CODE_SECONDS = 300  # RFC 6749 recommends at most ten minutes for an authorization code
LOGIN_SECONDS = 600  # a sign-in page left open longer than this must be opened again from Claude
SIGN_IN_PATH = "/sign-in"
PUBLIC_PATHS = frozenset({"/authorize", "/token", "/register", "/revoke", SIGN_IN_PATH})
# No form-action directive: Chrome applies it to the redirect after the POST, which would block the hop back to Claude.
PAGE_HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"}
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL, created REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS oauth_grants (digest TEXT PRIMARY KEY, kind TEXT NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS oauth_tokens (digest TEXT PRIMARY KEY, kind TEXT NOT NULL, family TEXT NOT NULL, client_id TEXT NOT NULL, "
    "expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS oauth_tokens_family ON oauth_tokens (family)",
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def is_public(path: str) -> bool:
    """The paths a caller reaches before it has a token: the metadata, the OAuth endpoints and the sign-in page."""
    return path in PUBLIC_PATHS or path.startswith("/.well-known/")


class Code(AuthorizationCode):
    credential: str  # the principal's token hash at sign-in; a new token ends the sign-in


class Refresh(RefreshToken):
    family: str
    credential: str


class Access(AccessToken):
    credential: str


class SignInStore:
    """Clients, pending sign-ins, codes and tokens, in their own tables of the service's state file. Only hashes of the
    secrets are kept; a pending sign-in and a code are taken once."""

    def __init__(self, path: str, clock=time.time):
        self.path, self.clock = path, clock
        with self.connect() as db:
            for statement in SCHEMA:
                db.execute(statement)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_client(self, client: OAuthClientInformationFull) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO oauth_clients VALUES (?, ?, ?)", (client.client_id, client.model_dump_json(), self.clock()))

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self.connect() as db:
            row = db.execute("SELECT info FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    def put_grant(self, secret: str, kind: str, lifetime: float, payload: dict) -> None:
        now = self.clock()
        with self.connect() as db:
            db.execute("DELETE FROM oauth_grants WHERE expires < ?", (now,))
            db.execute("INSERT INTO oauth_grants VALUES (?, ?, ?, ?)", (digest(secret), kind, now + lifetime, json.dumps(payload)))

    def get_grant(self, secret: str, kind: str, take: bool = False) -> dict | None:
        """A live pending sign-in or code; `take` removes it in the same transaction, so it is used at most once."""
        with self.connect() as db:
            row = db.execute("SELECT expires, payload FROM oauth_grants WHERE digest=? AND kind=?", (digest(secret), kind)).fetchone()
            if row and take:
                db.execute("DELETE FROM oauth_grants WHERE digest=?", (digest(secret),))
        return json.loads(row[1]) if row and row[0] >= self.clock() else None

    def add_token(self, token: str, kind: str, family: str, client_id: str, lifetime: float, payload: dict) -> None:
        now = self.clock()
        with self.connect() as db:
            db.execute("DELETE FROM oauth_tokens WHERE expires < ?", (now,))
            db.execute("INSERT INTO oauth_tokens VALUES (?, ?, ?, ?, ?, 0, ?)", (digest(token), kind, family, client_id, now + lifetime, json.dumps(payload)))

    def get_token(self, token: str, kind: str) -> dict | None:
        """The token's row, revoked or not, while it has not expired."""
        with self.connect() as db:
            row = db.execute("SELECT family, client_id, expires, revoked, payload FROM oauth_tokens WHERE digest=? AND kind=?", (digest(token), kind)).fetchone()
        if not row or row[2] < self.clock():
            return None
        return {"family": row[0], "client_id": row[1], "expires": row[2], "revoked": bool(row[3]), **json.loads(row[4])}

    def revoke_token(self, token: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE oauth_tokens SET revoked=1 WHERE digest=?", (digest(token),))

    def revoke_family(self, family: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE oauth_tokens SET revoked=1 WHERE family=?", (family,))


class SignIn:
    """The authorization server the MCP SDK's OAuth routes call, plus the sign-in page and the token lookup the request
    gate uses. Every sign-in, refusal and ended session is an audit event."""

    def __init__(self, config, state=None, store: SignInStore | None = None):
        self.config, self.settings, self.audit = config, config.sign_in, state
        self.store = store or SignInStore(config.database)
        self.issuer = self.settings.url.rstrip("/")
        self.resource = self.issuer + "/mcp"
        self.metadata_url = str(build_resource_metadata_url(AnyHttpUrl(self.resource)))
        self.failures = RateLimiter(config.auth_failures_per_minute)

    is_public = staticmethod(is_public)

    def note(self, kind, identity=None, client=None, outcome=None, detail=None):
        if self.audit is not None:
            self.audit.record_event(kind, name="sign_in", identity=identity, client=client, outcome=outcome, detail=detail)

    def current(self, name: str, credential: str):
        """The principal a sign-in was made for, while it is configured with the same token."""
        return next((principal for principal in self.config.principals if principal.name == name and principal.token_sha256 == credential), None)

    def principal_for(self, token: str):
        """The principal behind a live access token, or None; the request gate calls this when no static token matched."""
        row = self.store.get_token(token, "access")
        return None if row is None or row["revoked"] else self.current(row["subject"], row["credential"])

    # ---- the SDK's OAuthAuthorizationServerProvider
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await run_in_threadpool(self.store.get_client, client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        refused = [str(uri) for uri in client_info.redirect_uris or [] if str(uri) not in self.settings.callbacks]
        if refused or not client_info.redirect_uris:
            self.note("oauth_register", outcome="refused", detail=f"callbacks not allowed: {refused}"[:500])
            raise RegistrationError("invalid_redirect_uri", f"This service returns only to {', '.join(self.settings.callbacks)}")
        await run_in_threadpool(self.store.add_client, client_info)
        self.note("oauth_register", outcome="ok", detail=f"{client_info.client_name or 'unnamed'} {client_info.client_id}"[:500])

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        login = secrets.token_urlsafe(32)
        payload = {"client_id": client.client_id, "client_name": client.client_name, "params": params.model_dump(mode="json")}
        await run_in_threadpool(self.store.put_grant, login, "login", LOGIN_SECONDS, payload)
        return f"{self.issuer}{SIGN_IN_PATH}?login={login}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> Code | None:
        found = await run_in_threadpool(self.store.get_grant, authorization_code, "code")
        return Code(code=authorization_code, **found) if found and found["client_id"] == client.client_id else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: Code) -> OAuthToken:
        if await run_in_threadpool(self.store.get_grant, authorization_code.code, "code", True) is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return await run_in_threadpool(self.issue, client.client_id, authorization_code.subject, authorization_code.credential, authorization_code.scopes,
                                       authorization_code.resource, secrets.token_hex(16))

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> Refresh | None:
        row = await run_in_threadpool(self.store.get_token, refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        if row["revoked"]:
            # a refresh token works once: a second use means it was copied, so the whole sign-in ends
            await run_in_threadpool(self.store.revoke_family, row["family"])
            self.note("token_reuse", identity=row["subject"], outcome="session_revoked", detail=f"client {client.client_id}")
            return None
        return Refresh(token=refresh_token, client_id=row["client_id"], scopes=row["scopes"], expires_at=int(row["expires"]), resource=row["resource"],
                       subject=row["subject"], family=row["family"], credential=row["credential"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: Refresh, scopes: list[str]) -> OAuthToken:
        if self.current(refresh_token.subject, refresh_token.credential) is None:
            await run_in_threadpool(self.store.revoke_family, refresh_token.family)
            raise TokenError("invalid_grant", "the principal of this sign-in was removed or given a new token; sign in again")
        await run_in_threadpool(self.store.revoke_token, refresh_token.token)  # rotation: the old one is now a reuse signal
        return await run_in_threadpool(self.issue, client.client_id, refresh_token.subject, refresh_token.credential, scopes or refresh_token.scopes,
                                       refresh_token.resource, refresh_token.family)

    async def load_access_token(self, token: str) -> Access | None:
        row = await run_in_threadpool(self.store.get_token, token, "access")
        if row is None or row["revoked"]:
            return None
        return Access(token=token, client_id=row["client_id"], scopes=row["scopes"], expires_at=int(row["expires"]), resource=row["resource"],
                      subject=row["subject"], credential=row["credential"])

    async def revoke_token(self, token: Access | Refresh) -> None:
        kind = "refresh" if isinstance(token, Refresh) else "access"
        row = await run_in_threadpool(self.store.get_token, token.token, kind)
        if row is not None:
            await run_in_threadpool(self.store.revoke_family, row["family"])
            self.note("sign_out", identity=row["subject"], outcome="ok", detail=f"client {row['client_id']}")

    def issue(self, client_id, subject, credential, scopes, resource, family) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        payload = {"subject": subject, "credential": credential, "scopes": scopes, "resource": resource}
        self.store.add_token(access, "access", family, client_id, self.settings.access_token_minutes * 60, payload)
        self.store.add_token(refresh, "refresh", family, client_id, self.settings.refresh_token_days * 86_400, payload)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=self.settings.access_token_minutes * 60, refresh_token=refresh,  # noqa: S106
                          scope=" ".join(scopes) or None)

    # ---- the sign-in page
    def page(self, login: str, pending: dict, problem: str | None = None, status_code: int = 200) -> HTMLResponse:
        client = html.escape(pending.get("client_name") or "an MCP client")
        returns = html.escape(urlparse(pending["params"]["redirect_uri"]).netloc)
        error = f'<p class="error">{html.escape(problem)}</p>' if problem else ""
        body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in: Application Delivery</title><style>body{{font:16px system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem;color:#1f2328}}
input,button{{font:inherit;width:100%;box-sizing:border-box;padding:.6rem;margin:.4rem 0}}button{{cursor:pointer}}.error{{color:#b42318}}small{{color:#59636e}}</style></head>
<body><h1>Sign in</h1><p><strong>{client}</strong> asks to use Application Delivery as you. Paste your token. It stays here; afterwards you return to
<strong>{returns}</strong>.</p>{error}<form method="post" action="{SIGN_IN_PATH}"><input type="hidden" name="login" value="{html.escape(login)}">
<label for="token">Your token</label><input id="token" name="token" type="password" autocomplete="off" required autofocus>
<button type="submit">Sign in</button></form><small>The link works for ten minutes. Close this page to refuse.</small></body></html>"""
        return HTMLResponse(body, status_code=status_code, headers=PAGE_HEADERS)

    def expired(self) -> HTMLResponse:
        return HTMLResponse("<!doctype html><title>Sign-in expired</title><p>This sign-in link has expired or was already used. Connect again from Claude.</p>",
                            status_code=400, headers=PAGE_HEADERS)

    async def show(self, request: Request):
        login = request.query_params.get("login", "")
        pending = await run_in_threadpool(self.store.get_grant, login, "login")
        return self.page(login, pending) if pending else self.expired()

    async def submit(self, request: Request):
        address = request.client.host if request.client else "unknown"
        form = await request.form()
        login, token = str(form.get("login", "")), str(form.get("token", "")).strip()
        pending = await run_in_threadpool(self.store.get_grant, login, "login")
        if pending is None:
            return self.expired()
        principal = find_principal(self.config, token)
        if principal is None:
            allowed, retry_after = self.failures.allow(address)
            self.note("sign_in", client=address, outcome="refused" if allowed else "throttled", detail=f"client {pending['client_id']}")
            if not allowed:
                return HTMLResponse("<!doctype html><title>Too many attempts</title><p>Too many wrong tokens. Wait a minute, then try again.</p>",
                                    status_code=429, headers={**PAGE_HEADERS, "Retry-After": str(retry_after)})
            return self.page(login, pending, "That token is not one of this service's principals.", 401)
        if await run_in_threadpool(self.store.get_grant, login, "login", True) is None:
            return self.expired()
        params = AuthorizationParams.model_validate(pending["params"])
        code = secrets.token_urlsafe(32)
        record = {"scopes": params.scopes or [], "expires_at": time.time() + CODE_SECONDS, "client_id": pending["client_id"], "code_challenge": params.code_challenge,
                  "redirect_uri": str(params.redirect_uri), "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly, "resource": params.resource,
                  "subject": principal.name, "credential": principal.token_sha256}
        await run_in_threadpool(self.store.put_grant, code, "code", CODE_SECONDS, record)
        self.note("sign_in", identity=principal.name, client=address, outcome="ok", detail=f"{pending.get('client_name') or 'unnamed'} {pending['client_id']}"[:500])
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), status_code=302, headers={"Cache-Control": "no-store"})

    def routes(self) -> list[Route]:
        """The SDK's metadata, authorize, token, register and revoke routes, the protected resource metadata (at the path
        RFC 9728 derives from /mcp and at the root, which some clients ask first), and the sign-in page."""
        issuer = AnyHttpUrl(self.issuer)
        resource = create_protected_resource_routes(AnyHttpUrl(self.resource), [issuer], resource_name="Application Delivery")
        root = [Route("/.well-known/oauth-protected-resource", endpoint=resource[0].endpoint, methods=["GET", "OPTIONS"])]
        return [*create_auth_routes(self, issuer, client_registration_options=ClientRegistrationOptions(enabled=True), revocation_options=RevocationOptions(enabled=True)),
                *resource, *root, Route(SIGN_IN_PATH, endpoint=self.show, methods=["GET"]), Route(SIGN_IN_PATH, endpoint=self.submit, methods=["POST"])]
