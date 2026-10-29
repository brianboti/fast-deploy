"""Each cluster's nodes run as an account made for that cluster (docs/DELIVERY-SPEC.md, phase 3, step 3): the minimum
node role on the project, reader on the registries the request names and the operator allows, and nothing left once
the cluster is gone, or once Google refused it."""
import pytest
from gcp_fakes import PROJECT, config_dict, make_orchestrator, principal
from pydantic import ValidationError

from cloudinfra.config import Config
from cloudinfra.models import InfrastructureRequest
from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.providers.gcp.nodeaccount import NODE_ROLE, READER, account_id

APPS = "us-central1-docker.pkg.dev/test-project/apps"
SHARED = "us-central1-docker.pkg.dev/test-project/shared"


def orchestrator_with(tmp_path, **gcp):
    data = config_dict()
    data["database"] = str(tmp_path / "cloudinfra.db")
    data["providers"]["gcp"].update({"registries": [APPS, SHARED], **gcp})
    return make_orchestrator(tmp_path, config=Config.model_validate(data))


def provision(tmp_path, cluster=None, **gcp):
    orchestrator, fake = orchestrator_with(tmp_path, **gcp)
    who = principal(orchestrator.config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": cluster or {"node_count": 1}})
    return orchestrator, fake, who, orchestrator.provision(request, who, wait_seconds=45)


def node_pool(fake):
    [cluster] = fake.clusters.values()
    return cluster["nodePools"][0]["config"]


def test_the_nodes_run_as_an_account_made_for_their_cluster_with_the_minimum_node_role(tmp_path):
    _, fake, _, response = provision(tmp_path)
    [cluster] = response.resources.cluster
    email = f"{account_id(response.request_id, 'cluster')}@{PROJECT}.iam.gserviceaccount.com"
    assert cluster.node_service_account == email and node_pool(fake)["serviceAccount"] == email and set(fake.service_accounts) == {email}
    assert fake.members(("project",), NODE_ROLE) == [f"serviceAccount:{email}"]
    assert [binding["role"] for binding in fake.policies[("project",)]["bindings"]] == [NODE_ROLE], "no other project role"


def test_the_nodes_may_pull_from_the_registries_the_request_names_and_no_other(tmp_path):
    _, fake, _, response = provision(tmp_path, {"registries": [APPS]})
    [cluster] = response.resources.cluster
    assert cluster.registries == [APPS]
    assert fake.members(("repository", APPS), READER) == [f"serviceAccount:{cluster.node_service_account}"]
    assert ("repository", SHARED) not in fake.policies


def test_each_cluster_of_a_request_has_its_own_account(tmp_path):
    _, fake, _, response = provision(tmp_path, [{"name": "blue", "registries": [APPS]}, {"name": "green"}])
    accounts = {cluster.name: cluster.node_service_account for cluster in response.resources.cluster}
    assert len(set(accounts.values())) == 2 and all(account.startswith("node-") for account in accounts.values())
    assert fake.members(("repository", APPS), READER) == [f"serviceAccount:{accounts['shop-blue']}"]


def test_destroying_the_request_takes_the_roles_away_and_deletes_the_account(tmp_path):
    orchestrator, fake, who, response = provision(tmp_path, {"registries": [APPS, SHARED]})
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == "DESTROYED"
    assert not fake.clusters and not fake.service_accounts
    assert fake.members(("project",), NODE_ROLE) == [] and fake.members(("repository", APPS), READER) == [] and fake.members(("repository", SHARED), READER) == []


def test_a_grant_iam_does_not_know_the_new_account_for_yet_is_tried_again(tmp_path):
    orchestrator, fake = orchestrator_with(tmp_path)
    fake.unknown_for = 3
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": {"registries": [APPS]}})
    response = orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert response.error is None, response.error
    assert fake.members(("repository", APPS), READER) == [f"serviceAccount:{response.resources.cluster[0].node_service_account}"]


def test_a_cluster_google_refused_leaves_no_account_behind(tmp_path):
    orchestrator, fake = orchestrator_with(tmp_path)
    fake.fail_on.append(("POST", "/clusters"))
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": {"registries": [APPS]}})
    response = orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert response.status == "FAILED" and not fake.service_accounts
    assert fake.members(("project",), NODE_ROLE) == [] and fake.members(("repository", APPS), READER) == []


def test_a_registry_grant_that_fails_leaves_no_account_behind(tmp_path):
    orchestrator, fake = orchestrator_with(tmp_path)
    fake.repositories.discard(APPS)  # allowed by the operator, but gone at Google
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": {"registries": [APPS]}})
    response = orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert response.status == "FAILED" and not fake.service_accounts and not fake.clusters
    assert fake.members(("project",), NODE_ROLE) == []


def test_a_registry_the_operator_does_not_allow_is_refused_before_anything_is_made(tmp_path):
    orchestrator, fake = orchestrator_with(tmp_path, registries=[SHARED])
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": {"registries": [APPS]}})
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert caught.value.code in (ErrorCode.INVALID_CLUSTER_CONFIGURATION, ErrorCode.INVALID_REQUEST) and APPS in str(caught.value.to_dict())
    assert not fake.service_accounts and not fake.clusters


def test_an_operator_can_keep_the_default_node_account(tmp_path):
    _, fake, _, response = provision(tmp_path, cluster_node_accounts=False)
    assert response.resources.cluster[0].node_service_account is None and "serviceAccount" not in node_pool(fake) and not fake.service_accounts
    (tmp_path / "again").mkdir()
    orchestrator, fake = orchestrator_with(tmp_path / "again", cluster_node_accounts=False)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": {"registries": [APPS]}})
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.provision(request, principal(orchestrator.config), wait_seconds=45)
    assert "cluster_node_accounts" in str(caught.value.to_dict())


@pytest.mark.parametrize("registries", [["docker.io/library/nginx"], ["us-central1-docker.pkg.dev/test-project"], [APPS, APPS]])
def test_a_registry_is_an_artifact_registry_repository_named_once(registries):
    with pytest.raises(ValidationError):
        InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "cluster": {"registries": registries}})


def test_an_account_id_fits_what_iam_takes():
    longest = account_id("f" * 32, "a" * 12)
    assert len(longest) <= 30 and longest.startswith("node-") and longest[-1].isalnum()
