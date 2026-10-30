"""HTTPS on the load balancer (coverage research, 2026-10-30: about 98 percent of websites use a CA-issued certificate):
hostnames in the operator's domains get a Google-managed certificate and a 443 front end on the same address, whose state
is read with the load balancer; with a DNS zone in the request, its records can point the names at it."""
import pytest
from gcp_fakes import config_dict, make_orchestrator, principal
from pydantic import ValidationError

from cloudinfra.config import Config
from cloudinfra.models import InfrastructureRequest
from cloudinfra.models.errors import InfrastructureError
from cloudinfra.models.resources import ResourceKind

HOST = "www.shop.example.com"
WEB = {"compute": {"group": "web", "cpu": 2, "memory_gb": 4}}


def orchestrator_with(tmp_path, domains=("example.com",)):
    data = config_dict()
    data["database"] = str(tmp_path / "cloudinfra.db")
    data["providers"]["gcp"]["public_domains"] = list(domains)
    return make_orchestrator(tmp_path, config=Config.model_validate(data))


def provision(tmp_path, body, domains=("example.com",)):
    orchestrator, fake = orchestrator_with(tmp_path, domains)
    who = principal(orchestrator.config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **WEB, **body})
    return orchestrator, fake, who, orchestrator.provision(request, who, wait_seconds=45)


def test_hostnames_add_https_on_the_same_address_with_a_managed_certificate(tmp_path):
    _, fake, _, response = provision(tmp_path, {"load_balancer": {"port": 8080, "hostnames": [HOST]}})
    assert response.error is None, response.error
    [balancer] = response.resources.load_balancer
    assert fake.sslCertificates["shop-lb-cert"]["managed"]["domains"] == [HOST] and balancer.certificate == "PROVISIONING"
    https = fake.targetHttpsProxies["shop-lb-https"]
    assert https["urlMap"].endswith("/urlMaps/shop-lb-map") and https["sslCertificates"][0].endswith("/sslCertificates/shop-lb-cert")
    rules = {name: rule for name, rule in fake.forwardingRules.items()}
    assert rules["shop-lb-https-rule"]["portRange"] == "443" and rules["shop-lb-https-rule"]["IPAddress"] == rules["shop-lb-rule"]["IPAddress"]
    assert balancer.hostnames == [HOST] and balancer.connect.commands["https"] == f"curl https://{HOST}/"


def test_the_certificate_state_is_read_with_the_load_balancer(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {"load_balancer": {"hostnames": [HOST]}})
    fake.sslCertificates["shop-lb-cert"]["managed"]["status"] = "ACTIVE"
    assert orchestrator.get_resource(response.resources.load_balancer[0].id, who, ResourceKind.load_balancer).resource.certificate == "ACTIVE"


def test_a_zone_in_the_request_points_the_name_at_it(tmp_path):
    _, fake, _, response = provision(tmp_path, {"load_balancer": {"hostnames": [HOST]}, "dns": {"zone": "shop.example.com", "records": [{"name": "www", "to": "load_balancer"}]}})
    [zone] = fake.zones.values()
    assert [item["rrdatas"] for item in zone["rrsets"] if item["type"] == "A"] == [[response.resources.load_balancer[0].address]]


def test_destroy_takes_https_away_with_the_rest(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {"load_balancer": {"hostnames": [HOST]}})
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED"
    assert not fake.sslCertificates and not fake.targetHttpsProxies and not fake.forwardingRules


def test_without_hostnames_it_stays_plain_http(tmp_path):
    _, fake, _, response = provision(tmp_path, {"load_balancer": {}})
    assert not fake.sslCertificates and not fake.targetHttpsProxies and response.resources.load_balancer[0].certificate is None


def test_a_name_outside_the_operators_domains_is_refused(tmp_path):
    orchestrator, fake = orchestrator_with(tmp_path)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **WEB, "load_balancer": {"hostnames": ["www.other.org"]}})
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert "www.other.org" in str(caught.value.to_dict()) and not fake.instances


@pytest.mark.parametrize("balancer, said", [({"type": "tcp", "hostnames": [HOST]}, "HTTPS needs type http"), ({"hostnames": ["*.shop.example.com"]}, "no wildcard"),
                                            ({"hostnames": [HOST, HOST]}, "each name once")])
def test_hostnames_are_checked(balancer, said):
    with pytest.raises(ValidationError) as caught:
        InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **WEB, "load_balancer": balancer})
    assert said in str(caught.value)
