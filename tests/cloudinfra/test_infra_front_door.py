"""A cluster's front door (docs/DELIVERY-SPEC.md, phase 3, step 5): this service reserves a public address, makes a
Google-managed certificate for the hostnames, turns on Gateway API and puts one Gateway in its own namespace that takes
routes from the request's namespace only; a namespace with edit may write HTTPRoutes. On delete the Gateway goes first and
the cluster waits for GKE's forwarding rules to go, so no load balancer outlives it."""
import pytest
from gcp_fakes import config_dict, make_orchestrator, principal
from pydantic import ValidationError

from cloudinfra.config import Config
from cloudinfra.models import InfrastructureRequest
from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.models.resources import ResourceKind
from cloudinfra.providers.gcp.kubernetes import DOOR_NAMESPACE, GATEWAY_CLASS, GATEWAYS, ROUTES

HOST = "shop.example.com"


def orchestrator_with(tmp_path, domains=("example.com",)):
    data = config_dict()
    data["database"] = str(tmp_path / "cloudinfra.db")
    data["providers"]["gcp"]["public_domains"] = list(domains)
    return make_orchestrator(tmp_path, config=Config.model_validate(data))


def provision(tmp_path, public, domains=("example.com",)):
    orchestrator, fake = orchestrator_with(tmp_path, domains)
    who = principal(orchestrator.config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": {"public": public}})
    return orchestrator, fake, who, orchestrator.provision(request, who, wait_seconds=45)


def objects(fake, endpoint):
    return {path: body for (where, path), body in fake.cluster_objects.items() if where == endpoint}


def test_a_front_door_with_hostnames_serves_https_on_a_reserved_address(tmp_path):
    _, fake, _, response = provision(tmp_path, {"hostnames": [HOST]})
    assert response.error is None, response.error
    [cluster] = response.resources.cluster
    [created] = fake.clusters.values()
    assert created["networkConfig"] == {"gatewayApiConfig": {"channel": "CHANNEL_STANDARD"}} and created["addonsConfig"]["httpLoadBalancing"] == {"disabled": False}
    door = cluster.public
    assert door.address == fake.addresses["shop-cluster-door"]["address"] and door.hostnames == [HOST] and door.gateway == "cloudinfra-gateway/public"
    assert fake.sslCertificates["shop-cluster-door"]["managed"]["domains"] == [HOST] and door.certificate == "PROVISIONING"
    found = objects(fake, cluster.endpoint)
    gateway = found[f"{GATEWAYS}/public"]
    assert gateway["spec"]["gatewayClassName"] == GATEWAY_CLASS and gateway["spec"]["addresses"] == [{"type": "NamedAddress", "value": "shop-cluster-door"}]
    https, http = gateway["spec"]["listeners"]
    assert https["port"] == 443 and https["tls"]["options"] == {"networking.gke.io/pre-shared-certs": "shop-cluster-door"}
    assert https["allowedRoutes"]["namespaces"]["selector"]["matchLabels"] == {"kubernetes.io/metadata.name": "shop"}, "routes from the request's namespace only"
    assert http["allowedRoutes"] == {"namespaces": {"from": "Same"}}, "plain HTTP only redirects"
    redirect = found[f"{ROUTES}/to-https"]["spec"]["rules"][0]["filters"][0]
    assert redirect == {"type": "RequestRedirect", "requestRedirect": {"scheme": "https", "statusCode": 301}}
    assert "cloudinfra-tenant" not in fake.namespace_labels[(cluster.endpoint, DOOR_NAMESPACE)], "the platform's namespace is not a tenant's"


def test_a_namespace_with_edit_may_write_routes_and_not_gateways(tmp_path):
    _, fake, _, response = provision(tmp_path, {"hostnames": [HOST]})
    role = objects(fake, response.resources.cluster[0].endpoint)["/apis/rbac.authorization.k8s.io/v1/clusterroles/cloudinfra-routes"]
    assert role["metadata"]["labels"]["rbac.authorization.k8s.io/aggregate-to-edit"] == "true"
    assert [(rule["apiGroups"], rule["resources"]) for rule in role["rules"]] == [(["gateway.networking.k8s.io"], ["httproutes"]),
                                                                                (["networking.gke.io"], ["healthcheckpolicies", "gcpbackendpolicies"])]


def test_a_front_door_without_hostnames_serves_plain_http_and_has_no_certificate(tmp_path):
    _, fake, _, response = provision(tmp_path, {}, domains=())
    [cluster] = response.resources.cluster
    assert cluster.public.certificate is None and cluster.public.certificate_name is None and not fake.sslCertificates
    found = objects(fake, cluster.endpoint)
    [listener] = found[f"{GATEWAYS}/public"]["spec"]["listeners"]
    assert listener["protocol"] == "HTTP" and listener["allowedRoutes"]["namespaces"]["from"] == "Selector" and f"{ROUTES}/to-https" not in found


def test_destroy_closes_the_door_before_the_cluster_and_releases_the_address_after(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {"hostnames": [HOST]})
    assert fake.forwardingRules, "GKE made the load balancer"
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED"
    gateway = fake.order.index(("kubernetes DELETE", f"{GATEWAYS}/public"))
    assert gateway < fake.order.index(("cluster DELETE", "shop-cluster"))
    assert not fake.forwardingRules and not fake.addresses and not fake.sslCertificates and not fake.clusters


def test_the_certificate_state_is_read_with_the_cluster(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {"hostnames": [HOST]})
    fake.sslCertificates["shop-cluster-door"]["managed"]["status"] = "ACTIVE"
    cluster = orchestrator.get_resource(response.resources.cluster[0].id, who, ResourceKind.cluster).resource
    assert cluster.public.certificate == "ACTIVE"


def test_gateway_kinds_that_are_not_there_yet_are_waited_for(tmp_path):
    orchestrator, fake = orchestrator_with(tmp_path)
    fake.gateway_kinds_missing = 3
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": {"public": {"hostnames": [HOST]}}})
    response = orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert response.error is None, response.error


def test_a_failed_front_door_leaves_nothing_behind(tmp_path):
    orchestrator, fake = orchestrator_with(tmp_path)
    fake.fail_on.append(("POST", "/global/sslCertificates"))
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": {"public": {"hostnames": [HOST]}}})
    response = orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert response.status == "FAILED" and not fake.clusters and not fake.addresses and not fake.service_accounts


@pytest.mark.parametrize("domains, hostname, said", [(("example.com",), "shop.example.org", "allowed: example.com"),
                                                     ((), HOST, "none (plain HTTP on the address only)")])
def test_a_hostname_outside_the_operators_domains_is_refused(tmp_path, domains, hostname, said):
    orchestrator, fake = orchestrator_with(tmp_path, domains)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": {"public": {"hostnames": [hostname]}}})
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert said in str(caught.value.to_dict()) and not fake.clusters
    assert caught.value.code in (ErrorCode.INVALID_CLUSTER_CONFIGURATION, ErrorCode.INVALID_REQUEST)


def test_example_com_itself_is_in_its_domain(tmp_path):
    _, _, _, response = provision(tmp_path, {"hostnames": ["example.com"]})
    assert response.error is None


@pytest.mark.parametrize("hostnames", [["*.example.com"], ["Shop.example.com"], ["shop"], [HOST, HOST], ["a.example.com"] * 1 + [f"h{n}.example.com" for n in range(5)]])
def test_hostnames_are_plain_lowercase_dns_names_named_once(hostnames):
    with pytest.raises(ValidationError):
        InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": {"public": {"hostnames": hostnames}}})


def test_the_plan_prices_the_front_door_and_says_what_dns_must_do(tmp_path):
    orchestrator, _ = orchestrator_with(tmp_path)
    plain = orchestrator.plan(InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": {}}), principal(orchestrator.config))
    https = orchestrator.plan(InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": {"public": {"hostnames": [HOST]}}}),
                              principal(orchestrator.config))
    assert https.estimate.monthly_usd > plain.estimate.monthly_usd
    assert any("point their DNS at the address" in warning for warning in https.warnings)


def test_a_door_that_failed_after_its_gateway_still_waits_for_the_load_balancer(tmp_path):
    """The address was reserved and the Gateway made, then the redirect failed: the address was never recorded."""
    orchestrator, fake = orchestrator_with(tmp_path)
    request_body = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": {"public": {"hostnames": [HOST]}}})
    from gcp_fakes import FakeKubeApi
    put = FakeKubeApi.put

    def failing_put(self, collection, body):
        put(self, collection, body)
        if body["kind"] == "HTTPRoute":
            raise InfrastructureError(ErrorCode.PROVIDER_ERROR, "The cluster's API answered 422", {"http_status": 422})
    FakeKubeApi.put = failing_put
    try:
        response = orchestrator.provision(request_body, principal(orchestrator.config), wait_seconds=45)
    finally:
        FakeKubeApi.put = put
    assert response.status == "FAILED"
    assert fake.order.index(("kubernetes DELETE", f"{GATEWAYS}/public")) < fake.order.index(("cluster DELETE", "shop-cluster"))
    assert not fake.forwardingRules and not fake.addresses and not fake.clusters
