"""Provider selection, the operator's regions and limits, and the rules that tie a request's blocks together."""
import pytest
from gcp_fakes import VM_REQUEST, make_config, make_orchestrator, make_provider, principal

from cloudinfra.models import InfrastructureRequest
from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.orchestration import planner, validator
from cloudinfra.providers import PLANNED_PROVIDERS, ProviderRegistry


def problems(data, config=None):
    config = config or make_config()
    provider, _ = make_provider(config)
    return validator.validate(InfrastructureRequest.model_validate(data), config, provider)


def test_unsupported_provider_is_a_structured_refusal():
    registry = ProviderRegistry(make_config())
    for name in ("aws", "azure", "jetstream", "digitalocean"):
        with pytest.raises(InfrastructureError) as caught:
            registry.get(name)
        assert caught.value.code == ErrorCode.UNSUPPORTED_PROVIDER and "gcp" in caught.value.message
        if name in PLANNED_PROVIDERS:
            assert "Planned adapters" in caught.value.message
    assert registry.names() == ["gcp"]


def test_validate_tool_reports_unsupported_provider_without_raising(tmp_path):
    orchestrator, _ = make_orchestrator(tmp_path)
    result = orchestrator.validate(InfrastructureRequest.model_validate({**VM_REQUEST, "provider": "aws"}), principal(orchestrator.config))
    assert result.valid is False and result.problems[0].code == ErrorCode.UNSUPPORTED_PROVIDER


def test_region_must_be_allowed_by_the_operator():
    found = problems({**VM_REQUEST, "region": "asia-east1"})
    assert [item.code for item in found] == [ErrorCode.INVALID_REGION] and "allowed: us-central1, europe-west1" in found[0].message


def test_region_the_adapter_does_not_know_needs_zones():
    config = make_config(providers={"gcp": {"project_id": "test-project", "allowed_regions": ["mars-north1"]}})
    found = problems({**VM_REQUEST, "region": "mars-north1"}, config)
    assert found[0].code == ErrorCode.INVALID_REGION and "providers.gcp.zones" in found[0].message
    config = make_config(providers={"gcp": {"project_id": "test-project", "allowed_regions": ["mars-north1"], "zones": {"mars-north1": ["a"]}}})
    assert problems({**VM_REQUEST, "region": "mars-north1", "zone": "mars-north1-a"}, config) == []
    assert problems({**VM_REQUEST, "region": "mars-north1", "zone": "mars-north1-z"}, config)[0].field == "zone"


@pytest.mark.parametrize("compute, text", [
    ({"cpu": 3, "memory_gb": 12}, "even number"),
    ({"cpu": 1, "memory_gb": 16}, "1 CPU allows"),
    ({"cpu": 4, "memory_gb": 1}, "0.5 to 8 GB"),
    ({"cpu": 4, "memory_gb": 64}, "0.5 to 8 GB"),
    ({"cpu": 64, "memory_gb": 256}, "2 to 32"),
])
def test_invalid_compute_configuration(compute, text):
    found = problems({"region": "us-central1", "compute": compute})
    assert found and found[0].code == ErrorCode.INVALID_COMPUTE_CONFIGURATION and text in found[0].message


def test_limits_are_cost_protection():
    config = make_config(limits={"max_instances_per_request": 2, "max_cpu_per_instance": 8, "max_memory_gb_per_instance": 32, "max_storage_gb_per_request": 500, "max_database_storage_gb": 50})
    found = problems({"region": "us-central1", "compute": {"count": 3, "cpu": 16, "memory_gb": 64}, "storage": {"size_gb": 400}, "database": {"storage_gb": 100, "public_access": True}}, config)
    assert sorted({item.code for item in found}) == [ErrorCode.LIMIT_EXCEEDED]
    assert sorted(item.field for item in found) == ["compute.count", "compute.cpu", "compute.memory_gb", "database.storage_gb", "storage.size_gb"]


def test_private_database_needs_a_network():
    found = problems({"region": "us-central1", "database": {"engine": "postgresql"}})
    assert found[0].code == ErrorCode.INVALID_DATABASE_CONFIGURATION and "network" in found[0].message
    assert problems({"region": "us-central1", "database": {"engine": "postgresql"}, "network": {}}) == []
    assert problems({"region": "us-central1", "database": {"engine": "postgresql", "network_id": "net-0123456789ab"}}) == []
    assert problems({"region": "us-central1", "database": {"engine": "postgresql", "public_access": True}}) == []
    found = problems({"region": "us-central1", "database": {"network_id": "net-0123456789ab"}, "network": {}})
    assert "choose one" in found[0].message


def test_public_ip_needs_public_access_on_the_requests_network():
    found = problems({"region": "us-central1", "compute": {"cpu": 2, "memory_gb": 8, "public_ip": True}, "network": {"public_access": False}})
    assert found[0].code == ErrorCode.INVALID_NETWORK_CONFIGURATION
    assert problems({"region": "us-central1", "compute": {"cpu": 2, "memory_gb": 8, "public_ip": True}, "network": {"public_access": True}}) == []
    assert problems({"region": "us-central1", "compute": {"cpu": 2, "memory_gb": 8, "public_ip": True}}) == []


def test_database_version_must_be_offered():
    found = problems({"region": "us-central1", "database": {"engine": "mysql", "version": "9.9", "public_access": True}})
    assert found[0].code == ErrorCode.INVALID_DATABASE_CONFIGURATION and "5.7, 8.0, 8.4, 9.7" in found[0].message


def test_cost_ceiling_per_service_and_per_caller():
    config = make_config(limits={"max_monthly_cost_usd": 100.0})
    provider, _ = make_provider(config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "compute": {"count": 2, "cpu": 8, "memory_gb": 32}})
    ctx = planner.context(request, "a" * 32, "app-layer", config, provider)
    estimate = provider.estimate_cost(planner.plan(request, ctx, provider))
    problem = planner.cost_problem(estimate, config, principal(config))
    assert problem.code == ErrorCode.COST_LIMIT_EXCEEDED and "100.00 USD" in problem.message
    tighter = make_config(principals=[{"name": "thrifty", "token_sha256": "0" * 64, "max_monthly_cost_usd": 5.0}])
    small = InfrastructureRequest.model_validate({"region": "us-central1", "compute": {"cpu": 1, "memory_gb": 1}})
    estimate = provider.estimate_cost(planner.plan(small, planner.context(small, "a" * 32, "thrifty", tighter, provider), provider))
    assert planner.cost_problem(estimate, tighter, tighter.principals[0]).code == ErrorCode.COST_LIMIT_EXCEEDED
    assert planner.cost_problem(estimate, config, principal(config)) is None


def test_raise_first_keeps_every_problem_in_details():
    found = problems({"region": "asia-east1", "compute": {"cpu": 3, "memory_gb": 1}})
    with pytest.raises(InfrastructureError) as caught:
        validator.raise_first(found)
    assert caught.value.code == ErrorCode.INVALID_REGION and len(caught.value.details["problems"]) == 2
