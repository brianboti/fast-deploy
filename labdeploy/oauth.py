"""OAuth 2.1 sign-in for Claude's hosted connector.

claude.ai can only add a fixed bearer header for organizations in a limited beta, so the controller can act as its
own small authorization server instead: the operator signs in once in the browser with the existing operator token,
Claude receives a short-lived access token plus a rotating refresh token, and every later call passes through the
same request gate as a static token. Static bearer tokens keep working for Claude Code, the REST routes and the
walkthrough. Nothing here contacts a third party; codes and tokens are stored only as SHA-256 hashes.
"""
import hashlib
import hmac
import html
import json
import secrets
import time
import uuid
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_resource_metadata_url, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

# Endpoints Claude must reach before it has a token; the request gate rate limits them per client address instead.
PUBLIC_PATHS = frozenset({"/authorize", "/token", "/register", "/revoke", "/login"})
CODE_SECONDS = 300  # RFC 6749 recommends at most ten minutes for an authorization code
LOGIN_SECONDS = 600  # a sign-in page left open longer than this must be reopened from Claude
# No form-action directive: Chrome applies it to the redirect after the POST, which would block the hop back to Claude.
PAGE_HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"}
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lab Deploy sign-in</title><style>body{font-family:system-ui,sans-serif;max-width:28rem;margin:4rem auto;padding:0 1rem;color:#222}
input{width:100%;box-sizing:border-box;padding:.5rem;font-size:1rem;margin:.5rem 0 1rem}button{padding:.5rem 1rem;font-size:1rem}.error{color:#b00020}</style></head>
<body><h1>Lab Deploy</h1><p><strong>__CLIENT__</strong> is asking to use Lab Deploy as you. After sign-in you return to <strong>__HOST__</strong>.</p>
__ERROR__<form method="post" action="/login"><input type="hidden" name="request" value="__REQUEST__">
<label for="token">Operator token</label><input id="token" name="token" type="password" autocomplete="off" required autofocus>
<button type="submit">Sign in</button></form><p>The token is checked on this server only and is never shown to Claude.</p></body></html>"""


def normalize(url):
    return str(AnyHttpUrl(url)).rstrip("/")


def redirect_allowed(uri, allowed):
    """Exact match for registered callbacks; loopback callbacks match on any port, as RFC 8252 requires."""
    if uri in allowed:
        return True
    parsed = urlparse(uri)
    for entry in allowed:
        expected = urlparse(entry)
        if expected.scheme == "http" and expected.hostname in ("localhost", "127.0.0.1") and (parsed.scheme, parsed.hostname, parsed.path) == (expected.scheme, expected.hostname, expected.path):
            return True
    return False


class Code(AuthorizationCode):
    credential: str  # SHA-256 of the operator token used to sign in; rotating that token ends the session


class Refresh(RefreshToken):
    credential: str
    family: str


class OAuthStore:
    """Clients, pending sign-ins, codes and tokens in the controller's SQLite file. Secrets are stored only as hashes."""

    def __init__(self, store):
        self.connect = store.connect
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS oauth_clients (id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS oauth_logins (id TEXT PRIMARY KEY, expires REAL NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS oauth_codes (digest TEXT PRIMARY KEY, expires REAL NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS oauth_tokens (digest TEXT PRIMARY KEY, kind TEXT NOT NULL, family TEXT NOT NULL, client_id TEXT NOT NULL, "
                       "expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL)")

    @staticmethod
    def digest(secret):
        return hashlib.sha256(secret.encode()).hexdigest()

    def add_client(self, client_id, payload):
        with self.connect() as db:
            db.execute("INSERT INTO oauth_clients VALUES (?, ?, ?)", (client_id, time.time(), payload))

    def client(self, client_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM oauth_clients WHERE id=?", (client_id,)).fetchone()
        return row[0] if row else None

    def add_login(self, login_id, expires, payload):
        with self.connect() as db:
            db.execute("INSERT INTO oauth_logins VALUES (?, ?, ?)", (login_id, expires, json.dumps(payload)))

    def login(self, login_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM oauth_logins WHERE id=? AND expires > ?", (login_id, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def delete_login(self, login_id):
        with self.connect() as db:
            db.execute("DELETE FROM oauth_logins WHERE id=?", (login_id,))

    def add_code(self, code, expires, payload):
        with self.connect() as db:
            db.execute("INSERT INTO oauth_codes VALUES (?, ?, ?)", (self.digest(code), expires, json.dumps(payload)))

    def pop_code(self, code):
        """Single use: a code is deleted when first presented, so a failed exchange burns it."""
        with self.connect() as db:
            row = db.execute("SELECT payload FROM oauth_codes WHERE digest=? AND expires > ?", (self.digest(code), time.time())).fetchone()
            db.execute("DELETE FROM oauth_codes WHERE digest=?", (self.digest(code),))
        return json.loads(row[0]) if row else None

    def add_token(self, token, kind, family, client_id, expires, payload):
        with self.connect() as db:
            db.execute("INSERT INTO oauth_tokens VALUES (?, ?, ?, ?, ?, 0, ?)", (self.digest(token), kind, family, client_id, expires, json.dumps(payload)))

    def token(self, token):
        with self.connect() as db:
            row = db.execute("SELECT kind, family, client_id, expires, revoked, payload FROM oauth_tokens WHERE digest=?", (self.digest(token),)).fetchone()
        if row is None:
            return None
        return {"kind": row[0], "family": row[1], "client_id": row[2], "expires": row[3], "revoked": bool(row[4]), "payload": json.loads(row[5])}

    def revoke(self, token):
        with self.connect() as db:
            db.execute("UPDATE oauth_tokens SET revoked=1 WHERE digest=?", (self.digest(token),))

    def revoke_family(self, family):
        with self.connect() as db:
            db.execute("UPDATE oauth_tokens SET revoked=1 WHERE family=?", (family,))

    def sessions(self):
        """One row per sign-in session (token family): who, which client, when issued, when the refresh token expires, revoked or live."""
        with self.connect() as db:
            rows = db.execute("SELECT family, client_id, MIN(expires), MAX(expires), MAX(revoked), payload FROM oauth_tokens GROUP BY family ORDER BY MIN(rowid) DESC").fetchall()
            names = {row[0]: json.loads(row[1]).get("client_name") for row in db.execute("SELECT id, payload FROM oauth_clients").fetchall()}
        result = []
        for family, client_id, _, refresh_expires, revoked, payload in rows:
            subject = json.loads(payload).get("subject")
            result.append({"session": family, "identity": subject, "client": names.get(client_id) or client_id, "refresh_expires": refresh_expires,
                           "state": "revoked" if revoked else ("expired" if refresh_expires < time.time() else "live")})
        return result

    def prune(self, now):
        with self.connect() as db:
            db.execute("DELETE FROM oauth_logins WHERE expires < ?", (now,))
            db.execute("DELETE FROM oauth_codes WHERE expires < ?", (now,))
            db.execute("DELETE FROM oauth_tokens WHERE expires < ?", (now - 86_400,))
            db.execute("DELETE FROM oauth_clients WHERE created < ? AND id NOT IN (SELECT client_id FROM oauth_tokens)", (now - 90 * 86_400,))


class Provider:
    """The MCP SDK's authorization-server provider protocol, with the operator token as the only credential."""

    def __init__(self, config, store):
        self.config, self.oauth = config, config.oauth
        self.store = OAuthStore(store)
        self.audit = store  # the controller's event trail: sign-ins, registrations, reuse detection, revocations
        self.issuer = normalize(self.oauth.public_url)
        self.resource = self.issuer + "/mcp"
        self.metadata_url = str(build_resource_metadata_url(AnyHttpUrl(self.resource)))

    def principal(self, subject, credential):
        """The configured principal that signed in, or None once its token was rotated or the principal removed."""
        return next((p for p in self.config.principals if p.name == subject and hmac.compare_digest(p.token_sha256, credential)), None)

    def login(self, token):
        digest = self.store.digest(token)
        return next((p for p in self.config.principals if token and hmac.compare_digest(digest, p.token_sha256)), None)

    def authenticate(self, token):
        """Synchronous lookup for the request gate: the principal behind a live access token, or None."""
        row = self.store.token(token)
        if row is None or row["kind"] != "access" or row["revoked"] or row["expires"] < time.time():
            return None
        return self.principal(row["payload"]["subject"], row["payload"]["credential"])

    def issue(self, client_id, subject, credential, scopes, resource, family):
        now = time.time()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        payload = {"client_id": client_id, "subject": subject, "credential": credential, "scopes": scopes, "resource": resource}
        self.store.add_token(access, "access", family, client_id, now + self.oauth.access_token_seconds, payload)
        self.store.add_token(refresh, "refresh", family, client_id, now + self.oauth.refresh_token_days * 86_400, payload)
        return OAuthToken(access_token=access, expires_in=self.oauth.access_token_seconds, refresh_token=refresh, scope=" ".join(scopes) or None)

    def complete_login(self, login, principal):
        """Issue a single-use authorization code for a verified sign-in and return the client's redirect URL."""
        params = AuthorizationParams.model_validate(login["params"])
        code = secrets.token_urlsafe(32)
        record = Code(code=code, scopes=params.scopes or [], expires_at=time.time() + CODE_SECONDS, client_id=login["client_id"], code_challenge=params.code_challenge,
                      redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource,
                      subject=principal.name, credential=principal.token_sha256)
        self.store.add_code(code, record.expires_at, record.model_dump(mode="json", exclude={"code"}))
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def get_client(self, client_id):
        payload = self.store.client(client_id)
        return OAuthClientInformationFull.model_validate_json(payload) if payload else None

    async def register_client(self, client_info):
        for uri in client_info.redirect_uris or []:
            if not redirect_allowed(str(uri), self.oauth.redirect_uris):
                raise RegistrationError("invalid_redirect_uri", f"Redirect URI {uri} is not on this controller's allowlist")
        self.store.prune(time.time())
        self.store.add_client(client_info.client_id, client_info.model_dump_json())
        self.audit.record_event("client_registered", name=client_info.client_name, outcome="ok", detail=client_info.client_id)

    async def authorize(self, client, params):
        if params.resource is not None:
            try:
                same = normalize(params.resource) == self.resource
            except ValueError:
                same = False
            if not same:
                raise AuthorizeError("invalid_request", f"This server only issues tokens for {self.resource}")
        login_id = secrets.token_urlsafe(32)
        self.store.add_login(login_id, time.time() + LOGIN_SECONDS, {"client_id": client.client_id, "client_name": client.client_name, "params": params.model_dump(mode="json")})
        return f"{self.issuer}/login?request={login_id}"

    async def load_authorization_code(self, client, authorization_code):
        payload = self.store.pop_code(authorization_code)
        if payload is None or payload["client_id"] != client.client_id:
            return None
        return Code(code=authorization_code, **payload)

    async def exchange_authorization_code(self, client, authorization_code):
        if self.principal(authorization_code.subject, authorization_code.credential) is None:
            raise TokenError("invalid_grant", "The operator token used to sign in is no longer valid; sign in again")
        return self.issue(client.client_id, authorization_code.subject, authorization_code.credential, authorization_code.scopes, authorization_code.resource, uuid.uuid4().hex)

    async def load_refresh_token(self, client, refresh_token):
        row = self.store.token(refresh_token)
        if row is None or row["kind"] != "refresh" or row["client_id"] != client.client_id:
            return None
        if row["revoked"]:
            # A rotated-out refresh token presented again means a copy leaked: end every token in the session.
            self.store.revoke_family(row["family"])
            self.audit.record_event("token_reuse", identity=row["payload"].get("subject"), outcome="session_revoked", detail=f"client {client.client_id}")
            return None
        if row["expires"] < time.time():
            return None
        payload = row["payload"]
        return Refresh(token=refresh_token, client_id=payload["client_id"], scopes=payload["scopes"], expires_at=int(row["expires"]), resource=payload["resource"],
                       subject=payload["subject"], credential=payload["credential"], family=row["family"])

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if self.principal(refresh_token.subject, refresh_token.credential) is None:
            self.store.revoke_family(refresh_token.family)
            raise TokenError("invalid_grant", "The operator token used to sign in is no longer valid; sign in again")
        self.store.revoke(refresh_token.token)  # rotation: the previous refresh token becomes a reuse signal
        return self.issue(client.client_id, refresh_token.subject, refresh_token.credential, scopes, refresh_token.resource, refresh_token.family)

    async def load_access_token(self, token):
        row = self.store.token(token)
        if row is None or row["kind"] != "access" or row["revoked"] or row["expires"] < time.time():
            return None
        payload = row["payload"]
        if self.principal(payload["subject"], payload["credential"]) is None:
            return None
        return AccessToken(token=token, client_id=payload["client_id"], scopes=payload["scopes"], expires_at=int(row["expires"]), resource=payload["resource"], subject=payload["subject"])

    async def revoke_token(self, token):
        row = self.store.token(token.token)
        if row is not None:
            self.store.revoke_family(row["family"])
            self.audit.record_event("session_revoked", identity=row["payload"].get("subject"), outcome="ok", detail=f"client {row['client_id']}")


def login_routes(provider, failures):
    """The browser sign-in page: the operator token is verified here, on the server, and never leaves it."""

    def page(login, request_id, error="", status=200):
        host = urlparse(login["params"]["redirect_uri"]).netloc
        body = (PAGE.replace("__CLIENT__", html.escape(login.get("client_name") or "An MCP client")).replace("__HOST__", html.escape(host))
                .replace("__REQUEST__", html.escape(request_id)).replace("__ERROR__", f'<p class="error">{html.escape(error)}</p>' if error else ""))
        return HTMLResponse(body, status_code=status, headers=PAGE_HEADERS)

    def expired():
        return HTMLResponse("<p>This sign-in link has expired. Return to Claude and connect again.</p>", status_code=400, headers=PAGE_HEADERS)

    async def form(request):
        request_id = request.query_params.get("request", "")
        login = provider.store.login(request_id)
        return expired() if login is None else page(login, request_id)

    async def submit(request):
        data = await request.form()
        request_id, token = str(data.get("request", "")), str(data.get("token", "")).strip()  # pasted tokens often carry a stray newline
        login = provider.store.login(request_id)
        if login is None:
            return expired()
        principal = provider.login(token)
        host = request.client.host if request.client else "unknown"
        client_name = login.get("client_name") or login["client_id"]
        if principal is None:
            allowed, retry_after = failures.allow(host)
            if not allowed:
                provider.audit.record_event("sign_in", name=client_name, client=host, outcome="throttled")
                return HTMLResponse("<p>Too many failed sign-in attempts from this address. Wait a minute and try again.</p>", status_code=429,
                                    headers={**PAGE_HEADERS, "Retry-After": str(retry_after)})
            provider.audit.record_event("sign_in", name=client_name, client=host, outcome="rejected")
            return page(login, request_id, "That token was not accepted.", status=401)
        provider.store.delete_login(request_id)
        provider.audit.record_event("sign_in", name=client_name, identity=principal.name, client=host, outcome="ok")
        return RedirectResponse(provider.complete_login(login, principal), status_code=302, headers={"Cache-Control": "no-store"})

    return [Route("/login", form, methods=["GET"]), Route("/login", submit, methods=["POST"])]


def mount(app, provider, failures):
    """Add the authorization-server, protected-resource metadata and sign-in routes ahead of the MCP mount."""
    issuer = AnyHttpUrl(provider.issuer)
    routes = create_auth_routes(provider, issuer, client_registration_options=ClientRegistrationOptions(enabled=True), revocation_options=RevocationOptions(enabled=True))
    routes += create_protected_resource_routes(AnyHttpUrl(provider.resource), [issuer], resource_name="Lab Deploy")
    routes += login_routes(provider, failures)
    app.router.routes.extend(routes)
