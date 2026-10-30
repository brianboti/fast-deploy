"""Monitoring (coverage research, 2026-10-30: 67 percent run Prometheus in production, and companies use eight
observability tools on average): uptime checks on the request's own public endpoints, an alert per check to addresses in
the operator's allowed domains, and nothing left behind, alerts going before the channels they use."""
import pytest
from gcp_fakes import config_dict, make_orchestrator, principal
from pydantic import ValidationError

from cloudinfra.config import Config
from cloudinfra.models import InfrastructureRequest
from cloudinfra.models.errors import InfrastructureError

WEB = {"compute": {"group": "web", "cpu": 2, "memory_gb": 4}}


def orchestrator_with(tmp_path, **gcp):
    data = config_dict()
    data["database"] = str(tmp_path / "cloudinfra.db")
    data["providers"]["gcp"].update({"public_domains": ["example.com"], "allow_public_services": True, **gcp})
    return make_orchestrator(tmp_path, config=Config.model_validate(data))


def provision(tmp_path, body):
    orchestrator, fake = orchestrator_with(tmp_path)
    who = principal(orchestrator.config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **body})
    return orchestrator, fake, who, orchestrator.provision(request, who, wait_seconds=45)


def kinds(fake, kind):
    return [item for name, item in fake.monitoring.items() if f"/{kind}/" in name]


def test_a_check_on_the_load_balancer_alerts_by_email_when_it_fails(tmp_path):
    body = {**WEB, "load_balancer": {"port": 8080}, "monitor": {"notify": ["oncall@example.com"], "uptime": [{"to": "load_balancer", "path": "/healthz", "period_seconds": 60}]}}
    _, fake, _, response = provision(tmp_path, body)
    assert response.error is None, response.error
    [channel], [check], [policy] = kinds(fake, "notificationChannels"), kinds(fake, "uptimeCheckConfigs"), kinds(fake, "alertPolicies")
    address = response.resources.load_balancer[0].address
    assert channel["type"] == "email" and channel["labels"] == {"email_address": "oncall@example.com"}
    assert check["monitoredResource"]["labels"]["host"] == address and check["httpCheck"] == {"path": "/healthz", "port": 8080, "useSsl": False, "validateSsl": False}
    assert check["period"] == "60s" and policy["notificationChannels"] == [channel["name"]]
    assert "uptime_check/check_passed" in policy["conditions"][0]["conditionThreshold"]["filter"]
    [monitor] = response.resources.monitor
    assert monitor.checks[0]["url"] == f"http://{address}:8080/healthz" and monitor.notify == ["oncall@example.com"]


def test_a_named_endpoint_is_checked_over_https_by_its_name(tmp_path):
    body = {**WEB, "load_balancer": {"hostnames": ["www.shop.example.com"]}, "service": {"name": "api", "public": True},
            "monitor": {"notify": ["oncall@example.com"], "uptime": [{"to": "load_balancer"}, {"to": "service:api"}]}}
    _, fake, _, response = provision(tmp_path, body)
    assert response.error is None, response.error
    hosts = sorted((item["monitoredResource"]["labels"]["host"], item["httpCheck"]["useSsl"]) for item in kinds(fake, "uptimeCheckConfigs"))
    assert hosts == [("shop-api-xyz.a.run.app", True), ("www.shop.example.com", True)] and len(kinds(fake, "alertPolicies")) == 2


def test_alerts_go_only_to_the_operators_domains(tmp_path):
    orchestrator, fake = orchestrator_with(tmp_path)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **WEB, "load_balancer": {},
                                                    "monitor": {"notify": ["someone@elsewhere.org"], "uptime": [{"to": "load_balancer"}]}})
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert "someone@elsewhere.org" in str(caught.value.to_dict()) and not fake.monitoring and not fake.instances


def test_destroy_takes_alerts_away_before_their_channels(tmp_path):
    body = {**WEB, "load_balancer": {}, "monitor": {"notify": ["oncall@example.com", "dev@example.com"], "uptime": [{"to": "load_balancer"}]}}
    orchestrator, fake, who, response = provision(tmp_path, body)
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED" and not fake.monitoring


def test_a_failed_check_leaves_nothing_behind(tmp_path):
    orchestrator, fake = orchestrator_with(tmp_path)
    fake.fail_on.append(("POST", "/alertPolicies"))
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **WEB, "load_balancer": {},
                                                    "monitor": {"notify": ["oncall@example.com"], "uptime": [{"to": "load_balancer"}]}})
    response = orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert response.status == "FAILED" and not fake.monitoring


@pytest.mark.parametrize("body, said", [
    ({"monitor": {"notify": ["oncall@example.com"], "uptime": [{"to": "load_balancer"}]}}, "no http load balancer"),
    ({"service": {"name": "api"}, "monitor": {"notify": ["oncall@example.com"], "uptime": [{"to": "service:api"}]}}, "cannot sign in"),
    ({"monitor": {"notify": ["Oncall@Example.com"], "uptime": [{"to": "load_balancer"}]}}, "lowercase email"),
    ({"monitor": {"notify": ["oncall@example.com"], "uptime": [{"to": "database"}]}}, "is not load_balancer"),
    ({"monitor": {"notify": ["oncall@example.com"], "uptime": [{"to": "load_balancer", "period_seconds": 30}]}}, "60, 300, 600 or 900"),
    ({"monitor": {"notify": [], "uptime": [{"to": "load_balancer"}]}}, "at least 1"),
])
def test_a_monitor_block_is_checked(body, said):
    with pytest.raises(ValidationError) as caught:
        InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **body})
    assert said in str(caught.value)
