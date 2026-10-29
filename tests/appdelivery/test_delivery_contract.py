"""The release contract and the configuration: what is accepted, what is refused, and with which code."""
import pydantic
import pytest
from delivery_fakes import DIGEST, config_dict, make_config, release_dict

from appdelivery.config import Config
from appdelivery.errors import ErrorCode, problems_from
from appdelivery.models import ReleaseRequest, looks_like_credential


def refused(data):
    with pytest.raises(pydantic.ValidationError) as caught:
        ReleaseRequest.model_validate(data)
    return problems_from(caught.value)


def test_a_release_with_an_image_by_digest_and_three_kinds_of_binding_is_accepted():
    release = ReleaseRequest.model_validate(release_dict())
    assert release.registry() == "us-central1-docker.pkg.dev"
    assert release.repository() == "us-central1-docker.pkg.dev/acme-apps/shop/web"
    assert {name: binding.source() for name, binding in release.env.items()} == {"LOG_LEVEL": "value", "DB_HOST": "endpoint database_host",
                                                                                "DB_PASSWORD": "secret db-password"}


def test_a_tag_is_refused_because_it_can_change_after_the_check():
    problems = refused(release_dict(image="us-central1-docker.pkg.dev/acme-apps/shop/web:1.4.2"))
    assert problems[0].code == ErrorCode.INVALID_RELEASE and "digest" in problems[0].message


@pytest.mark.parametrize("image", [f"web@sha256:{DIGEST}", f"US-central1-docker.pkg.dev/a/b@sha256:{DIGEST}", "us-central1-docker.pkg.dev/a/b@sha256:abc",
                                   f"us-central1-docker.pkg.dev/a/b@sha256:{DIGEST} ; rm -rf /"])
def test_a_malformed_image_reference_is_refused(image):
    assert refused(release_dict(image=image))[0].code == ErrorCode.INVALID_RELEASE


@pytest.mark.parametrize("field", ["command", "args", "entrypoint", "volumes", "privileged", "dockerfile", "repository", "build"])
def test_a_field_that_could_run_or_build_something_is_not_part_of_a_release(field):
    problems = refused(release_dict(**{field: "anything"}))
    assert problems[0].code == ErrorCode.INVALID_RELEASE and "not a field of a release" in problems[0].message


def test_a_binding_takes_exactly_one_source():
    assert refused(release_dict(env={"A": {"value": "1", "endpoint": "database_host"}}))[0].code == ErrorCode.INVALID_BINDING
    assert refused(release_dict(env={"A": {}}))[0].code == ErrorCode.INVALID_BINDING


def test_an_unknown_endpoint_is_refused():
    assert refused(release_dict(env={"A": {"endpoint": "public_ips"}}))[0].code == ErrorCode.INVALID_RELEASE


@pytest.mark.parametrize("name", ["DB_PASSWORD", "API_KEY", "GITHUB_TOKEN", "OAUTH_AUTH", "PRIVATE_KEY", "STRIPE_SECRET", "AWS_ACCESS_KEY"])
def test_a_literal_under_a_credential_name_is_refused(name):
    problems = refused(release_dict(env={name: {"value": "hunter2"}}))
    assert problems[0].code == ErrorCode.INVALID_BINDING and "secret" in problems[0].message
    assert "hunter2" not in problems[0].message


@pytest.mark.parametrize("name", ["AUTHOR_NAME", "TOKENIZER_MODEL", "KEYBOARD_LAYOUT", "LOG_LEVEL"])
def test_ordinary_names_are_not_mistaken_for_credentials(name):
    assert not looks_like_credential(name)


def test_a_literal_with_a_line_break_is_refused():
    assert refused(release_dict(env={"GREETING": {"value": "a\nb"}}))[0].code == ErrorCode.INVALID_BINDING


@pytest.mark.parametrize("name", ["lower", "1ST", "WITH-DASH", "A" * 65])
def test_environment_names_follow_the_shell_rules(name):
    assert refused(release_dict(env={name: {"value": "x"}}))[0].code == ErrorCode.INVALID_BINDING


@pytest.mark.parametrize("path", ["healthz", "/a b", "/x;reboot", "/$(id)"])
def test_a_health_path_that_is_not_a_plain_path_is_refused(path):
    assert refused(release_dict(health={"path": path}))[0].code == ErrorCode.INVALID_RELEASE


def test_port_22_is_refused_because_it_is_the_way_in():
    assert refused(release_dict(port=22))[0].code == ErrorCode.INVALID_RELEASE


def test_a_request_id_and_a_group_are_checked():
    assert refused(release_dict(request_id="not-an-id"))[0].code == ErrorCode.INVALID_RELEASE
    assert refused(release_dict(target={"group": "Web_Servers"}))[0].code == ErrorCode.INVALID_RELEASE


def test_the_example_configuration_loads():
    config = make_config()
    assert config.environments["prod"].require_attestation and config.environments["dev"].allowed_registries


def test_attestation_without_a_trusted_signer_is_refused():
    data = config_dict()
    data["environments"]["prod"]["trusted_signers"] = []
    with pytest.raises(pydantic.ValidationError, match="trusted_signers"):
        Config.model_validate(data)


def test_a_signer_must_be_a_kms_key_a_key_file_or_a_keyless_identity():
    data = config_dict()
    data["environments"]["prod"]["trusted_signers"] = ["https://evil.example.com/key"]
    with pytest.raises(pydantic.ValidationError, match="neither a gcpkms"):
        Config.model_validate(data)
    data["environments"]["prod"]["trusted_signers"] = ["gcpkms://projects/acme-sec/locations/global/keyRings/release/cryptoKeys/attestor",
                                                       {"identity": "release@acme.iam.gserviceaccount.com", "issuer": "https://accounts.google.com"}]
    assert len(Config.model_validate(data).environments["prod"].trusted_signers) == 2


def test_environments_need_their_own_service_accounts_and_principals_known_environments():
    data = config_dict()
    data["environments"]["dev"]["service_account"] = data["environments"]["prod"]["service_account"]
    with pytest.raises(pydantic.ValidationError, match="own service_account"):
        Config.model_validate(data)
    data = config_dict()
    data["principals"][0]["environments"] = ["staging"]
    with pytest.raises(pydantic.ValidationError, match="unknown environments"):
        Config.model_validate(data)
