"""A request may carry several clusters (docs/DELIVERY-SPEC.md, phase 3, step 1): each named, each its own provider
cluster `<request>-<name>`, its own namespace named after the request and its own grants; one cluster without a name
stays `<request>-cluster`, as before."""
import pytest
from gcp_fakes import make_orchestrator, principal
from pydantic import ValidationError

from cloudinfra.models import InfrastructureRequest
from cloudinfra.models.errors import ErrorCode, InfrastructureError

DELIVERY = "serviceAccount:appdelivery-dev@example.com"


def provision(tmp_path, clusters, access=None):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    body = {"region": "us-central1", "name": "shop", "cluster": clusters, "access": access or []}
    response = orchestrator.provision(InfrastructureRequest.model_validate(body), who, wait_seconds=45)
    return orchestrator, fake, who, response


def test_each_cluster_of_a_list_is_its_own_cluster_with_its_own_namespace_and_grants(tmp_path):
    clusters = [{"name": "blue", "node_count": 1}, {"name": "green", "node_count": 2, "node_cpu": 4, "node_memory_gb": 16}]
    _, fake, _, response = provision(tmp_path, clusters, access=[{"member": DELIVERY, "level": "admin"}])
    assert response.error is None, response.error
    found = {cluster.name: cluster for cluster in response.resources.cluster}
    assert set(found) == {"shop-blue", "shop-green"} and {name for _, name in fake.clusters} == {"shop-blue", "shop-green"}
    assert found["shop-green"].node_count == 2 and found["shop-green"].machine_type == "e2-standard-4"
    assert found["shop-blue"].endpoint != found["shop-green"].endpoint
    namespace = f"shop-{response.request_id[:6]}"
    assert all(cluster.namespace == namespace for cluster in found.values())
    assert fake.namespaces == {found["shop-blue"].endpoint: {namespace}, found["shop-green"].endpoint: {namespace}}
    assert {(endpoint, item["role"]) for (endpoint, _, _), item in fake.role_bindings.items()} == {
        (found["shop-blue"].endpoint, "edit"), (found["shop-green"].endpoint, "edit")}
    [entry] = response.access
    assert entry.status == "GRANTED" and set(entry.resources) == {cluster.id for cluster in found.values()}


def test_one_cluster_without_a_name_keeps_its_old_name(tmp_path):
    _, fake, _, response = provision(tmp_path, {"node_count": 1})
    assert [cluster.name for cluster in response.resources.cluster] == ["shop-cluster"]
    (tmp_path / "list").mkdir()
    _, fake, _, response = provision(tmp_path / "list", [{"node_count": 1}])
    assert [cluster.name for cluster in response.resources.cluster] == ["shop-cluster"], "a list of one needs no name either"


def test_destroying_the_request_deletes_every_cluster(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, [{"name": "blue"}, {"name": "green"}])
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED" and not fake.clusters


@pytest.mark.parametrize("clusters, said", [
    ([{"name": "blue"}, {}], "every cluster of a list needs a name"),
    ([{"name": "blue"}, {"name": "blue"}], "names must be unique"),
    ([{"name": f"c{index}"} for index in range(5)], "at most 4 per request"),
    ([], "a list cannot be empty"),
    ([{"name": "Blue"}], "pattern"),
])
def test_a_list_of_clusters_is_checked(clusters, said):
    with pytest.raises(ValidationError) as caught:
        InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": clusters})
    assert said in str(caught.value)


def test_the_node_limit_counts_every_cluster_of_the_request(tmp_path):
    orchestrator, _ = make_orchestrator(tmp_path)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop",
                                                    "cluster": [{"name": "blue", "node_count": 12}, {"name": "green", "node_count": 12}]})
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert caught.value.code in (ErrorCode.LIMIT_EXCEEDED, ErrorCode.INVALID_REQUEST) and "24 asked" in str(caught.value.to_dict())


def test_the_plan_names_and_prices_each_cluster(tmp_path):
    orchestrator, _ = make_orchestrator(tmp_path)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": [{"name": "blue"}, {"name": "green"}]})
    plan = orchestrator.plan(request, principal(orchestrator.config))
    assert [item.name for item in plan.planned if item.kind == "cluster"] == ["shop-blue", "shop-green"]
    assert len([line for line in plan.estimate.lines if line.kind == "cluster"]) == 2
