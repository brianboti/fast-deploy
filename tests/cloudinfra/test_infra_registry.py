"""Registries: a container image repository a request owns. Images are built only in the trusted project (decided
2026-10-30): only the operator's registry writers, the accounts that build there, push, named in access at admin and
granted on registries and nothing else; every other member pulls, whatever its level. A CI job's federated identity is
not a member at all."""
import pytest
from gcp_fakes import make_config, make_orchestrator, principal

from cloudinfra.config import AccessSettings
from cloudinfra.models import InfrastructureRequest, ResourceKind
from cloudinfra.models.errors import ErrorCode, InfrastructureError

BUILDER = "serviceAccount:build-dev@test-project.iam.gserviceaccount.com"
CI_ACCOUNT = "serviceAccount:shop-ci@example.com"
DEVS = "group:shop-devs@example.com"
ADDRESS = "us-central1-docker.pkg.dev/test-project/shop-images"
REPOSITORY = ("repository", ADDRESS)
ACCESS = {"allowed_domains": ["example.com"], "registry_writers": [BUILDER]}


def setup(tmp_path, access=(), **body):
    config = make_config(tmp_path, access=ACCESS)
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
    assert any("only the operator's registry writers" in item for item in response.warnings)


def test_only_the_build_account_pushes_and_everyone_else_pulls(tmp_path):
    _, fake, _, response = setup(tmp_path, access=[{"member": BUILDER, "level": "admin"}, {"member": CI_ACCOUNT, "level": "admin"}, {"member": DEVS, "level": "use"}])
    assert fake.members(REPOSITORY, "roles/artifactregistry.writer") == [BUILDER], "a CI account at admin is not a writer"
    assert set(fake.members(REPOSITORY, "roles/artifactregistry.reader")) >= {CI_ACCOUNT, DEVS}
    assert all(entry.status == "GRANTED" for entry in response.access), response.access


def test_a_person_at_admin_only_pulls(tmp_path):
    lead = "user:lead@example.com"
    _, fake, _, _ = setup(tmp_path, access=[{"member": lead, "level": "admin"}])
    assert lead in fake.members(REPOSITORY, "roles/artifactregistry.reader") and fake.members(REPOSITORY, "roles/artifactregistry.writer") == []


def test_a_workload_identity_at_admin_only_pulls(tmp_path):
    """The request's own workload identity (the identity kind) is an access member like any other, so the registry rule
    holds for it too: what runs on the request's machines can read its images and never push one."""
    _, fake, _, response = setup(tmp_path, access=[{"member": BUILDER, "level": "admin"}], identity={"name": "app", "level": "admin"})
    [identity] = response.resources.identity
    assert response.status == "READY", response.error
    assert f"serviceAccount:{identity.email}" in fake.members(REPOSITORY, "roles/artifactregistry.reader")
    assert fake.members(REPOSITORY, "roles/artifactregistry.writer") == [BUILDER]


def test_the_build_account_is_granted_on_registries_only(tmp_path):
    _, fake, _, response = setup(tmp_path, access=[{"member": BUILDER, "level": "admin"}], compute={"cpu": 2, "memory_gb": 4}, secret={"name": "token"})
    [registry], [machine], [secret] = response.resources.registry, response.resources.compute, response.resources.secret
    entry = next(item for item in response.access if item.member == BUILDER)
    assert entry.status == "GRANTED" and entry.resources == [registry.id]
    for key in (("instance", machine.zone, machine.name), ("secret", secret.name)):
        assert all(BUILDER not in binding["members"] for binding in fake.policies.get(key, {}).get("bindings", [])), key


def test_a_registry_writer_at_use_only_pulls(tmp_path):
    _, fake, _, _ = setup(tmp_path, access=[{"member": BUILDER, "level": "use"}])
    assert fake.members(REPOSITORY, "roles/artifactregistry.writer") == [] and BUILDER in fake.members(REPOSITORY, "roles/artifactregistry.reader")


@pytest.mark.parametrize("member", ["principalSet://iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/github/attribute.repository/acme/shop",
                                    "principal://iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/github/subject/repo:acme/shop:ref:refs/heads/main"])
def test_a_ci_jobs_federated_identity_is_not_a_member(member):
    with pytest.raises(ValueError, match="INVALID_ACCESS"):  # the contract's refusal, carried by pydantic's ValidationError
        InfrastructureRequest.model_validate({"region": "us-central1", "registry": {"name": "x"}, "access": [{"member": member}]})


def test_the_removed_pool_setting_says_what_replaced_it():
    with pytest.raises(ValueError, match="registry_writers"):
        AccessSettings.model_validate({"workload_identity_pools": ["iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/github"]})


@pytest.mark.parametrize("member", ["user:builder@example.com", "group:builders@example.com", "serviceAccount:not-an-email"])
def test_a_registry_writer_is_a_service_account(member):
    with pytest.raises(ValueError, match="registry_writers"):
        AccessSettings.model_validate({"registry_writers": [member]})


def test_a_registry_writer_may_be_named_outside_the_allowed_domains(tmp_path):
    orchestrator, _, who, _ = setup(tmp_path)
    found = orchestrator.validate(InfrastructureRequest.model_validate({"region": "us-central1", "registry": {"name": "x"},
                                                                        "access": [{"member": BUILDER, "level": "admin"}]}), who)
    assert found.valid, found.problems


def test_grant_and_revoke_after_creation(tmp_path):
    orchestrator, fake, who, response = setup(tmp_path)
    orchestrator.manage_access(response.request_id, "grant", BUILDER, who, level="admin")
    assert fake.members(REPOSITORY, "roles/artifactregistry.writer") == [BUILDER]
    orchestrator.manage_access(response.request_id, "grant", BUILDER, who, level="use")
    assert fake.members(REPOSITORY, "roles/artifactregistry.writer") == [] and BUILDER in fake.members(REPOSITORY, "roles/artifactregistry.reader")
    orchestrator.manage_access(response.request_id, "grant", CI_ACCOUNT, who, level="admin")
    assert CI_ACCOUNT not in fake.members(REPOSITORY, "roles/artifactregistry.writer")
    orchestrator.manage_access(response.request_id, "revoke", BUILDER, who)
    assert BUILDER not in fake.members(REPOSITORY, "roles/artifactregistry.reader")


def test_connect_and_cost(tmp_path):
    orchestrator, _, who, response = setup(tmp_path, registry={"name": "images", "size_gb": 50})
    [registry] = response.resources.registry
    assert registry.connect.commands["login"] == "gcloud auth configure-docker us-central1-docker.pkg.dev"
    assert "push" not in registry.connect.commands and registry.connect.commands["pull"] == f"docker pull {ADDRESS}/IMAGE@sha256:DIGEST"
    assert any("Nothing outside pushes here" in note for note in registry.connect.notes)
    line = next(item for item in orchestrator.cost(who, request_id=response.request_id)["estimate"]["lines"] if item["kind"] == "registry")
    assert line["monthly_usd"] == 5.0


def test_destroy_deletes_the_repository_and_its_policy(tmp_path):
    orchestrator, fake, who, response = setup(tmp_path, access=[{"member": BUILDER, "level": "admin"}])
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
    config = make_config(tmp_path, access=ACCESS)
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
