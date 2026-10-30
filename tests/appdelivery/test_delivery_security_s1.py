"""The fixes of release S1 of the security assessment of 2026-10-30 (docs/research/RESEARCH-2026-10-30-security-assessment.md),
delivery side: a machine receives a pull-only identity's fresh token and never the environment's (H1); the OS Login key
expires (M3, in test_delivery_machine.py); a container runs bounded (M4, M5, in test_delivery_cluster.py and
test_delivery_host_script.py); a build's context is a path (H2) and builds are budgeted (M9), in test_delivery_build.py."""
import httpx
import pydantic
import pytest
from delivery_fakes import DEV_PULL_SA, DEV_SA, TEAM_TOKEN, config_dict
from fastapi.testclient import TestClient
from fleet_fakes import V2, World
from test_delivery_sign_in import BASE, CALLBACK

from appdelivery import google
from appdelivery.config import Config, SignInSettings
from appdelivery.server import build_app


def test_the_machine_gets_the_pull_accounts_token_and_the_environments_token_never_leaves():
    world = World()
    world.deploy()
    deploys = [call for call in world.fleet.calls if call["action"] == "deploy"]
    assert {call["token"] for call in deploys} == {"dev-pull-token"}
    assert "dev-token" not in {call["token"] for call in deploys}
    assert DEV_PULL_SA in world.tokens and world.tokens.count(DEV_PULL_SA) == len(deploys), "minted once per machine"


def test_the_pull_token_is_minted_fresh_and_short_not_taken_from_the_cache():
    minted = []

    def handle(request):
        minted.append(request.read())
        return httpx.Response(200, json={"accessToken": f"token-{len(minted)}"})
    tokens = google.ServiceAccountTokens(httpx.Client(transport=httpx.MockTransport(handle)), lambda: "host-token")
    assert tokens(DEV_SA) == "token-1" and tokens(DEV_SA) == "token-1", "the environment's own token is cached"
    assert tokens.fresh(DEV_PULL_SA) == "token-2" and tokens.fresh(DEV_PULL_SA) == "token-3", "a pull token is never reused"
    assert b'"lifetime":"900s"' in minted[0] and b'"lifetime":"300s"' in minted[1]


@pytest.mark.parametrize("same", ["service_account", "build"])
def test_the_pull_account_is_an_account_of_its_own(same):
    data = config_dict()
    data["environments"]["dev"]["pull_service_account"] = data["environments"]["dev"]["service_account"] if same == "service_account" else \
        data["environments"]["dev"]["build"]["service_account"]
    with pytest.raises(pydantic.ValidationError, match="account of its own"):
        Config.model_validate(data)


def test_an_environment_names_a_pull_account():
    data = config_dict()
    del data["environments"]["dev"]["pull_service_account"]
    with pytest.raises(pydantic.ValidationError, match="pull_service_account"):
        Config.model_validate(data)


def test_a_rollback_and_a_second_release_use_the_pull_account_too():
    world = World()
    world.deploy()
    world.fleet.unhealthy.add(V2)
    world.deploy(V2)
    assert all(call["token"] == "dev-pull-token" for call in world.fleet.calls if call["action"] == "deploy")


# ---- the gate: the same bounds as the infrastructure layer's (M1, M2, L2), on this service's own app
def test_a_body_above_the_cap_is_refused_and_registration_is_bounded(tmp_path):
    world = World(database=str(tmp_path / "delivery.db"))
    world.config.allowed_hosts = ["localhost:*"]
    world.config.sign_in = SignInSettings(url=BASE)
    world.config.max_request_bytes = 4096
    world.config.registrations_per_hour = 2
    with TestClient(build_app(world.config, delivery=world.delivery), base_url=BASE, follow_redirects=False) as client:
        headers = {"Authorization": f"Bearer {TEAM_TOKEN}", "Content-Type": "application/json"}
        assert client.post("/releases", content=b'{"note": "' + b"n" * 5000 + b'"}', headers=headers).status_code == 413
        assert client.post("/mcp", content=b"x" * 5000, headers={**headers, "Accept": "application/json, text/event-stream"}).status_code == 413
        confidential = client.post("/register", json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "client_secret_basic"})
        assert confidential.status_code == 400 and confidential.json()["error"] == "invalid_client_metadata"
        assert client.post("/register", json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"}).status_code == 201
        assert client.post("/register", json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"}).status_code == 429
        assert client.get("/.well-known/oauth-authorization-server").json()["token_endpoint_auth_methods_supported"] == ["none"]
    assert oct((tmp_path / "delivery.db").stat().st_mode & 0o777) == "0o600"
