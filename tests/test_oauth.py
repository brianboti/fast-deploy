"""Browser sign-in for Claude's hosted connector: the controller as its own OAuth 2.1 authorization server."""
import base64
import hashlib
import secrets
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient

from labdeploy.config import CLAUDE_CALLBACK, Config
from labdeploy.server import build_app

RESOURCE = "http://localhost/mcp"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def make_config(tmp_path, token=b"test-token", **oauth):
    return Config.model_validate({
        "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}}, "database": str(tmp_path / "db"),
        "principals": [{"name": "manager", "token_sha256": hashlib.sha256(token).hexdigest(), "applications": ["demo"], "can_deploy": True}],
        "oauth": {"public_url": "http://localhost", **oauth},
    })


@pytest.fixture
def client(tmp_path):
    with TestClient(build_app(make_config(tmp_path), FakeGitHub())) as client:
        yield client


def pkce():
    verifier = secrets.token_urlsafe(32)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def register(client, redirect=CLAUDE_CALLBACK):
    body = {"redirect_uris": [redirect], "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "client_name": "Claude"}
    return client.post("/register", json=body)


def start(client, registration, redirect=CLAUDE_CALLBACK, resource=RESOURCE):
    """Claude's authorization request: returns the sign-in request id (or the error redirect) and the PKCE verifier."""
    verifier, challenge = pkce()
    params = {"client_id": registration["client_id"], "redirect_uri": redirect, "response_type": "code", "code_challenge": challenge,
              "code_challenge_method": "S256", "state": "xyz", "resource": resource}
    response = client.get("/authorize", params=params, follow_redirects=False)
    assert response.status_code == 302, response.text
    return response.headers["location"], verifier


def sign_in(client, registration, token="test-token"):
    location, verifier = start(client, registration)
    assert location.startswith("http://localhost/login?request=")
    request_id = parse_qs(urlparse(location).query)["request"][0]
    page = client.get("/login", params={"request": request_id})
    assert page.status_code == 200 and "Claude" in page.text and "claude.ai" in page.text and "no-store" in page.headers["Cache-Control"]
    assert "form-action" not in page.headers["Content-Security-Policy"]  # Chrome would otherwise block the redirect back to Claude
    return client.post("/login", data={"request": request_id, "token": token}, follow_redirects=False), verifier, request_id


def exchange(client, registration, code, verifier, redirect=CLAUDE_CALLBACK):
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "client_id": registration["client_id"], "code_verifier": verifier, "resource": RESOURCE}
    return client.post("/token", data=data)


def tokens_for(client):
    registration = register(client).json()
    done, verifier, _ = sign_in(client, registration)
    code = parse_qs(urlparse(done.headers["location"]).query)["code"][0]
    return registration, exchange(client, registration, code, verifier).json()


def test_unauthenticated_challenge_points_at_resource_metadata(client):
    denied = client.post("/mcp", headers=MCP_HEADERS, json=INITIALIZE)
    assert denied.status_code == 401
    assert denied.headers["WWW-Authenticate"] == 'Bearer resource_metadata="http://localhost/.well-known/oauth-protected-resource/mcp"'
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == RESOURCE and resource["authorization_servers"] == ["http://localhost/"]
    server = client.get("/.well-known/oauth-authorization-server").json()
    assert server["registration_endpoint"] == "http://localhost/register" and server["code_challenge_methods_supported"] == ["S256"]
    assert server["token_endpoint"] == "http://localhost/token" and "refresh_token" in server["grant_types_supported"]


def test_browser_sign_in_issues_tokens_that_open_mcp_and_rest(client):
    registration = register(client).json()
    assert registration.get("client_secret") is None  # Claude registers as a public client with PKCE
    done, verifier, request_id = sign_in(client, registration, token=" test-token\n")  # whitespace from a paste is ignored
    assert done.status_code == 302
    location = done.headers["location"]
    assert location.startswith(CLAUDE_CALLBACK + "?")
    query = parse_qs(urlparse(location).query)
    assert query["state"] == ["xyz"]
    assert client.get("/login", params={"request": request_id}).status_code == 400  # a used sign-in link is gone
    issued = exchange(client, registration, query["code"][0], verifier)
    assert issued.status_code == 200, issued.text
    body = issued.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600 and body["refresh_token"]
    headers = {"Authorization": f"Bearer {body['access_token']}", **MCP_HEADERS}
    init = client.post("/mcp", headers=headers, json=INITIALIZE)
    assert init.status_code == 200 and init.json()["result"]["serverInfo"]["name"] == "Fast Deploy"
    listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_applications", "arguments": {}}})
    assert "lab/demo" in listed.text
    assert client.get("/applications", headers=headers).status_code == 200  # the REST routes accept the same token
    assert client.get("/applications", headers={"Authorization": "Bearer test-token"}).status_code == 200  # static tokens still work


def test_wrong_token_is_refused_and_budgeted(client):
    registration = register(client).json()
    done, verifier, request_id = sign_in(client, registration, token="wrong")
    assert done.status_code == 401 and "not accepted" in done.text
    for _ in range(25):
        last = client.post("/login", data={"request": request_id, "token": "wrong"}, follow_redirects=False)
    assert last.status_code == 429 and int(last.headers["Retry-After"]) >= 1


def test_codes_are_single_use_and_bound_to_pkce(client):
    registration = register(client).json()
    done, verifier, _ = sign_in(client, registration)
    code = parse_qs(urlparse(done.headers["location"]).query)["code"][0]
    wrong = exchange(client, registration, code, "not-the-verifier")
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
    burned = exchange(client, registration, code, verifier)  # a failed exchange burns the code
    assert burned.status_code == 400 and burned.json()["error"] == "invalid_grant"
    done, verifier, _ = sign_in(client, registration)
    code = parse_qs(urlparse(done.headers["location"]).query)["code"][0]
    assert exchange(client, registration, code, verifier).status_code == 200
    assert exchange(client, registration, code, verifier).json()["error"] == "invalid_grant"


def test_refresh_rotates_and_reuse_ends_the_session(client):
    registration, first = tokens_for(client)
    refreshed = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": registration["client_id"]})
    assert refreshed.status_code == 200, refreshed.text
    second = refreshed.json()
    assert second["refresh_token"] != first["refresh_token"] and second["access_token"] != first["access_token"]
    assert client.get("/applications", headers={"Authorization": f"Bearer {second['access_token']}"}).status_code == 200
    reuse = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": registration["client_id"]})
    assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
    # Presenting a rotated-out refresh token means it leaked: every token in that session is revoked.
    assert client.get("/applications", headers={"Authorization": f"Bearer {second['access_token']}"}).status_code == 401
    later = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": second["refresh_token"], "client_id": registration["client_id"]})
    assert later.json()["error"] == "invalid_grant"


def test_revocation_and_expiry(client, tmp_path):
    registration, issued = tokens_for(client)
    headers = {"Authorization": f"Bearer {issued['access_token']}"}
    assert client.get("/applications", headers=headers).status_code == 200
    revoked = client.post("/revoke", data={"token": issued["refresh_token"], "client_id": registration["client_id"], "client_secret": ""})
    assert revoked.status_code == 200
    assert client.get("/applications", headers=headers).status_code == 401
    registration, issued = tokens_for(client)
    with sqlite3.connect(tmp_path / "db") as db:
        db.execute("UPDATE oauth_tokens SET expires = 0")
    assert client.get("/applications", headers={"Authorization": f"Bearer {issued['access_token']}"}).status_code == 401
    stale = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": issued["refresh_token"], "client_id": registration["client_id"]})
    assert stale.json()["error"] == "invalid_grant"


def test_rotating_the_operator_token_ends_sessions(client, tmp_path):
    _, issued = tokens_for(client)
    with TestClient(build_app(make_config(tmp_path, token=b"rotated"), FakeGitHub())) as rotated:
        assert rotated.get("/applications", headers={"Authorization": f"Bearer {issued['access_token']}"}).status_code == 401


def test_registration_allows_only_claude_and_loopback_callbacks(client):
    foreign = register(client, "https://attacker.example/callback")
    assert foreign.status_code == 400 and foreign.json()["error"] == "invalid_redirect_uri"
    assert register(client, "http://localhost:3118/callback").status_code == 201  # Claude Code's loopback, any port
    assert register(client, "http://localhost:3118/elsewhere").status_code == 400
    assert register(client, "https://claude.ai/api/mcp/other").status_code == 400


def test_other_resource_is_refused_with_a_redirect_error(client):
    registration = register(client).json()
    location, _ = start(client, registration, resource="http://localhost/other")
    assert location.startswith(CLAUDE_CALLBACK) and "error=invalid_request" in location and "state=xyz" in location


def test_expired_or_unknown_sign_in_link(client):
    assert client.get("/login", params={"request": "nope"}).status_code == 400
    assert client.post("/login", data={"request": "nope", "token": "test-token"}, follow_redirects=False).status_code == 400
    assert client.get("/login").status_code == 400


def test_public_endpoints_are_rate_limited_per_client(tmp_path):
    with TestClient(build_app(make_config(tmp_path), FakeGitHub())) as client:
        for _ in range(130):
            last = client.get("/.well-known/oauth-authorization-server")
        assert last.status_code == 429


def test_configuration_requires_https_origin_and_allowed_host(tmp_path):
    with pytest.raises(ValueError, match="https"):
        make_config(tmp_path, public_url="http://deploy.example.edu")
    with pytest.raises(ValueError, match="origin only"):
        make_config(tmp_path, public_url="https://deploy.example.edu/mcp")
    with pytest.raises(ValueError, match="allowed_hosts must list"):
        make_config(tmp_path, public_url="https://deploy.example.edu")
    base = make_config(tmp_path).model_dump()
    base.update(oauth={"public_url": "https://deploy.example.edu/"}, allowed_hosts=["deploy.example.edu"])
    config = Config.model_validate(base)
    assert config.oauth.public_url == "https://deploy.example.edu" and config.oauth.redirect_uris[0] == CLAUDE_CALLBACK
    with pytest.raises(ValueError, match="redirect_uris"):
        make_config(tmp_path, redirect_uris=["http://attacker.example/cb"])


def test_without_oauth_nothing_is_public(tmp_path):
    config = Config.model_validate({"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}}, "database": str(tmp_path / "db"),
                                    "principals": [{"name": "m", "token_sha256": hashlib.sha256(b"t").hexdigest(), "applications": ["demo"]}]})
    with TestClient(build_app(config, FakeGitHub())) as client:
        denied = client.post("/mcp", headers=MCP_HEADERS, json=INITIALIZE)
        assert denied.status_code == 401 and denied.headers["WWW-Authenticate"] == "Bearer"
        assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 401
        assert client.get("/login").status_code == 401
