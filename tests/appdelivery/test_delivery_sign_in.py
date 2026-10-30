"""Sign-in for Claude's hosted connector: the OAuth flow as claude.ai runs it, and the ways a sign-in ends. The module is
written again from the infrastructure layer's, so these tests are too."""
import base64
import hashlib
import re
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest
from delivery_fakes import TEAM_TOKEN, make_config
from fastapi.testclient import TestClient
from fleet_fakes import World
from pydantic import ValidationError

from appdelivery.config import SignInSettings
from appdelivery.server import build_app

BASE = "http://localhost:8000"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "a-verifier-long-enough-for-pkce-0123456789-abcdefghijkl"
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def served(tmp_path):
    world = World(database=str(tmp_path / "delivery.db"))
    world.config.auth_failures_per_minute = 3
    world.config.allowed_hosts = ["localhost:*"]
    world.config.sign_in = SignInSettings(url=BASE)
    with TestClient(build_app(world.config, delivery=world.delivery), base_url=BASE, follow_redirects=False) as client:
        yield client, world, tmp_path / "delivery.db"


def signed_in(client, token=TEAM_TOKEN):
    registered = client.post("/register", json={"redirect_uris": [CALLBACK], "client_name": "Claude", "token_endpoint_auth_method": "none",
                                                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    client_id = registered.json()["client_id"]
    authorize = client.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "xyz",
                                                 "code_challenge": CHALLENGE, "code_challenge_method": "S256"})
    login = parse_qs(urlparse(authorize.headers["location"]).query)["login"][0]
    assert "Application Delivery" in client.get("/sign-in", params={"login": login}).text
    back = client.post("/sign-in", data={"login": login, "token": token})
    if back.status_code != 302:
        return client_id, back
    code = parse_qs(urlparse(back.headers["location"]).query)["code"][0]
    tokens = client.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "client_id": client_id, "code_verifier": VERIFIER})
    assert tokens.status_code == 200, tokens.text
    return client_id, tokens.json()


def releases(client, access):
    return client.get("/releases", headers={"Authorization": f"Bearer {access}"})


def test_claude_finds_the_sign_in_and_gets_the_principals_rights(served):
    client, _, database = served
    refused = client.post("/mcp", headers=MCP_HEADERS, json={})
    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"'
    assert client.get("/.well-known/oauth-protected-resource/mcp").json()["resource"] == BASE + "/mcp"
    _, tokens = signed_in(client)
    assert releases(client, tokens["access_token"]).status_code == 200
    listed = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listed.status_code == 200 and listed.json()["result"]["tools"]
    rows = sqlite3.connect(database).execute("SELECT kind, identity, outcome FROM events").fetchall()
    assert ("sign_in", "shop-team", "ok") in rows and ("rest", "shop-team", "ok") in [(kind, who, outcome) for kind, who, outcome in rows]


def test_a_wrong_token_is_refused(served):
    client, _, _ = served
    _, refused = signed_in(client, token="not-a-token")
    assert refused.status_code == 401 and "not one of this service" in refused.text


def test_a_reused_refresh_token_or_a_new_principal_token_ends_the_sign_in(served):
    client, world, _ = served
    client_id, tokens = signed_in(client)
    renewed = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id}).json()
    assert releases(client, renewed["access_token"]).status_code == 200
    again = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
    assert again.json()["error"] == "invalid_grant" and releases(client, renewed["access_token"]).status_code == 401
    _, tokens = signed_in(client)
    world.config.principals[0].token_sha256 = hashlib.sha256(b"rotated").hexdigest()
    assert releases(client, tokens["access_token"]).status_code == 401


def test_sign_in_is_off_by_default(tmp_path):
    world = World(database=str(tmp_path / "delivery.db"))
    world.config.allowed_hosts = ["testserver"]
    with TestClient(build_app(world.config, delivery=world.delivery)) as plain:
        assert plain.post("/register", json={}).status_code == 401 and plain.get("/sign-in").status_code == 401
        assert plain.get("/releases").headers["www-authenticate"] == "Bearer"
        assert plain.get("/releases", headers={"Authorization": f"Bearer {TEAM_TOKEN}"}).status_code == 200


@pytest.mark.parametrize(("sign_in", "hosts", "message"), [
    ({"url": "http://delivery.example.com"}, ["delivery.example.com"], "https address"),
    ({"url": "https://delivery.example.com"}, ["localhost:*"], "must be in allowed_hosts"),
])
def test_the_configuration_is_checked(sign_in, hosts, message):
    with pytest.raises(ValidationError, match=re.escape(message)):
        make_config(sign_in=sign_in, allowed_hosts=hosts)
    assert make_config(sign_in={"url": "https://delivery.example.com"}, allowed_hosts=["delivery.example.com"]).sign_in.callbacks[0] == CALLBACK
