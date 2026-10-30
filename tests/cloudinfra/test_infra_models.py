"""The provider-neutral contract: what it accepts, what it refuses, and that it carries nothing about an application."""
import pydantic
import pytest

from cloudinfra.models import InfrastructureRequest, InfrastructureResponse, RequestStatus
from cloudinfra.models.errors import ErrorCode, InfrastructureError, problems_from
from cloudinfra.security import validation


def test_minimal_request_is_one_machine_and_nothing_else():
    request = InfrastructureRequest.model_validate({"region": "us-central1", "compute": {"cpu": 4, "memory_gb": 16}})
    assert request.provider == "gcp" and request.kinds() == ["compute"] and request.compute.count == 1 and request.compute.os is None
    assert request.compute.os_family == "ubuntu" and request.compute.profile is None and request.lifetime is None and request.access == []
    assert request.storage is None and request.database is None and request.network is None


def test_request_carries_no_application_knowledge():
    """The design test's precondition: no field can name a repository, an image, a command, a script or a key."""
    schema = InfrastructureRequest.model_json_schema()
    words = {name.lower() for definition in [schema, *schema.get("$defs", {}).values()] for name in definition.get("properties", {})}
    for forbidden in ("repository", "repo", "github", "image", "dockerfile", "container", "command", "script", "startup_script", "ssh_key", "key", "source", "build", "test", "workflow"):
        assert forbidden not in words, forbidden


@pytest.mark.parametrize("bad, code, text", [
    ({"region": "us-central1"}, ErrorCode.INVALID_REQUEST, "at least one of identity, network, storage, compute"),
    ({"region": "US Central", "compute": {"cpu": 2, "memory_gb": 8}}, ErrorCode.INVALID_REGION, "not a region name"),
    ({"region": "us-central1", "compute": {"cpu": "4", "memory_gb": 16}}, ErrorCode.INVALID_REQUEST, "integer"),
    ({"region": "us-central1", "compute": {"cpu": 4, "memory_gb": 16, "image": "x"}}, ErrorCode.INVALID_REQUEST, "not a field"),
    ({"region": "us-central1", "name": "Bad_Name", "compute": {"cpu": 4, "memory_gb": 16}}, ErrorCode.INVALID_RESOURCE_NAME, "lowercase"),
    ({"region": "us-central1", "labels": {"managed-by": "me"}, "compute": {"cpu": 4, "memory_gb": 16}}, ErrorCode.INVALID_REQUEST, "reserved"),
    ({"region": "us-central1", "network": {"cidr": "8.8.8.0/24"}}, ErrorCode.INVALID_NETWORK_CONFIGURATION, "private range"),
    ({"region": "us-central1", "network": {"cidr": "10.0.0.0/7"}}, ErrorCode.INVALID_NETWORK_CONFIGURATION, "prefix"),
    ({"region": "us-central1", "network": {"allowed_ports": [0]}}, ErrorCode.INVALID_NETWORK_CONFIGURATION, "1 to 65535"),
    ({"region": "us-central1", "network": {"allowed_ports": [80, 80]}}, ErrorCode.INVALID_NETWORK_CONFIGURATION, "unique"),
    ({"region": "us-central1", "network": {"allowed_sources": ["everywhere"]}}, ErrorCode.INVALID_NETWORK_CONFIGURATION, "IPv4 range"),
    ({"region": "us-central1", "database": {"engine": "oracle"}}, ErrorCode.INVALID_REQUEST, "database.engine"),
    ({"region": "us-central1", "database": {"version": "sixteen"}}, ErrorCode.INVALID_REQUEST, "database.version"),
    ({"region": "us-central1", "compute": {"cpu": 4, "memory_gb": 16, "network_id": "not-an-id"}}, ErrorCode.NOT_FOUND, "not an identifier"),
    ({"region": "us-central1", "compute": {"cpu": 0, "memory_gb": 16}}, ErrorCode.INVALID_REQUEST, "compute.cpu"),
])
def test_contract_refusals_carry_a_code_and_a_sentence(bad, code, text):
    with pytest.raises(pydantic.ValidationError) as caught:
        InfrastructureRequest.model_validate(bad)
    problems = problems_from(caught.value)
    assert problems and problems[0].code == code, problems
    assert text in problems[0].message, problems[0].message


def test_every_problem_is_reported_not_only_the_first():
    with pytest.raises(pydantic.ValidationError) as caught:
        InfrastructureRequest.model_validate({"region": "nope", "compute": {"cpu": 4, "memory_gb": 16, "extra": 1}, "network": {"cidr": "1.2.3.4/32"}})
    codes = sorted(item.code.value for item in problems_from(caught.value))
    assert codes == ["INVALID_NETWORK_CONFIGURATION", "INVALID_REGION", "INVALID_REQUEST"]


def test_structured_error_shape():
    error = InfrastructureError(ErrorCode.INVALID_COMPUTE_CONFIGURATION, "Requested machine configuration is not supported.", {"cpu": 3})
    assert error.to_dict() == {"status": "FAILED", "error": {"code": "INVALID_COMPUTE_CONFIGURATION", "message": "Requested machine configuration is not supported.",
                                                              "details": {"cpu": 3}, "resource": None}}
    assert str(error) == "INVALID_COMPUTE_CONFIGURATION: Requested machine configuration is not supported."


def test_validation_helpers():
    assert validation.check_cidr("192.168.1.0/24") == "192.168.1.0/24"
    assert validation.check_sources(["0.0.0.0/0", "10.0.0.1"]) == ["0.0.0.0/0", "10.0.0.1/32"]
    assert validation.check_ports([443, 80]) == [80, 443]
    with pytest.raises(InfrastructureError) as caught:
        validation.check_name("x" * 21)
    assert caught.value.code == ErrorCode.INVALID_RESOURCE_NAME
    with pytest.raises(InfrastructureError):
        validation.check_labels({f"k{i}": "v" for i in range(17)})
    with pytest.raises(InfrastructureError):
        validation.check_request_id("short")


def test_response_model_round_trips_as_json():
    response = InfrastructureResponse(request_id="a" * 32, name="x", provider="gcp", region="us-central1", status=RequestStatus.READY, created_at="t", updated_at="t")
    data = response.model_dump(mode="json")
    kinds = ("identity", "network", "storage", "compute", "database", "cache", "queue", "secret", "cluster", "service", "job", "load_balancer", "dns", "monitor")
    assert data["status"] == "READY" and data["resources"] == {kind: [] for kind in kinds}
    assert data["credentials"] is None
    assert InfrastructureResponse.model_validate(data) == response
