"""Sign-in for Claude's hosted connector: the whole OAuth flow as claude.ai runs it, and every way a sign-in ends."""
import base64
import hashlib
import re
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from gcp_fakes import TOKEN, make_config, make_provider
from pydantic import ValidationError

from cloudinfra.mcp.server import build_app
from cloudinfra.orchestration.provisioner import InlineExecutor
from cloudinfra.providers import ProviderRegistry

BASE = "http://localhost:8000"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "a-verifier-long-enough-for-pkce-0123456789-abcdefghijkl"
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def served(tmp_path):
    config = make_config(tmp_path, sign_in={"url": BASE}, auth_failures_per_minute=3)
    provider, _ = make_provider(config)
    app = build_app(config, registry=ProviderRegistry(config, {"gcp": provider}), executor=InlineExecutor())
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        yield client, config, app.state.state


def register(client, callbacks=(CALLBACK,)):
    return client.post("/register", json={"redirect_uris": list(callbacks), "client_name": "Claude", "token_endpoint_auth_method": "none",
                                          "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})


def sign_in_page(client, client_id):
    """What claude.ai does after registering: send the browser to /authorize, which hands it to the sign-in page."""
    authorize = client.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "xyz",
                                                 "code_challenge": CHALLENGE, "code_challenge_method": "S256", "resource": BASE + "/mcp"})
    assert authorize.status_code == 302, authorize.text
    location = authorize.headers["location"]
    assert location.startswith(BASE + "/sign-in?login=")
    return parse_qs(urlparse(location).query)["login"][0]


def signed_in(client, token=TOKEN):
    client_id = register(client).json()["client_id"]
    login = sign_in_page(client, client_id)
    back = client.post("/sign-in", data={"login": login, "token": token})
    assert back.status_code == 302, back.text
    query = parse_qs(urlparse(back.headers["location"]).query)
    tokens = client.post("/token", data={"grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": CALLBACK,
                                         "client_id": client_id, "code_verifier": VERIFIER})
    assert tokens.status_code == 200, tokens.text
    return client_id, tokens.json()


def tools(client, access):
    return client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {access}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


def refresh(client, client_id, token):
    return client.post("/token", data={"grant_type": "refresh_token", "refresh_token": token, "client_id": client_id})


def test_claude_finds_the_sign_in_from_the_401(served):
    client, _, _ = served
    refused = client.post("/mcp", headers=MCP_HEADERS, json={})
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"'
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == BASE + "/mcp" and resource["authorization_servers"] == [BASE + "/"]
    assert client.get("/.well-known/oauth-protected-resource").json() == resource
    server = client.get("/.well-known/oauth-authorization-server").json()
    assert server["authorization_endpoint"] == BASE + "/authorize" and server["registration_endpoint"] == BASE + "/register"
    assert server["code_challenge_methods_supported"] == ["S256"]


def test_a_sign_in_gives_claude_the_principals_rights(served):
    client, _, state = served
    page = client.get("/sign-in", params={"login": sign_in_page(client, register(client).json()["client_id"])})
    assert page.status_code == 200 and "<strong>Claude</strong>" in page.text and "claude.ai" in page.text
    assert page.headers["x-frame-options"] == "DENY" and page.headers["cache-control"] == "no-store"
    _, tokens = signed_in(client)
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600 and tokens["refresh_token"]
    listed = tools(client, tokens["access_token"])
    assert listed.status_code == 200 and listed.json()["result"]["tools"]
    assert client.get("/infrastructure", headers={"Authorization": f"Bearer {tokens['access_token']}"}).status_code == 200
    events = state.recent_events(limit=20)
    assert any(item["kind"] == "sign_in" and item["identity"] == "app-layer" and item["outcome"] == "ok" for item in events)
    assert any(item["kind"] == "rest" and item["identity"] == "app-layer" for item in events), "calls with the access token are audited as the principal"
    assert TOKEN not in repr(events) and tokens["access_token"] not in repr(events)


def test_a_wrong_token_is_refused_and_throttled(served):
    client, _, state = served
    login = sign_in_page(client, register(client).json()["client_id"])
    wrong = client.post("/sign-in", data={"login": login, "token": "not-a-token"})
    assert wrong.status_code == 401 and "not one of this service" in wrong.text
    client.post("/sign-in", data={"login": login, "token": "not-a-token"})
    client.post("/sign-in", data={"login": login, "token": "not-a-token"})
    assert client.post("/sign-in", data={"login": login, "token": TOKEN}).status_code == 302, "a right token still passes"
    login = sign_in_page(client, register(client).json()["client_id"])
    assert client.post("/sign-in", data={"login": login, "token": "not-a-token"}).status_code == 429
    assert [item["outcome"] for item in state.recent_events(limit=20) if item["kind"] == "sign_in"].count("refused") == 3


def test_a_sign_in_link_and_a_code_work_once(served):
    client, _, _ = served
    client_id = register(client).json()["client_id"]
    login = sign_in_page(client, client_id)
    code = parse_qs(urlparse(client.post("/sign-in", data={"login": login, "token": TOKEN}).headers["location"]).query)["code"][0]
    assert client.post("/sign-in", data={"login": login, "token": TOKEN}).status_code == 400
    assert client.get("/sign-in", params={"login": login}).status_code == 400
    exchange = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "client_id": client_id, "code_verifier": "x" * 43}
    assert client.post("/token", data=exchange).json()["error"] == "invalid_grant", "a wrong PKCE verifier"
    assert client.post("/token", data={**exchange, "code_verifier": VERIFIER}).status_code == 200
    assert client.post("/token", data={**exchange, "code_verifier": VERIFIER}).json()["error"] == "invalid_grant"


def test_only_the_configured_callbacks_may_register(served):
    client, _, _ = served
    refused = register(client, ["https://evil.example/callback"])
    assert refused.status_code == 400 and refused.json()["error"] == "invalid_redirect_uri"
    assert register(client, ["https://claude.com/api/mcp/auth_callback"]).status_code == 201


def test_refresh_rotates_and_a_reused_refresh_token_ends_the_sign_in(served):
    client, _, state = served
    client_id, tokens = signed_in(client)
    renewed = refresh(client, client_id, tokens["refresh_token"])
    assert renewed.status_code == 200 and renewed.json()["access_token"] != tokens["access_token"]
    assert tools(client, renewed.json()["access_token"]).status_code == 200
    assert refresh(client, client_id, tokens["refresh_token"]).json()["error"] == "invalid_grant"
    assert tools(client, renewed.json()["access_token"]).status_code == 401, "the copy's use ended the whole sign-in"
    assert refresh(client, client_id, renewed.json()["refresh_token"]).status_code == 400
    assert any(item["kind"] == "token_reuse" for item in state.recent_events(limit=20))


def test_a_new_principal_token_ends_its_sign_ins(served):
    client, config, _ = served
    client_id, tokens = signed_in(client)
    config.principals[0].token_sha256 = hashlib.sha256(b"rotated").hexdigest()
    assert tools(client, tokens["access_token"]).status_code == 401
    assert refresh(client, client_id, tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_revoking_ends_the_sign_in(served):
    # the SDK 1.30 revocation form requires a client_secret field even for a public client; an empty one passes
    client, _, _ = served
    client_id, tokens = signed_in(client)
    assert client.post("/revoke", data={"token": tokens["refresh_token"], "client_id": client_id, "client_secret": ""}).status_code == 200
    assert tools(client, tokens["access_token"]).status_code == 401


def test_static_tokens_still_work_and_sign_in_is_off_by_default(served, tmp_path):
    client, _, _ = served
    assert tools(client, TOKEN).status_code == 200
    config = make_config(tmp_path / "off")
    (tmp_path / "off").mkdir()
    provider, _ = make_provider(config)
    with TestClient(build_app(config, registry=ProviderRegistry(config, {"gcp": provider}), executor=InlineExecutor())) as plain:
        assert plain.post("/register", json={}).status_code == 401 and plain.get("/sign-in").status_code == 401
        assert plain.get("/infrastructure").headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(("sign_in", "hosts", "message"), [
    ({"url": "http://infra.example.com"}, ["infra.example.com"], "https address"),
    ({"url": "https://infra.example.com/mcp"}, ["infra.example.com"], "no path"),
    ({"url": "https://infra.example.com"}, ["localhost:*"], "must be in allowed_hosts"),
    ({"url": "https://infra.example.com", "callbacks": ["http://claude.ai/cb"]}, ["infra.example.com"], "callbacks must be https"),
])
def test_the_configuration_is_checked(sign_in, hosts, message):
    with pytest.raises(ValidationError, match=re.escape(message)):
        make_config(sign_in=sign_in, allowed_hosts=hosts)
    assert make_config(sign_in={"url": "https://infra.example.com/"}, allowed_hosts=["infra.example.com"]).sign_in.url == "https://infra.example.com"
