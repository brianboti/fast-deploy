"""A request's namespace cannot open a public address of its own (docs/DELIVERY-SPEC.md, phase 3, step 4): every cluster
gets a ValidatingAdmissionPolicy, bound to the namespaces made for requests by a label `edit` cannot change, that refuses
a Service of type LoadBalancer, an Ingress and a Gateway there. The policy's expressions run in a real cluster only; the
real run checks them (docs/DELIVERY-SPIKE.md)."""
import json

import httpx
from gcp_fakes import make_orchestrator, principal

from cloudinfra.models import InfrastructureRequest
from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.providers.gcp.kubernetes import ADMISSION, NO_PUBLIC_ENTRY, TENANT, KubeApi, no_public_entry

POLICY = f"{ADMISSION}/validatingadmissionpolicies/{NO_PUBLIC_ENTRY}"
BINDING = f"{ADMISSION}/validatingadmissionpolicybindings/{NO_PUBLIC_ENTRY}"


def provision(tmp_path, clusters):
    orchestrator, fake = make_orchestrator(tmp_path)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": clusters})
    return fake, orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)


def test_every_cluster_gets_the_policy_and_the_requests_namespace_its_label(tmp_path):
    fake, response = provision(tmp_path, [{"name": "blue"}, {"name": "green"}])
    for cluster in response.resources.cluster:
        assert {path for endpoint, path in fake.cluster_objects if endpoint == cluster.endpoint} == {POLICY, BINDING}
        assert fake.namespace_labels[(cluster.endpoint, "shop")]["cloudinfra-tenant"] == "true"
        binding = fake.cluster_objects[(cluster.endpoint, BINDING)]
        assert binding["spec"]["matchResources"]["namespaceSelector"] == {"matchLabels": TENANT} and binding["spec"]["validationActions"] == ["Deny"]


def test_the_policy_refuses_the_three_ways_to_a_public_load_balancer():
    [(_, policy), (_, binding)] = no_public_entry({})
    rules = {(rule["apiGroups"][0], rule["resources"][0]) for rule in policy["spec"]["matchConstraints"]["resourceRules"]}
    assert rules == {("", "services"), ("networking.k8s.io", "ingresses"), ("gateway.networking.k8s.io", "gateways")}
    assert all(rule["operations"] == ["CREATE", "UPDATE"] for rule in policy["spec"]["matchConstraints"]["resourceRules"])
    expressions = [item["expression"] for item in policy["spec"]["validations"]]
    assert any("'LoadBalancer'" in item and "has(object.spec.type)" in item for item in expressions), "a Service without a type is a ClusterIP"
    assert "object.kind != 'Ingress'" in expressions and "object.kind != 'Gateway'" in expressions
    assert policy["spec"]["failurePolicy"] == "Fail" and binding["spec"]["policyName"] == policy["metadata"]["name"]


def test_a_cluster_that_will_not_take_the_policy_is_not_left_behind(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    fake.kube_fail = InfrastructureError(ErrorCode.PROVIDER_PERMISSION_DENIED, "The cluster's API answered 403", {"http_status": 403})
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": {}})
    response = orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert response.status == "FAILED" and not fake.clusters and not fake.namespaces


def test_put_creates_an_object_or_replaces_it_with_the_version_read():
    seen = []

    def answer(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"metadata": {"name": "p", "resourceVersion": "41"}}) if "exists" in str(request.url) else httpx.Response(404)
        return httpx.Response(200, json={})
    api = KubeApi("35.201.0.7", "unused", lambda: "google-token", client=httpx.Client(transport=httpx.MockTransport(answer)))
    api.put("/apis/x/v1/things", {"metadata": {"name": "new"}})
    api.put("/apis/x/v1/things", {"metadata": {"name": "exists"}})
    assert [(request.method, request.url.path) for request in seen] == [("GET", "/apis/x/v1/things/new"), ("POST", "/apis/x/v1/things"),
                                                                       ("GET", "/apis/x/v1/things/exists"), ("PUT", "/apis/x/v1/things/exists")]
    assert json.loads(seen[3].content)["metadata"]["resourceVersion"] == "41"
