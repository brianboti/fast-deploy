"""Registries: a container image repository a request owns. Members at use pull, at admin push; a team's CI job is named in
access at admin, as a service account or as its own federated identity (principal://, principalSet://) from a workload
identity pool the operator allows, which needs no key and is granted on registries only."""
import pytest
from gcp_fakes import make_config, make_orchestrator, principal

from cloudinfra.models import InfrastructureRequest, ResourceKind
from cloudinfra.models.errors import ErrorCode, InfrastructureError

POOL = "iam.googleapis.com/projects/123456789/locations/global/workloadIdentityPools/github"
CI = f"principalSet://{POOL}/attribute.repository/Acme/Shop"  # GitHub repository names keep their case
CI_ACCOUNT = "serviceAccount:shop-ci@example.com"
DEVS = "group:shop-devs@example.com"
ADDRESS = "us-central1-docker.pkg.dev/test-project/shop-images"
REPOSITORY = ("repository", ADDRESS)


def setup(tmp_path, access=(), **body):
    config = make_config(tmp_path, access={"allowed_domains": ["example.com"], "workload_identity_pools": [POOL]})
    orchestrator, fake = make_orchestrator(tmp_path, config=config)
    who = principal(config)
    request = {"region": "us-central1", "name": "shop", "registry": {"name": "images"}, "access": list(access), **body}
    response = orchestrator.provision(InfrastructureRequest.model_validate(request), who, wait_seconds=45)
    return orchestrator, fake, who, response


def test_a_registry_is_a_docker_repository_in_the_region(tmp_path):
    _, fake, _, response = setup(tmp_path)
    assert response.status == "READY", response.error
    [registry] = response.resources.registry
    assert registry.id.startswith("reg-") and registry.address == ADDRESS
    assert registry.repository == "projects/test-project/locations/us-central1/repositories/shop-images"
    body = fake.repository_bodies[ADDRESS]
    assert body["format"] == "DOCKER" and body["dockerConfig"] == {"immutableTags": False} and body["labels"]["managed-by"] == "cloudinfra"
    assert response.endpoints.registry_addresses == [ADDRESS]


def test_ci_pushes_and_developers_pull(tmp_path):
    _, fake, _, response = setup(tmp_path, access=[{"member": CI, "level": "admin"}, {"member": CI_ACCOUNT, "level": "admin"}, {"member": DEVS, "level": "use"}])
    assert fake.members(REPOSITORY, "roles/artifactregistry.writer") == [CI, CI_ACCOUNT]
    assert fake.members(REPOSITORY, "roles/artifactregistry.reader") == [DEVS]
    assert all(entry.status == "GRANTED" for entry in response.access), response.access
    assert CI in fake.members(REPOSITORY, "roles/artifactregistry.writer"), "a federated identity is kept as written, case and all"


def test_a_federated_identity_is_granted_on_registries_only(tmp_path):
    _, fake, _, response = setup(tmp_path, access=[{"member": CI, "level": "admin"}], compute={"cpu": 2, "memory_gb": 4}, secret={"name": "token"})
    [registry], [machine], [secret] = response.resources.registry, response.resources.compute, response.resources.secret
    entry = next(item for item in response.access if item.member == CI)
    assert entry.status == "GRANTED" and entry.resources == [registry.id]
    for key in (("instance", machine.zone, machine.name), ("secret", secret.name)):
        assert all(CI not in binding["members"] for binding in fake.policies.get(key, {}).get("bindings", [])), key


def test_a_federated_identity_outside_the_allowed_pools_is_refused(tmp_path):
    other = "principalSet://iam.googleapis.com/projects/999/locations/global/workloadIdentityPools/gitlab/attribute.project_path/acme/shop"
    orchestrator, fake, who, _ = setup(tmp_path)
    found = orchestrator.validate(InfrastructureRequest.model_validate({"region": "us-central1", "registry": {"name": "x"}, "access": [{"member": other}]}), who)
    assert not found.valid and [problem.code for problem in found.problems] == ["INVALID_ACCESS"]
    assert "not a workload identity pool this service grants to" in found.problems[0].message


@pytest.mark.parametrize("member", [f"principalSet://{POOL}/*", f"principal://{POOL}", "principalSet://iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/x/y"])
def test_a_whole_pool_or_a_malformed_identity_is_not_a_member(member):
    with pytest.raises(ValueError, match="INVALID_ACCESS"):  # the contract's refusal, carried by pydantic's ValidationError
        InfrastructureRequest.model_validate({"region": "us-central1", "registry": {"name": "x"}, "access": [{"member": member}]})


def test_the_google_adapter_takes_google_pools_only(tmp_path):
    elsewhere = "sts.example.net/pools/ci"
    config = make_config(tmp_path, access={"allowed_domains": ["example.com"], "workload_identity_pools": [elsewhere]})
    orchestrator, _ = make_orchestrator(tmp_path, config=config)
    request = {"region": "us-central1", "registry": {"name": "x"}, "access": [{"member": f"principal://{elsewhere}/subject/job"}]}
    found = orchestrator.validate(InfrastructureRequest.model_validate(request), principal(config))
    assert [problem.code for problem in found.problems] == ["INVALID_ACCESS"] and "Google workload identity pool" in found.problems[0].message


def test_grant_and_revoke_after_creation(tmp_path):
    orchestrator, fake, who, response = setup(tmp_path)
    orchestrator.manage_access(response.request_id, "grant", CI, who, level="admin")
    assert fake.members(REPOSITORY, "roles/artifactregistry.writer") == [CI]
    orchestrator.manage_access(response.request_id, "grant", CI, who, level="use")
    assert fake.members(REPOSITORY, "roles/artifactregistry.writer") == [] and fake.members(REPOSITORY, "roles/artifactregistry.reader") == [CI]
    lower = CI.replace("Acme/Shop", "acme/shop")
    orchestrator.manage_access(response.request_id, "grant", lower, who, level="use")
    orchestrator.manage_access(response.request_id, "revoke", lower, who)
    assert fake.members(REPOSITORY, "roles/artifactregistry.reader") == [CI], "another case is another identity: revoking it leaves this one"


def test_connect_and_cost(tmp_path):
    orchestrator, _, who, response = setup(tmp_path, registry={"name": "images", "size_gb": 50})
    [registry] = response.resources.registry
    assert registry.connect.commands["login"] == "gcloud auth configure-docker us-central1-docker.pkg.dev"
    assert registry.connect.commands["push"] == f"docker push {ADDRESS}/IMAGE:TAG"
    line = next(item for item in orchestrator.cost(who, request_id=response.request_id)["estimate"]["lines"] if item["kind"] == "registry")
    assert line["monthly_usd"] == 5.0


def test_destroy_deletes_the_repository_and_its_policy(tmp_path):
    orchestrator, fake, who, response = setup(tmp_path, access=[{"member": CI, "level": "admin"}])
    destroyed = orchestrator.destroy(response.request_id, who, wait_seconds=45)
    assert destroyed.status == "DESTROYED" and ADDRESS not in fake.repository_bodies and REPOSITORY not in fake.policies
    assert ("registry", "shop-images") in fake.order


def test_one_registry_alone_through_its_own_tools(tmp_path):
    orchestrator, fake, who, _ = setup(tmp_path)
    made = orchestrator.create_registry(who, region="us-central1", registry_name="cache", name="ci", immutable_tags=True, wait_seconds=45)
    [registry] = made.resources.registry
    assert fake.repository_bodies["us-central1-docker.pkg.dev/test-project/ci-cache"]["dockerConfig"] == {"immutableTags": True}
    assert orchestrator.get_resource(registry.id, who, ResourceKind.registry).resource.status == "READY"
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.get_resource(registry.id, who, ResourceKind.queue)
    assert caught.value.code == ErrorCode.NOT_FOUND
    gone = orchestrator.delete_resource(registry.id, who, ResourceKind.registry, wait_seconds=45)
    assert gone.resource.status == "DELETED" and "us-central1-docker.pkg.dev/test-project/ci-cache" not in fake.repository_bodies


def test_a_failed_wait_still_deletes_the_repository(tmp_path):
    config = make_config(tmp_path, access={"allowed_domains": ["example.com"], "workload_identity_pools": [POOL]})
    orchestrator, fake = make_orchestrator(tmp_path, config=config)
    fake.fail_on.append(("GET", "/operations/repo-"))
    response = orchestrator.provision(InfrastructureRequest.model_validate({"region": "us-central1", "name": "shop", "registry": {"name": "images"}}),
                                      principal(config), wait_seconds=45)
    assert response.status == "FAILED" and response.error.details["cause"] == "PROVIDER_ERROR"
    fake.fail_on.clear()
    assert ADDRESS not in fake.repository_bodies, "the create was accepted, so cleanup deleted it"


def test_list_resource_kinds_offers_several_registries(tmp_path):
    orchestrator, _, _, _ = setup(tmp_path)
    kind = next(item for item in orchestrator.resource_kinds().kinds if item.kind == "registry")
    assert kind.several_per_request and {field.name for field in kind.fields} == {"name", "immutable_tags", "size_gb"}
