"""Public DNS zones (coverage research, 2026-10-30): a zone for a domain the operator allows, record sets written in one
change, A records that point at the request's own load balancer or a cluster's front door by name, grants on the zone,
and a delete that empties the zone first, as Cloud DNS requires."""
import pytest
from gcp_fakes import config_dict, make_orchestrator, principal
from pydantic import ValidationError

from cloudinfra.config import Config
from cloudinfra.models import InfrastructureRequest
from cloudinfra.models.errors import InfrastructureError

ZONE = "shop.example.com"
DATA = "group:data@example.com"


def orchestrator_with(tmp_path, domains=("example.com",)):
    data = config_dict()
    data["database"] = str(tmp_path / "cloudinfra.db")
    data["providers"]["gcp"]["public_domains"] = list(domains)
    return make_orchestrator(tmp_path, config=Config.model_validate(data))


def provision(tmp_path, body, domains=("example.com",)):
    orchestrator, fake = orchestrator_with(tmp_path, domains)
    who = principal(orchestrator.config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **body})
    return orchestrator, fake, who, orchestrator.provision(request, who, wait_seconds=45)


def rrsets(fake):
    [zone] = fake.zones.values()
    return {(item["name"], item["type"]): item["rrdatas"] for item in zone["rrsets"] if item["type"] not in ("NS", "SOA")}


def test_a_zone_and_its_records_are_written_as_cloud_dns_stores_them(tmp_path):
    records = [{"name": "@", "type": "TXT", "values": ["v=spf1 -all"]}, {"name": "www", "type": "CNAME", "values": ["shop.example.com"]},
               {"name": "@", "type": "MX", "values": ["10 mail.example.com"]}, {"name": "api", "values": ["203.0.113.7"], "ttl": 60}]
    _, fake, _, response = provision(tmp_path, {"dns": {"zone": ZONE, "records": records}})
    assert response.error is None, response.error
    [zone] = response.resources.dns
    assert zone.dns_name == ZONE and len(zone.name_servers) == 4 and fake.zones["shop-zone"]["visibility"] == "public"
    assert rrsets(fake) == {(f"{ZONE}.", "TXT"): ['"v=spf1 -all"'], (f"www.{ZONE}.", "CNAME"): [f"{ZONE}."], (f"{ZONE}.", "MX"): ["10 mail.example.com."],
                            (f"api.{ZONE}.", "A"): ["203.0.113.7"]}
    assert any(ZONE in note and "registrar" in note for note in zone.connect.notes)


def test_a_record_points_at_the_requests_own_load_balancer_and_front_door(tmp_path):
    body = {"compute": {"group": "web", "cpu": 2, "memory_gb": 4}, "load_balancer": {}, "cluster": {"public": {}},
            "dns": {"zone": ZONE, "records": [{"name": "www", "to": "load_balancer"}, {"name": "apps", "to": "cluster"}]}}
    _, fake, _, response = provision(tmp_path, body)
    assert response.error is None, response.error
    found = rrsets(fake)
    assert found[(f"www.{ZONE}.", "A")] == [response.resources.load_balancer[0].address]
    assert found[(f"apps.{ZONE}.", "A")] == [response.resources.cluster[0].public.address]


def test_use_reads_the_zone_and_admin_changes_it(tmp_path):
    _, fake, _, response = provision(tmp_path, {"dns": {"zone": ZONE}, "access": [{"member": DATA, "level": "admin"}]})
    assert fake.members(("zone", "shop-zone"), "roles/dns.admin") == [DATA]


def test_destroy_empties_the_zone_then_deletes_it(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {"dns": {"zone": ZONE, "records": [{"name": "www", "values": ["203.0.113.7"]}]}})
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED" and not fake.zones


@pytest.mark.parametrize("domains, zone", [(("example.com",), "shop.example.org"), ((), ZONE)])
def test_a_domain_the_operator_does_not_allow_is_refused(tmp_path, domains, zone):
    orchestrator, fake = orchestrator_with(tmp_path, domains)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "dns": {"zone": zone}})
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert "INVALID_DNS_CONFIGURATION" in str(caught.value.to_dict()) and not fake.zones


@pytest.mark.parametrize("dns, said", [
    ({"zone": "Shop.Example.com"}, "not a lowercase domain"),
    ({"zone": ZONE, "records": [{"name": "www", "values": ["not-an-address"]}]}, "not an IPv4 address"),
    ({"zone": ZONE, "records": [{"name": "www"}]}, "give values or to"),
    ({"zone": ZONE, "records": [{"name": "www", "type": "CNAME", "to": "load_balancer"}]}, "to makes an A record"),
    ({"zone": ZONE, "records": [{"name": "www", "values": ["203.0.113.7"]}, {"name": "www", "values": ["203.0.113.8"]}]}, "one record set per name and type"),
    ({"zone": ZONE, "records": [{"name": "www", "to": "load_balancer"}]}, "the request has no load balancer"),
    ({"zone": ZONE, "records": [{"name": "Bad_Name", "values": ["203.0.113.7"]}]}, "not a name within the zone"),
])
def test_a_dns_block_is_checked(dns, said):
    with pytest.raises(ValidationError) as caught:
        InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "dns": dns})
    assert said in str(caught.value)
