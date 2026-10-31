"""The managed container service kind (docs/DELIVERY-SPEC.md, phase 4, step 1): a Cloud Run service made empty, with a
stand-in image, its limits, scaling and ingress, an account made for it that reads the request's secrets and nothing
else, grants for calling and deploying, and nothing left behind."""
import pytest
from gcp_fakes import PROJECT, config_dict, make_orchestrator, principal
from pydantic import ValidationError

from cloudinfra.config import Config
from cloudinfra.models import InfrastructureRequest
from cloudinfra.models.errors import InfrastructureError
from cloudinfra.models.resources import ResourceKind
from cloudinfra.providers.gcp.run import STAND_IN, runtime_account

DELIVERY = "serviceAccount:appdelivery-dev@example.com"
DATA = "group:data@example.com"


def orchestrator_with(tmp_path, **gcp):
    data = config_dict()
    data["database"] = str(tmp_path / "cloudinfra.db")
    data["providers"]["gcp"].update(gcp)
    return make_orchestrator(tmp_path, config=Config.model_validate(data))


def provision(tmp_path, service, access=None, secret=None, **gcp):
    orchestrator, fake = orchestrator_with(tmp_path, **gcp)
    who = principal(orchestrator.config)
    body = {"region": "us-central1", "name": "shop", "service": service, "access": access or []}
    if secret:
        body["secret"] = secret
    return orchestrator, fake, who, orchestrator.provision(InfrastructureRequest.model_validate(body), who, wait_seconds=45)


def test_a_service_is_made_empty_with_its_limits_and_its_own_account(tmp_path):
    _, fake, _, response = provision(tmp_path, {"cpu": 2, "memory_mb": 1024, "min_instances": 1, "max_instances": 5, "concurrency": 40, "timeout_seconds": 120,
                                                "ingress": "internal"})
    assert response.error is None, response.error
    [service] = response.resources.service
    made = fake.run_services["shop-service"]
    email = f"{runtime_account(response.request_id, 'service')}@{PROJECT}.iam.gserviceaccount.com"
    assert service.runtime_service_account == email and made["template"]["serviceAccount"] == email and email in fake.service_accounts
    container = made["template"]["containers"][0]
    assert container["image"] == STAND_IN and container["resources"]["limits"] == {"cpu": "2", "memory": "1024Mi"}
    assert made["template"]["scaling"] == {"minInstanceCount": 1, "maxInstanceCount": 5} and made["template"]["maxInstanceRequestConcurrency"] == 40
    assert made["template"]["timeout"] == "120s" and made["ingress"] == "INGRESS_TRAFFIC_INTERNAL_ONLY" and made["invokerIamDisabled"] is False
    assert service.status == "READY" and service.url == "https://shop-service-xyz.a.run.app" and service.revision == "shop-service-00001-abc"


def test_the_runtime_account_reads_the_secrets_it_names_and_nothing_else(tmp_path):
    _, fake, _, response = provision(tmp_path, {"secrets": ["api-key"]}, secret=[{"name": "api-key", "generate_value": True}, {"name": "token"}])
    [service] = response.resources.service
    member = f"serviceAccount:{service.runtime_service_account}"
    readable = [secret.name.removeprefix("shop-") for secret in response.resources.secret
                if member in fake.members(("secret", secret.resource_name.rsplit("/", 1)[-1]), "roles/secretmanager.secretAccessor")]
    assert readable == ["api-key"], "the one it names, not token (S2, L9)"
    assert service.secrets == [secret.resource_name for secret in response.resources.secret if secret.name.endswith("api-key")]
    assert ("project",) not in fake.policies, "no project role"


def test_a_public_service_needs_the_operators_leave(tmp_path):
    orchestrator, _ = orchestrator_with(tmp_path)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "service": {"public": True}})
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert "allow_public_services" in str(caught.value.to_dict())
    (tmp_path / "allowed").mkdir()
    _, fake, _, response = provision(tmp_path / "allowed", {"public": True}, allow_public_services=True)
    assert fake.run_services["shop-service"]["invokerIamDisabled"] is True and response.resources.service[0].public


def test_use_calls_the_service_and_admin_also_deploys_it(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {}, access=[{"member": DELIVERY, "level": "admin"}, {"member": DATA, "level": "use"}])
    [service] = response.resources.service
    assert set(fake.members(("run", "shop-service"), "roles/run.invoker")) == {DELIVERY, DATA}
    assert fake.members(("run", "shop-service"), "roles/run.developer") == [DELIVERY]
    assert fake.members(("account", service.runtime_service_account), "roles/iam.serviceAccountUser") == [DELIVERY], "deploying runs as the runtime account"
    orchestrator.manage_access(response.request_id, "revoke", DELIVERY, who)
    assert fake.members(("run", "shop-service"), "roles/run.developer") == [] and fake.members(("account", service.runtime_service_account), "roles/iam.serviceAccountUser") == []


def test_destroy_deletes_the_service_and_then_its_account(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {}, secret=[{"name": "api-key", "generate_value": True}])
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED"
    assert not fake.run_services and not fake.service_accounts and not fake.secrets


def test_a_service_google_refused_leaves_no_account_behind(tmp_path):
    orchestrator, fake = orchestrator_with(tmp_path)
    fake.fail_on.append(("POST", "/services"))
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "service": {}})
    response = orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert response.status == "FAILED" and not fake.service_accounts and not fake.run_services


def test_several_services_each_with_their_own_account(tmp_path):
    _, fake, _, response = provision(tmp_path, [{"name": "api"}, {"name": "web", "ingress": "internal_and_load_balancer"}])
    assert set(fake.run_services) == {"shop-api", "shop-web"} and len({item.runtime_service_account for item in response.resources.service}) == 2
    assert fake.run_services["shop-web"]["ingress"] == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"


def test_a_service_sends_private_traffic_through_the_requests_network(tmp_path):
    _, fake, _, response = provision(tmp_path, {"egress": "all"})
    assert "vpcAccess" not in fake.run_services["shop-service"]["template"], "no network, no egress through one"
    (tmp_path / "net").mkdir()
    orchestrator, fake = orchestrator_with(tmp_path / "net")
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "network": {}, "service": {"egress": "all"}})
    response = orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert response.error is None, response.error
    access = fake.run_services["shop-service"]["template"]["vpcAccess"]
    assert access["egress"] == "ALL_TRAFFIC" and "/" not in access["networkInterfaces"][0]["network"], "by name, as Cloud Run takes it"


@pytest.mark.parametrize("service, said", [({"cpu": 3}, "1, 2, 4 or 8"), ({"min_instances": 5, "max_instances": 2}, "above max_instances"),
                                           ([{"name": "a"}, {}], "every service of a list needs a name"), ([{"name": "a"}, {"name": "a"}], "names must be unique")])
def test_a_service_block_is_checked(service, said):
    with pytest.raises(ValidationError) as caught:
        InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "service": service})
    assert said in str(caught.value)


@pytest.mark.parametrize("cpu, memory_mb", [(1, 8192), (4, 1024), (8, 2048)])
def test_cpu_and_memory_the_provider_does_not_pair_are_refused(tmp_path, cpu, memory_mb):
    orchestrator, fake = orchestrator_with(tmp_path)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "service": {"cpu": cpu, "memory_mb": memory_mb}})
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert "INVALID_SERVICE_CONFIGURATION" in str(caught.value.to_dict()) and not fake.run_services


def test_the_connect_block_says_how_to_call_it(tmp_path):
    _, _, _, response = provision(tmp_path, {}, access=[{"member": DATA, "level": "use"}])
    connect = response.resources.service[0].connect
    assert "print-identity-token" in connect.commands["call"] and "https://shop-service-xyz.a.run.app" in connect.commands["call"]


def test_only_warm_instances_cost_while_idle(tmp_path):
    orchestrator, _ = orchestrator_with(tmp_path)
    who = principal(orchestrator.config)
    cold = orchestrator.plan(InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "service": {}}), who)
    warm = orchestrator.plan(InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "service": {"min_instances": 2}}), who)
    assert cold.estimate.monthly_usd == 0 and warm.estimate.monthly_usd > 0


def test_a_service_that_serves_on_after_a_failed_revision_is_ready(tmp_path):
    """Found on a real project on 2026-10-30: a release's revision that failed leaves Cloud Run's condition failed while the
    revision before it serves all traffic; the service is usable, and must stay so for a rollback."""
    orchestrator, fake, who, response = provision(tmp_path, {})
    fake.run_services["shop-service"]["terminalCondition"] = {"type": "Ready", "state": "CONDITION_FAILED", "message": "Revision 'x' is not ready"}
    assert orchestrator.get_resource(response.resources.service[0].id, who, ResourceKind.service).resource.status == "READY"
    fake.run_services["shop-service"]["trafficStatuses"] = []
    assert orchestrator.get_resource(response.resources.service[0].id, who, ResourceKind.service).resource.status == "FAILED", "nothing serves: failed"
