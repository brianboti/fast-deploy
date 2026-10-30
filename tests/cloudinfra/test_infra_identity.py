"""Workload identities (docs/RESEARCH-2026-10-28-infrastructure-layer.md section 7, first of the next kinds): an account
made for the request that never gets a key, holds the request's grants at its level as any member does, and nothing
else; machines of a group run as it, and pods using the Kubernetes service account of its name act as it."""
import pytest
from gcp_fakes import PROJECT, make_orchestrator, principal
from pydantic import ValidationError

from cloudinfra.models import InfrastructureRequest
from cloudinfra.providers.gcp.identity import WORKLOAD_USER, account_id


def provision(tmp_path, body):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **body})
    return orchestrator, fake, who, orchestrator.provision(request, who, wait_seconds=45)


def test_an_identity_is_an_account_granted_on_the_requests_resources_only(tmp_path):
    _, fake, _, response = provision(tmp_path, {"identity": {"name": "app"}, "secret": [{"name": "api-key", "generate_value": True}],
                                                "queue": [{"name": "events"}], "storage": {"type": "object"}})
    assert response.error is None, response.error
    [identity] = response.resources.identity
    email = f"{account_id(response.request_id, 'app')}@{PROJECT}.iam.gserviceaccount.com"
    assert identity.email == email and email in fake.service_accounts and identity.level == "use"
    member = f"serviceAccount:{email}"
    entry = next(item for item in response.access if item.member == member)
    assert entry.status == "GRANTED" and len(entry.resources) == 3, "the secret, the topic and the bucket"
    secret = response.resources.secret[0]
    assert member in fake.members(("secret", secret.resource_name.rsplit("/", 1)[-1]), "roles/secretmanager.secretAccessor")
    assert ("project",) not in fake.policies, "no project role"


def test_machines_of_a_group_run_as_the_identity_it_names(tmp_path):
    _, fake, _, response = provision(tmp_path, {"identity": [{"name": "web"}, {"name": "batch", "level": "admin"}],
                                                "compute": [{"group": "web", "cpu": 2, "memory_gb": 4, "identity": "web"}, {"group": "plain", "cpu": 2, "memory_gb": 4}]})
    assert response.error is None, response.error
    accounts = {name: body.get("serviceAccounts") for (_, name), body in fake.instances.items()}
    assert accounts["shop-web-1"] == [{"email": f"{account_id(response.request_id, 'web')}@{PROJECT}.iam.gserviceaccount.com",
                                       "scopes": ["https://www.googleapis.com/auth/cloud-platform"]}]
    assert accounts["shop-plain-1"] == [], "a group that names none runs as no account"


def test_pods_in_the_requests_namespace_act_as_it_with_no_key(tmp_path):
    _, fake, _, response = provision(tmp_path, {"identity": {"name": "app"}, "cluster": {"node_count": 1}})
    [identity] = response.resources.identity
    namespace = f"shop-{response.request_id[:6]}"
    assert identity.kubernetes_service_account == f"{namespace}/app" and response.resources.cluster[0].namespace == namespace
    assert fake.members(("account", identity.email), WORKLOAD_USER) == [f"serviceAccount:{PROJECT}.svc.id.goog[{namespace}/app]"]
    assert "iam.gke.io/gcp-service-account=" in response.resources.identity[0].connect.commands["kubernetes"]


def test_two_requests_of_one_name_never_share_a_bound_principal(tmp_path):
    """A Workload Identity principal is project-wide: the same namespace/name pair in any cluster of the project is the same
    identity. So the namespace carries the request's id, and a second request of the same name, by anyone, binds another."""
    orchestrator, fake, who, first = provision(tmp_path, {"identity": {"name": "app"}, "cluster": {"node_count": 1}})
    other = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "zone": "us-central1-b", "identity": {"name": "app"}, "cluster": {"name": "two", "node_count": 1}})
    second = orchestrator.provision(other, who, wait_seconds=45)
    assert second.error is None, second.error
    principals = {fake.members(("account", response.resources.identity[0].email), WORKLOAD_USER)[0] for response in (first, second)}
    assert len(principals) == 2 and first.resources.cluster[0].namespace != second.resources.cluster[0].namespace


def test_without_a_cluster_no_kubernetes_account_may_act_as_it(tmp_path):
    _, fake, _, response = provision(tmp_path, {"identity": {}})
    [identity] = response.resources.identity
    assert identity.name == "shop-app" and identity.kubernetes_service_account is None and ("account", identity.email) not in fake.policies


def test_destroy_deletes_the_identity_last(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {"identity": {}, "secret": [{"name": "api-key", "generate_value": True}]})
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED"
    assert not fake.service_accounts and not fake.secrets


@pytest.mark.parametrize("body, said", [({"identity": [{"name": "a"}, {}]}, "every identity of a list needs a name"),
                                        ({"compute": {"cpu": 2, "memory_gb": 4, "identity": "ghost"}}, "not an identity of this request"),
                                        ({"identity": {"level": "owner"}}, "level")])
def test_identity_blocks_are_checked(body, said):
    with pytest.raises(ValidationError) as caught:
        InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", **body})
    assert said in str(caught.value)
