"""Tokens, secrets, bindings and image attestation: Google's APIs and Cosign replaced by fakes."""
import base64
import json

import httpx
import pytest
from delivery_fakes import DIGEST, IMAGE, PROD_SA, Runner, infra_response, make_config, release_dict

from appdelivery import attest, bindings, remote
from appdelivery.config import KeylessSigner
from appdelivery.errors import DeliveryError, ErrorCode
from appdelivery.google import HostToken, ServiceAccountTokens, read_secret
from appdelivery.infra import InfraRequest
from appdelivery.models import ReleaseRequest
from appdelivery.remote import Completed

SECRET_VALUE = "s3cr3t-Value-Never-Shown"


def google(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def no_metadata(request):
    raise httpx.ConnectError("no metadata server here")


def test_the_host_token_comes_from_the_metadata_server_and_is_kept():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"access_token": "host", "expires_in": 3599})
    token = HostToken(google(handle), runner=Runner(Completed(1, "", "should not run")), clock=lambda: 1000.0)
    assert token() == "host" and token() == "host" and len(calls) == 1 and calls[0].headers["Metadata-Flavor"] == "Google"


def test_without_a_metadata_server_the_host_token_comes_from_gcloud():
    runner = Runner(Completed(0, "laptop-token\n", ""))
    assert HostToken(google(no_metadata), runner=runner)() == "laptop-token"
    assert runner.calls[0]["argv"] == ["gcloud", "auth", "print-access-token", "--quiet"]


def test_a_host_with_no_identity_says_how_to_get_one():
    with pytest.raises(DeliveryError) as caught:
        HostToken(google(no_metadata), runner=Runner(Completed(1, "", "not signed in")))()
    assert caught.value.code == ErrorCode.GOOGLE_AUTH_FAILED and "gcloud auth login" in caught.value.message


def test_an_environment_token_is_issued_to_the_host_and_cached():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"accessToken": "env-token", "expireTime": "2026-10-29T11:00:00Z"})
    tokens = ServiceAccountTokens(google(handle), lambda: "host", clock=lambda: 1000.0)
    assert tokens(PROD_SA) == "env-token" and tokens(PROD_SA) == "env-token" and len(seen) == 1
    assert seen[0].url.path == f"/v1/projects/-/serviceAccounts/{PROD_SA}:generateAccessToken" and seen[0].headers["Authorization"] == "Bearer host"
    assert json.loads(seen[0].content) == {"scope": ["https://www.googleapis.com/auth/cloud-platform"], "lifetime": "900s"}


def test_a_host_that_may_not_act_as_the_environment_is_told_which_role_it_needs():
    tokens = ServiceAccountTokens(google(lambda request: httpx.Response(403, json={})), lambda: "host")
    with pytest.raises(DeliveryError) as caught:
        tokens(PROD_SA)
    assert caught.value.code == ErrorCode.GOOGLE_AUTH_FAILED and "serviceAccountTokenCreator" in caught.value.message


def secret_manager(value=SECRET_VALUE, status=200):
    def handle(request):
        assert request.url.path == "/v1/projects/infra-project/secrets/shop-db-password/versions/latest:access"
        assert request.headers["Authorization"] == "Bearer env-token"
        if status != 200:
            return httpx.Response(status, json={})
        return httpx.Response(200, json={"payload": {"data": base64.b64encode(value.encode()).decode()}})
    return google(handle)


def test_a_secret_is_read_as_the_environment():
    assert read_secret(secret_manager(), "env-token", "projects/infra-project/secrets/shop-db-password") == SECRET_VALUE


def test_a_secret_this_environment_may_not_read_names_the_grant():
    with pytest.raises(DeliveryError) as caught:
        read_secret(secret_manager(status=403), "env-token", "projects/infra-project/secrets/shop-db-password")
    assert caught.value.code == ErrorCode.SECRET_UNAVAILABLE and "access grant" in caught.value.message


def test_bindings_become_values_and_the_env_file_has_one_line_each():
    release = ReleaseRequest.model_validate(release_dict())
    values = bindings.resolve(release, InfraRequest.model_validate(infra_response()), secret_manager(), "env-token")
    assert values == {"LOG_LEVEL": "info", "DB_HOST": "10.20.0.5", "DB_PASSWORD": SECRET_VALUE}
    assert bindings.env_file(values) == f"LOG_LEVEL=info\nDB_HOST=10.20.0.5\nDB_PASSWORD={SECRET_VALUE}\n"


def test_a_secret_value_docker_cannot_carry_is_refused_without_showing_it():
    release = ReleaseRequest.model_validate(release_dict())
    with pytest.raises(DeliveryError) as caught:
        bindings.resolve(release, InfraRequest.model_validate(infra_response()), secret_manager(value="-----BEGIN KEY-----\nabc"), "env-token")
    assert caught.value.code == ErrorCode.INVALID_BINDING and "BEGIN" not in json.dumps(caught.value.to_dict())


def test_an_attested_image_passes_and_cosign_gets_a_login_of_its_own():
    config = make_config()
    runner = Runner(Completed(0, "[]", ""))
    result = attest.verify(ReleaseRequest.model_validate(release_dict()), config.environments["prod"], "env-token", runner)
    assert result == {"signer": "/etc/appdelivery/keys/prod.pub", "type": "slsaprovenance1"}
    call = runner.calls[0]
    assert call["argv"] == ["cosign", "verify-attestation", "--type", "slsaprovenance1", "--key", "/etc/appdelivery/keys/prod.pub", "--output", "json", IMAGE]
    login = call["docker_config"]["auths"]["us-central1-docker.pkg.dev"]["auth"]
    assert base64.b64decode(login).decode() == "oauth2accesstoken:env-token"
    assert "env-token" not in " ".join(call["argv"])


def test_the_next_signer_is_tried_and_every_refusal_is_reported():
    config = make_config()
    environment = config.environments["prod"]
    environment.trusted_signers = ["/etc/appdelivery/keys/old.pub", "/etc/appdelivery/keys/prod.pub"]
    runner = Runner(Completed(1, "", "no matching attestations"), Completed(0, "[]", ""))
    assert attest.verify(ReleaseRequest.model_validate(release_dict()), environment, "t", runner)["signer"].endswith("prod.pub")
    with pytest.raises(DeliveryError) as caught:
        attest.verify(ReleaseRequest.model_validate(release_dict()), environment, "t", Runner(Completed(1, "", "no matching attestations")))
    assert caught.value.code == ErrorCode.ATTESTATION_FAILED and len(caught.value.details["tried"]) == 2


def test_a_keyless_signer_is_checked_by_identity_and_issuer():
    config = make_config()
    environment = config.environments["prod"]
    environment.trusted_signers = [KeylessSigner(identity="release@acme-ci.iam.gserviceaccount.com", issuer="https://accounts.google.com")]
    runner = Runner(Completed(0, "[]", ""))
    attest.verify(ReleaseRequest.model_validate(release_dict()), environment, "t", runner)
    assert runner.calls[0]["argv"][4:8] == ["--certificate-identity", "release@acme-ci.iam.gserviceaccount.com", "--certificate-oidc-issuer", "https://accounts.google.com"]


def test_a_registry_outside_artifact_registry_gets_no_login():
    release = ReleaseRequest.model_validate(release_dict(image=f"ghcr.io/acme/web@sha256:{DIGEST}"))
    runner = Runner(Completed(0, "[]", ""))
    attest.verify(release, make_config().environments["prod"], "env-token", runner)
    assert runner.calls[0]["docker_config"] == {"auths": {}}


def test_only_gcloud_and_cosign_may_run():
    with pytest.raises(ValueError, match="runs only"):
        remote.run(["sh", "-c", "id"])


def test_an_expired_host_token_is_replaced_once():
    """gcloud may hand back a token with only minutes left (found on 2026-10-29)."""
    issued = iter(["stale", "fresh"])
    host = HostToken(google(no_metadata), runner=lambda argv, stdin=None, env=None, timeout=60: Completed(0, next(issued) + "\n", ""))
    seen = []

    def handle(request):
        seen.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer stale":
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"accessToken": "env-token"})
    assert ServiceAccountTokens(google(handle), host)(PROD_SA) == "env-token" and seen == ["Bearer stale", "Bearer fresh"]
