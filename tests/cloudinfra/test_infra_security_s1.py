"""The fixes of release S1 of the security assessment of 2026-10-30 (docs/research/RESEARCH-2026-10-30-security-assessment.md),
infrastructure side: SSH and RDP are never opened to a public range, however the rule is asked for (H4); a resize is
held to the cost ceiling as a provisioning is (M8). H3, the tenant namespace unique per request, is in test_infra_identity.py."""
import asyncio
import os
import threading

import pytest
from fastapi.testclient import TestClient
from gcp_fakes import TOKEN, config_dict, make_config, make_orchestrator, make_provider, principal
from mcp.shared.auth import OAuthClientInformationFull
from test_infra_release_a import orchestrated, provision, refused
from test_infra_sign_in import BASE, CALLBACK, register, signed_in

from cloudinfra.config import Config
from cloudinfra.mcp.server import build_app
from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.models.request import InfrastructureRequest
from cloudinfra.orchestration import validator
from cloudinfra.orchestration.provisioner import InlineExecutor
from cloudinfra.providers import ProviderRegistry
from cloudinfra.security import validation
from cloudinfra.security.oauth import SignIn, SignInStore
from cloudinfra.state.state_manager import StateManager

WEB = {"region": "us-central1", "name": "web", "compute": {"cpu": 2, "memory_gb": 8}, "network": {"public_access": True}}


@pytest.mark.parametrize("ports, sources", [([22], ["0.0.0.0/0"]), ([80, 22], ["203.0.113.0/24"]), ([3389], ["0.0.0.0/0"]), ([22], ["10.0.0.0/8", "0.0.0.0/0"])])
def test_a_network_block_never_opens_ssh_or_rdp_to_a_public_range(ports, sources):
    config = Config.model_validate(config_dict())
    provider, _ = make_provider(config)
    request = InfrastructureRequest.model_validate({**WEB, "network": {"public_access": True, "allowed_ports": ports, "allowed_sources": sources}})
    [problem] = [item for item in validator.validate(request, config, provider) if item.code == ErrorCode.INVALID_NETWORK_CONFIGURATION]
    assert "never opens SSH or RDP to a public range" in problem.message and problem.field == "network.allowed_ports"


@pytest.mark.parametrize("ports, sources", [([22], ["10.0.0.0/8"]), ([22], [validation.PROXY_RANGES[0].with_prefixlen]), ([80, 443], ["0.0.0.0/0"]), ([22, 3389], ["192.168.1.0/24", "172.16.0.0/12"])])
def test_ssh_from_a_private_or_proxy_range_and_web_ports_from_anywhere_still_pass(ports, sources):
    config = Config.model_validate(config_dict())
    provider, _ = make_provider(config)
    request = InfrastructureRequest.model_validate({**WEB, "network": {"public_access": True, "allowed_ports": ports, "allowed_sources": sources}})
    assert not [item for item in validator.validate(request, config, provider) if item.code == ErrorCode.INVALID_NETWORK_CONFIGURATION]


def test_configure_firewall_refuses_the_same_before_the_provider_is_called(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    network = orchestrator.create_network(who, region="us-central1", name="shared", public_access=False, wait_seconds=45)
    network_id = network.resources.network[0].id
    fake.calls.clear()
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.configure_firewall(network_id, [22], ["0.0.0.0/0"], who)
    assert caught.value.code == ErrorCode.INVALID_NETWORK_CONFIGURATION and caught.value.details["field"] == "ports" and fake.calls == []
    assert orchestrator.configure_firewall(network_id, [22], ["10.0.0.0/8"], who).resource.firewall_rules[-1].ports == [22]


def test_the_operator_may_close_more_ports_and_never_fewer(tmp_path):
    config = Config.model_validate({**config_dict(), "closed_public_ports": [5432]})
    assert config.closed_public_ports == [22, 3389, 5432]
    provider, _ = make_provider(config)
    request = InfrastructureRequest.model_validate({**WEB, "network": {"public_access": True, "allowed_ports": [5432], "allowed_sources": ["0.0.0.0/0"]}})
    assert any(item.code == ErrorCode.INVALID_NETWORK_CONFIGURATION for item in validator.validate(request, config, provider))
    with pytest.raises(ValueError):
        Config.model_validate({**config_dict(), "closed_public_ports": [70000]})


def test_a_resize_is_held_to_the_cost_ceiling(tmp_path):
    """Ten small machines under the ceiling, each resized to the largest shape, would multiply the bill: the request is priced
    as it would be after the resize, before the machine is touched."""
    orchestrator, fake, _, who = orchestrated(tmp_path, limits={"max_monthly_cost_usd": 400.0, "max_cpu_per_instance": 32, "max_memory_gb_per_instance": 128})
    response = provision(orchestrator, who, {"name": "fleet", "compute": {"cpu": 2, "memory_gb": 8, "count": 3, "os": "ubuntu"}})
    machine = response.resources.compute[0]
    fake.calls.clear()
    error = refused(ErrorCode.COST_LIMIT_EXCEEDED, orchestrator.resize_compute, machine.id, 32, 128, who)
    assert "above the ceiling" in error.message and error.details["resource_id"] == machine.id and fake.calls == []
    assert orchestrator.get_resource(machine.id, who).resource.machine_type == "e2-standard-2", "nothing was touched"
    resized = orchestrator.resize_compute(machine.id, 4, 16, who, wait_seconds=45)
    assert resized.resource.machine_type == "e2-standard-4", "a resize that stays under the ceiling goes through"


def test_a_callers_own_tighter_ceiling_holds_on_a_resize_too(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path, limits={"max_monthly_cost_usd": 5000.0})
    who = who.model_copy(update={"max_monthly_cost_usd": 120.0})
    response = provision(orchestrator, who, {"name": "one", "compute": {"cpu": 2, "memory_gb": 8, "os": "ubuntu"}})
    refused(ErrorCode.COST_LIMIT_EXCEEDED, orchestrator.resize_compute, response.resources.compute[0].id, 16, 64, who)


# ---- the gate (M1, M2, L1, L2): bounded bodies, bounded registration, single use that is atomic, private files




def serve(tmp_path, **settings):
    config = make_config(tmp_path, sign_in={"url": BASE}, **settings)
    provider, _ = make_provider(config)
    app = build_app(config, registry=ProviderRegistry(config, {"gcp": provider}), executor=InlineExecutor())
    return TestClient(app, base_url=BASE, follow_redirects=False), config


def test_a_body_above_the_cap_is_refused_before_it_is_read(tmp_path):
    client, config = serve(tmp_path, max_request_bytes=4096)
    with client:
        big = b'{"region": "us-central1", "name": "' + b"a" * 5000 + b'"}'
        headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        assert client.post("/infrastructure/validate", content=big, headers=headers).status_code == 413
        assert client.post("/mcp", content=big, headers={**headers, "Accept": "application/json, text/event-stream"}).status_code == 413
        assert client.post("/sign-in", content=b"login=" + b"x" * 5000, headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 413
        small = client.post("/infrastructure/validate", json={"region": "us-central1", "compute": {"cpu": 2, "memory_gb": 8}}, headers=headers)
        assert small.status_code == 200, small.text


def test_registration_is_public_but_small_and_few(tmp_path):
    client, config = serve(tmp_path, registrations_per_hour=6)  # five refusals, one registration, then the budget is spent
    with client:
        confidential = client.post("/register", json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "client_secret_post"})
        assert confidential.status_code == 400 and confidential.json()["error"] == "invalid_client_metadata" and "public" in confidential.json()["error_description"]
        keyed = client.post("/register", json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none", "jwks": {"keys": []}})
        assert keyed.status_code == 400 and keyed.json()["error"] == "invalid_client_metadata"
        long = client.post("/register", json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none", "client_name": "n" * 101})
        assert long.status_code == 400 and "client_name" in long.json()["error_description"]
        fat = client.post("/register", json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none", "contacts": ["c" * 3000]})
        assert fat.status_code == 400 and "larger than" in fat.json()["error_description"]
        assert register(client).status_code == 201, "Claude's registration passes"
        assert client.get("/.well-known/oauth-authorization-server").json()["token_endpoint_auth_methods_supported"] == ["none"]
        unnamed = client.post("/register", json={"redirect_uris": [CALLBACK]})
        assert unnamed.status_code == 400 and "public" in unnamed.json()["error_description"], "no method named: the SDK would issue a secret, which is refused"
        assert register(client).status_code == 429, "six registrations an hour from one address, refusals included"
        assert client.get("/.well-known/oauth-authorization-server").status_code == 200, "the other public paths keep their own budget"


def test_a_client_that_never_signed_in_is_forgotten_after_a_day(tmp_path):
    moment = [1_000_000.0]
    store = SignInStore(str(tmp_path / "s.db"), clock=lambda: moment[0])
    config = make_config(tmp_path, sign_in={"url": BASE})
    sign_in = SignIn(config, store=store)


    def registered(client_id):
        asyncio.run(sign_in.register_client(OAuthClientInformationFull(client_id=client_id, redirect_uris=[CALLBACK], token_endpoint_auth_method="none")))
    registered("idle")
    store.add_token("live-access", "access", "family-1", "busy", 200_000, {"subject": "app-layer", "credential": "x", "scopes": [], "resource": None})
    registered("busy")
    moment[0] += 86_401
    registered("later")
    assert store.get_client("idle") is None and store.get_client("busy") is not None and store.get_client("later") is not None


def test_a_code_is_taken_by_exactly_one_of_two_callers_at_once(tmp_path):
    store = SignInStore(str(tmp_path / "s.db"))
    store.put_grant("the-code", "code", 300, {"client_id": "c"})
    won = []
    gate = threading.Barrier(2)

    def take():
        gate.wait()
        won.append(store.get_grant("the-code", "code", take=True) is not None)
    threads = [threading.Thread(target=take) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(won) == [False, True]
    store.add_token("r", "refresh", "f", "c", 3600, {})
    assert store.revoke_token("r") is True and store.revoke_token("r") is False, "the second revocation of a refresh token is a reuse"


def test_a_refresh_token_exchanged_twice_at_once_ends_the_sign_in(tmp_path):
    client, config = serve(tmp_path)
    with client:
        client_id, tokens = signed_in(client)
        store = client.app.state.sign_in.store
        original = store.revoke_token
        store.revoke_token = lambda token: False  # as the second of two exchanges at the same moment sees it
        refused = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
        assert refused.json()["error"] == "invalid_grant"
        store.revoke_token = original
        assert client.post("/mcp", headers={"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {tokens['access_token']}"},
                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401, "the whole family is gone"


def test_the_state_file_is_the_services_alone(tmp_path):
    path = tmp_path / "state.db"
    StateManager(str(path))
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    client, config = serve(tmp_path)
    with client:
        assert os.umask(0o077) == 0o077, "the service sets its umask so every file it makes is private"
