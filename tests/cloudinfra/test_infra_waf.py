"""A web application firewall (coverage research, 2026-10-30): a Cloud Armor policy that refuses listed sources, limits
each client address to a rate and refuses what Google's preconfigured OWASP rules recognise, on an http load balancer's
backend or on the backends of a cluster's front door (the delivery layer names it in each exposed workload's
GCPBackendPolicy). Made before what names it, deleted after."""
import pytest
from gcp_fakes import config_dict, make_orchestrator, principal
from pydantic import ValidationError

from cloudinfra.config import Config
from cloudinfra.models import InfrastructureError, InfrastructureRequest
from cloudinfra.providers.gcp.armor import DEFAULT_PRIORITY, OWASP, GcpArmor, rules

WAF = {"rate_limit_per_minute": 600, "deny_sources": ["198.51.100.0/24"]}
LB = {"compute": {"cpu": 2, "memory_gb": 8, "count": 2}, "load_balancer": {"type": "http", "waf": WAF}}


def provision(tmp_path, body):
    data = config_dict()
    data["database"] = str(tmp_path / "cloudinfra.db")
    data["providers"]["gcp"]["public_domains"] = ["example.com"]
    orchestrator, fake = make_orchestrator(tmp_path, config=Config.model_validate(data))
    who = principal(orchestrator.config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **body})
    return orchestrator, fake, who, orchestrator.provision(request, who, wait_seconds=45)


def test_the_load_balancers_backend_names_its_policy(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, LB)
    assert response.error is None, response.error
    [balancer] = response.resources.load_balancer
    policy = fake.securityPolicies["shop-lb-waf"]
    assert balancer.security_policy == "shop-lb-waf" and fake.backendServices["shop-lb-backend"]["securityPolicy"].endswith("/securityPolicies/shop-lb-waf")
    found = {rule["priority"]: rule for rule in policy["rules"]}
    assert found[1000]["action"] == "deny(403)" and found[1000]["match"]["config"]["srcIpRanges"] == ["198.51.100.0/24"]
    assert found[2000]["rateLimitOptions"]["rateLimitThreshold"] == {"count": 600, "intervalSec": 60} and found[2000]["rateLimitOptions"]["exceedAction"] == "deny(429)"
    assert [found[3000 + index]["match"]["expr"]["expression"] for index in range(len(OWASP))][0] == "evaluatePreconfiguredWaf('sqli-v33-stable', {'sensitivity': 1})"
    assert found[DEFAULT_PRIORITY]["action"] == "allow" and not any(rule["preview"] for rule in policy["rules"] if rule["priority"] != DEFAULT_PRIORITY)
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED"
    assert not fake.securityPolicies and not fake.backendServices, "the backend goes first, then the policy it named"


def test_a_cluster_front_door_has_a_policy_for_its_workloads(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {"cluster": {"public": {"hostnames": ["shop.example.com"], "waf": {"preview": True}}}})
    assert response.error is None, response.error
    [cluster] = response.resources.cluster
    assert cluster.public.security_policy == "shop-cluster-waf" and all(rule["preview"] for rule in fake.securityPolicies["shop-cluster-waf"]["rules"][:-1])
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED"
    assert not fake.securityPolicies


def test_a_policy_still_named_by_a_backend_gke_is_deleting_is_tried_again():
    class Api:
        project, slept, calls = "p", [], 0

        def delete(self, url):
            Api.calls += 1
            if Api.calls < 3:
                raise InfrastructureError("PROVIDER_ERROR", "The security_policy resource 'x' is already being used by 'backend'")
            return {}

        def wait_compute(self, operation):
            return operation

        def sleep(self, seconds):
            Api.slept.append(seconds)

    GcpArmor(Api()).delete("x")
    assert Api.calls == 3 and len(Api.slept) == 2


def test_the_firewall_is_priced_per_policy_and_rule(tmp_path):
    orchestrator, _ = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    plan = orchestrator.plan(InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **LB}), who)
    [line] = [item for item in plan.estimate.lines if item.kind == "load_balancer"]
    count = len(rules(InfrastructureRequest.model_validate({"region": "us-central1", **LB}).load_balancer.waf))
    assert count == 2 + len(OWASP) + 1 and "firewall policy" in line.basis and line.monthly_usd == round(0.025 * 730 + 5 + count, 2)


@pytest.mark.parametrize("body, said", [
    ({"compute": {"cpu": 2, "memory_gb": 8}, "load_balancer": {"type": "tcp", "waf": {}}}, "needs type http"),
    ({"compute": {"cpu": 2, "memory_gb": 8}, "load_balancer": {"waf": {"owasp": False}}}, "nothing to do"),
    ({"compute": {"cpu": 2, "memory_gb": 8}, "load_balancer": {"waf": {"rate_limit_per_minute": 5}}}, "greater than or equal to 10"),
    ({"compute": {"cpu": 2, "memory_gb": 8}, "load_balancer": {"waf": {"deny_sources": ["not-a-range"]}}}, "not-a-range"),
])
def test_a_waf_block_is_checked(body, said):
    with pytest.raises((ValidationError, InfrastructureError)) as caught:
        InfrastructureRequest.model_validate({"region": "us-central1", **body})
    assert said in str(caught.value)
