"""Reading a request from the infrastructure layer over HTTP, and the shape of its answer."""
import httpx
import pytest
from delivery_fakes import REQUEST_ID, cluster_response, infra_response, infra_transport, make_config

from appdelivery.errors import DeliveryError, ErrorCode
from appdelivery.infra import InfraClient


def client(**kwargs):
    seen = []
    return InfraClient("https://infra.example.com/", "infra-token", httpx.Client(transport=infra_transport(seen=seen, **kwargs))), seen


def test_the_fake_answer_is_one_the_infrastructure_layer_itself_would_give():
    """A contract test: the fixture every delivery test uses validates against cloudinfra's own response model, so a change
    of that contract shows here rather than on a real machine."""
    from cloudinfra.models.response import InfrastructureResponse
    InfrastructureResponse.model_validate(infra_response())
    with_cluster = InfrastructureResponse.model_validate(cluster_response())
    assert with_cluster.resources.cluster[0].namespace == "shop", "the cluster's namespace and CA are cloudinfra's own fields"


def test_a_request_is_read_with_the_environment_token_and_kept_to_what_a_release_needs():
    infra, seen = client()
    request = infra.get_request(REQUEST_ID)
    assert seen[0].url == f"https://infra.example.com/infrastructure/{REQUEST_ID}" and seen[0].headers["Authorization"] == "Bearer infra-token"
    assert request.owner == "shop-team" and [machine.name for machine in request.group("web")] == ["shop-web-1", "shop-web-2"]
    assert request.secret("db-password").resource_name == "projects/infra-project/secrets/shop-db-password"
    assert request.secret_names() == ["db-password"] and request.network("net-aaaaaaaaaaaa").admits(22, "35.235.240.0/20")


def test_fields_the_layer_adds_later_are_ignored():
    answer = infra_response()
    answer["something_new"] = {"x": 1}
    answer["resources"]["compute"][0]["new_field"] = True
    infra, _ = client(answer=answer)
    assert infra.get_request(REQUEST_ID).request_id == REQUEST_ID


@pytest.mark.parametrize(("status", "code"), [(404, ErrorCode.NOT_FOUND), (401, ErrorCode.INFRA_AUTH_FAILED), (403, ErrorCode.INFRA_AUTH_FAILED),
                                              (502, ErrorCode.INFRA_UNAVAILABLE)])
def test_each_refusal_of_the_layer_has_its_own_code(status, code):
    infra, _ = client(status=status)
    with pytest.raises(DeliveryError) as caught:
        infra.get_request(REQUEST_ID)
    assert caught.value.code == code


def test_an_unreachable_layer_is_infra_unavailable():
    def fail(request):
        raise httpx.ConnectError("refused")
    infra = InfraClient("https://infra.example.com", "t", httpx.Client(transport=httpx.MockTransport(fail)))
    with pytest.raises(DeliveryError) as caught:
        infra.get_request(REQUEST_ID)
    assert caught.value.code == ErrorCode.INFRA_UNAVAILABLE


def test_the_token_comes_from_the_environment_variable_the_configuration_names(monkeypatch):
    config = make_config()
    monkeypatch.delenv("APPDELIVERY_PROD_INFRA_TOKEN", raising=False)
    with pytest.raises(DeliveryError) as caught:
        InfraClient.for_environment(config, "prod")
    assert caught.value.code == ErrorCode.INFRA_AUTH_FAILED and "APPDELIVERY_PROD_INFRA_TOKEN" in caught.value.message
    monkeypatch.setenv("APPDELIVERY_PROD_INFRA_TOKEN", "from-env")
    assert InfraClient.for_environment(config, "prod").token == "from-env"
