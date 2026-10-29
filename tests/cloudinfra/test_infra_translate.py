"""Generic request → Google Cloud shapes, and the estimate from list prices."""
import pytest
from gcp_fakes import make_config, make_provider

from cloudinfra.models import InfrastructureRequest
from cloudinfra.orchestration import planner
from cloudinfra.providers.base import ProvisionContext
from cloudinfra.providers.gcp import pricing, translate


@pytest.mark.parametrize("cpu, memory, expected", [
    (4, 16, "e2-standard-4"), (2, 16, "e2-highmem-2"), (8, 8, "e2-highcpu-8"), (32, 128, "e2-standard-32"),
    (4, 12, "e2-custom-4-12288"), (2, 1, "e2-custom-2-1024"), (6, 24, "e2-custom-6-24576"),
    (1, 1, "e2-micro"), (1, 2, "e2-small"), (1, 4, "e2-medium"), (1, 3, "e2-medium"),
    (3, 12, None), (1, 8, None), (4, 64, None), (34, 136, None), (4, 1, None),
])
def test_machine_type(cpu, memory, expected):
    assert translate.machine_type(cpu, memory) == expected
    if expected:
        shape = translate.machine_shape(expected)
        assert shape[0] == cpu and (shape[1] == memory or cpu == 1)


def test_images_versions_and_tiers():
    assert translate.image("ubuntu").endswith("ubuntu-2404-lts-amd64") and translate.image("debian").endswith("debian-12")
    from cloudinfra.models import DatabaseRequest
    assert translate.database_version(DatabaseRequest()) == ("18", "POSTGRES_18")
    assert translate.database_version(DatabaseRequest(engine="mysql")) == ("8.4", "MYSQL_8_4")
    assert translate.database_version(DatabaseRequest(version="13")) == ("13", "POSTGRES_13") and "13" in translate.EXTENDED_SUPPORT["postgresql"]
    assert translate.database_version(DatabaseRequest(engine="mysql", version="8.4")) == ("8.4", "MYSQL_8_4")
    assert translate.DB_TIERS == {"small": "db-f1-micro", "medium": "db-custom-2-7680", "large": "db-custom-4-15360"}


def test_labels_carry_ownership_and_are_legal():
    ctx = ProvisionContext(request_id="A" * 32, name="analytics", region="us-central1", zone="us-central1-a", owner="App.Layer", labels={"team": "Data Science"})
    labels = translate.labels(ctx)
    assert labels["managed-by"] == "cloudinfra" and labels["cloudinfra-request"] == "a" * 32 and labels["cloudinfra-owner"] == "app-layer" and labels["team"] == "data-science"
    assert all(translate.LABEL_CHARS.search(value) is None for value in labels.values())


def test_plan_names_shapes_and_order():
    config = make_config()
    provider, _ = make_provider(config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "analytics", "compute": {"count": 2, "cpu": 4, "memory_gb": 16, "public_ip": True},
                                                   "storage": {"size_gb": 200}, "database": {"size": "medium"}, "network": {"public_access": True}})
    ctx = planner.context(request, "abcdef" + "0" * 26, "app-layer", config, provider)
    planned = planner.plan(request, ctx, provider)
    assert [(item.kind, item.name) for item in planned] == [("network", "analytics-net"), ("storage", "analytics-vm-disk-1"), ("storage", "analytics-vm-disk-2"),
                                                            ("compute", "analytics-vm-1"), ("compute", "analytics-vm-2"), ("database", "analytics-db-abcdef"),
                                                            ("secret", "analytics-db-password-abcdef")]
    assert all(item.spec["resource_id"].startswith({"network": "net-", "storage": "sto-", "compute": "cmp-", "database": "db-", "secret": "sec-"}[item.kind]) for item in planned)
    assert planned[1].spec["attach_to"] == "analytics-vm-1" and planned[3].spec["machine_type"] == "e2-standard-4" and planned[3].spec["zone"] == "us-central1-a"
    assert planned[5].spec == {**planned[5].spec, "database_version": "POSTGRES_18", "tier": "db-custom-2-7680", "private": True, "network": "analytics-net", "port": 5432}
    assert "servicenetworking.connections.create" in planned[5].provider_mapping and "compute.instances.insert" == planned[3].provider_mapping


def test_estimate_lines_and_total():
    config = make_config()
    provider, _ = make_provider(config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "compute": {"cpu": 4, "memory_gb": 16, "public_ip": True}, "storage": {"size_gb": 100, "performance": "ssd"},
                                                   "database": {"public_access": True, "high_availability": True}})
    planned = planner.plan(request, planner.context(request, "a" * 32, "x", config, provider), provider)
    estimate = provider.estimate_cost(planned)
    by_kind = {line.kind: line for line in estimate.lines}
    machine = (4 * pricing.PRICING["e2_vcpu_hour"] + 16 * pricing.PRICING["e2_gb_hour"]) * 730 + 20 * 0.10 + 0.005 * 730
    assert by_kind["compute"].monthly_usd == round(machine, 2) and "public address" in by_kind["compute"].basis
    assert by_kind["storage"].monthly_usd == 17.0
    assert by_kind["database"].monthly_usd == round(7.67 * 2 + 10 * 0.17, 2) and "high availability" in by_kind["database"].basis
    assert estimate.monthly_usd == round(sum(line.monthly_usd for line in estimate.lines), 2)
    assert estimate.verified is None and "Never a bill" in estimate.note and estimate.currency == "USD"
