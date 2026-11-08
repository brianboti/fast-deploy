"""The delivery service on Azure (docs/research/RESEARCH-2026-11-03-azure.md, section 7; step 6b of section 10): machines
reached through the operator's Bastion with Entra sign-in as the environment's managed identity, services and jobs on
Container Apps with Key Vault references, AKS through its Entra audience, and a machine's pull with a registry token of
the pull identity. Against the fakes in azure_delivery_fakes.py; the real run comes once every capability is built."""
import base64
import json
import sqlite3

import httpx
import pytest
from azure_delivery_fakes import (
    APP_ID,
    AZ_V1,
    AZ_V2,
    AZ_V3,
    BASTION_SUBNET,
    ENVIRONMENT,
    IDENTITY,
    JOB_RUNTIME,
    JOB_STAND_IN,
    PULL,
    REGISTRY,
    RUNTIME,
    SECRET_URL,
    SECRET_VALUE,
    STAND_IN,
    SUBSCRIPTION,
    VAULT_HOST,
    FakeAz,
    FakeAzure,
    azure_answer,
    azure_config_dict,
    vm,
)
from delivery_fakes import principal, release_dict

from appdelivery import attest, azure, planner, remote
from appdelivery.clouds.azure import AzureCloud
from appdelivery.config import Config
from appdelivery.errors import DeliveryError, ErrorCode
from appdelivery.infra import InfraRequest
from appdelivery.models import ReleaseRequest
from appdelivery.releases import Delivery, InlineExecutor
from appdelivery.remote import Completed
from appdelivery.state import StateManager


class Clock:
    """Time that passes only when someone sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class AzureWorld:
    """A Delivery with an Azure environment, its fake Azure and Azure CLI, and cloudinfra's answer a test can change."""

    def __init__(self, tmp_path, service=False, job=False, **environment):
        self.config = Config.model_validate(azure_config_dict(str(tmp_path / "az"), **environment))
        self.state, self.azure, self.az, self.clock = StateManager(":memory:"), FakeAzure(), FakeAz(tmp_path / "machines"), Clock()
        self.answer = azure_answer(service, job)
        http = self.azure.client()
        self.delivery = Delivery(self.config, self.state, infra=self.infra, http=http, executor=InlineExecutor(), build_executor=InlineExecutor(),
                                 sleep=self.clock.sleep, clock=self.clock)
        self.delivery.clouds["azure"] = AzureCloud(self.delivery, tokens=azure.ManagedIdentityTokens(http, environ={}), runner=self.az, sleep=self.clock.sleep,
                                                   clock=self.clock)
        self.team = principal(self.config)
        for number, image in enumerate((AZ_V1, AZ_V2, AZ_V3), 1):
            self.state.create_build(f"bld-{number:012x}", "dev", "shop-team", "shop-team", "shop-web", "a" * 40, image.split("@")[0] + ":t", "WORKING", f"run-{number}", None,
                                    {"environment": "dev", "source": "shop-web", "revision": "a" * 40, "image": "web", "dockerfile": "Dockerfile", "context": "."})
            self.state.update_build(f"bld-{number:012x}", ("WORKING",), "SUCCEEDED", built=image)

    def infra(self, environment):
        world = self

        class Client:
            def get_request(self, request_id):
                return InfraRequest.model_validate(world.answer)
        return Client()

    def release(self, image=AZ_V1, **overrides):
        return ReleaseRequest.model_validate(release_dict(environment=ENVIRONMENT, image=image, **overrides))

    def deploy(self, image=AZ_V1, **overrides):
        return self.delivery.deploy(self.release(image, **overrides), self.team)

    def arm(self, method=None):
        return [call for call in self.azure.calls if call[1] == "management.azure.com" and (method is None or call[0] == method)]


# ---- the configuration

def test_an_azure_environment_names_two_managed_identities_and_the_operators_bastion(tmp_path):
    data = azure_config_dict(str(tmp_path))
    environment = Config.model_validate(data).environments[ENVIRONMENT]
    assert environment.member == f"serviceAccount:{IDENTITY['object_id']}", "cloudinfra takes a managed identity as serviceAccount:<object id>"
    assert environment.identity == f"managed identity {IDENTITY['object_id']}"

    def refused(change, said):
        broken = azure_config_dict(str(tmp_path))
        change(broken)
        with pytest.raises(ValueError, match=said):
            Config.model_validate(broken)
    refused(lambda d: d.pop("azure"), "are on azure: the configuration needs its azure block")
    refused(lambda d: d["environments"][ENVIRONMENT].pop("pull_identity"), "needs managed_identity and pull_identity")
    refused(lambda d: d["environments"][ENVIRONMENT].update(pull_identity=dict(IDENTITY)), "pull_identity must be an identity of its own")
    refused(lambda d: d["environments"][ENVIRONMENT].update(build={"service_account": "build-az@infra-project.iam.gserviceaccount.com"}), "step 6c")
    refused(lambda d: d["environments"][ENVIRONMENT].update(require_provenance=True), "step 6c")
    refused(lambda d: d["environments"][ENVIRONMENT].update(service_account="appdelivery-az@infra-project.iam.gserviceaccount.com"), "a Google environment's")
    refused(lambda d: d["environments"]["dev"].update(managed_identity=dict(PULL)), "an Azure environment's")
    refused(lambda d: d["azure"].update(bastion="/subscriptions/x/bastionHosts/b"), "bastion")
    # an operator on Azure alone needs no Google block, unless it builds there
    alone = azure_config_dict(str(tmp_path))
    alone.pop("gcp")
    alone["environments"] = {ENVIRONMENT: alone["environments"][ENVIRONMENT]}
    alone["sources"] = {}
    alone["principals"] = [{**alone["principals"][0], "environments": [ENVIRONMENT]}]
    assert Config.model_validate(alone).gcp is None
    alone["sources"] = azure_config_dict(str(tmp_path))["sources"]
    with pytest.raises(ValueError, match="sources are built by Cloud Build"):
        Config.model_validate(alone)


def test_cloudinfras_answer_for_an_azure_request_is_understood():
    infra = InfraRequest.model_validate(azure_answer(service=True, job=True))
    machine = infra.group("web")[0]
    assert machine.zone == "1" and machine.provider == "azure" and machine.provider_id == vm("shop-web-1")
    assert infra.service(True).provider_id == APP_ID and infra.service(True).region == "canadacentral"
    assert infra.readable_by(infra.service(True), infra.secret("db-password")), "Azure names the readable secrets by resource id"
    assert infra.secret("db-password").resource_name == SECRET_URL


# ---- the plan

def problems_of(world, release=None, answer=None):
    found, warnings = planner.check(release or world.release(), world.team, world.config, InfraRequest.model_validate(answer or world.answer))
    return [(item.code, item.message) for item in found], warnings


def test_a_machine_on_azure_needs_the_container_host_the_bastion_and_a_way_out(tmp_path):
    world = AzureWorld(tmp_path)
    assert problems_of(world) == ([], [])
    assert world.delivery.plan(world.release(), world.team).machines[0].provider_id == vm("shop-web-1")

    def changed(change):
        answer = azure_answer()
        change(answer)
        return problems_of(world, answer=answer)[0]
    found = changed(lambda a: a["resources"]["compute"][0].update(profile="gpu-host"))
    assert found and found[0][0] == ErrorCode.UNSUPPORTED_TARGET and "container-host" in found[0][1]
    found = changed(lambda a: a["resources"]["network"][0]["firewall_rules"].pop(1))
    assert {code for code, _ in found} == {ErrorCode.UNREACHABLE_TARGET} and BASTION_SUBNET in found[0][1]
    found = changed(lambda a: a["resources"]["network"][0].update(nat=False))
    assert {code for code, _ in found} == {ErrorCode.UNREACHABLE_TARGET} and "no NAT" in found[0][1]
    found = changed(lambda a: a["resources"]["compute"][0].update(provider_id="/subscriptions/x/virtualMachines/../../evil"))
    assert found == [(ErrorCode.UNREACHABLE_TARGET, "The infrastructure layer reports no Azure resource id for shop-web-1, which the Bastion reaches it by")]
    found = changed(lambda a: a["access"][0].update(member="serviceAccount:appdelivery-dev@infra-project.iam.gserviceaccount.com"))
    assert found[0][0] == ErrorCode.NOT_GRANTED and IDENTITY["object_id"] in found[0][1]


def test_an_environment_releases_only_on_its_own_cloud(tmp_path):
    world = AzureWorld(tmp_path)
    answer = azure_answer()
    answer["access"][0]["member"] = world.config.environments["dev"].member
    found, _ = problems_of(world, release=ReleaseRequest.model_validate(release_dict(environment="dev", image=AZ_V1)), answer=answer)
    assert [code for code, _ in found].count(ErrorCode.UNSUPPORTED_TARGET) == 2 and "is on azure, and this environment releases on gcp" in found[0][1]


# ---- tokens, Key Vault and the registry

def recorder(answer):
    seen = []

    def handle(request):
        seen.append(request)
        return answer(request)
    return httpx.Client(transport=httpx.MockTransport(handle)), seen


def test_tokens_come_from_the_hosts_own_managed_identity_endpoint_by_client_id():
    client, seen = recorder(lambda request: httpx.Response(200, json={"access_token": "t1", "expires_on": "4102444800"}))
    tokens = azure.ManagedIdentityTokens(client, environ={}, clock=lambda: 1_000_000)
    assert tokens(IDENTITY["client_id"], azure.ARM) == "t1" and tokens(IDENTITY["client_id"], azure.ARM) == "t1"
    [asked] = seen
    assert asked.url.host == "169.254.169.254" and asked.headers["Metadata"] == "true"
    assert dict(asked.url.params) == {"api-version": "2018-02-01", "resource": azure.ARM, "client_id": IDENTITY["client_id"]}
    # on Container Apps or App Service: IDENTITY_ENDPOINT on the loopback, with its header
    client, seen = recorder(lambda request: httpx.Response(200, json={"access_token": "t2", "expires_on": "4102444800"}))
    tokens = azure.ManagedIdentityTokens(client, environ={"IDENTITY_ENDPOINT": "http://localhost:42356/msi/token", "IDENTITY_HEADER": "h"})
    assert tokens(PULL["client_id"], azure.REGISTRY) == "t2" and seen[0].headers["X-IDENTITY-HEADER"] == "h" and seen[0].url.params["api-version"] == "2019-08-01"
    # the header is the host's credential: never sent off the host
    client, seen = recorder(lambda request: httpx.Response(200, json={"access_token": "t3"}))
    with pytest.raises(DeliveryError) as refused:
        azure.ManagedIdentityTokens(client, environ={"IDENTITY_ENDPOINT": "http://collector.example.com/token", "IDENTITY_HEADER": "h"})(PULL["client_id"], azure.ARM)
    assert refused.value.code == ErrorCode.AZURE_AUTH_FAILED and seen == []
    client, _ = recorder(lambda request: httpx.Response(400, json={"error": "invalid_request", "error_description": "Identity not found"}))
    with pytest.raises(DeliveryError) as refused:
        azure.ManagedIdentityTokens(client, environ={})(PULL["client_id"], azure.ARM)
    assert refused.value.code == ErrorCode.AZURE_AUTH_FAILED and "attach it to the host" in refused.value.message


def test_a_reference_is_pinned_from_the_version_list_without_reading_the_value():
    fake = FakeAzure()
    client = fake.client()
    assert azure.secret_version(client, "kv-token", SECRET_URL) == "1111new", "the newest enabled version; the disabled one cloudinfra made is skipped"
    assert [call[2] for call in fake.calls] == ["/secrets/shop-db-password-1a2b3c/versions"], "no value was asked for"
    assert azure.read_secret(client, "kv-token", SECRET_URL) == SECRET_VALUE
    fake.versions = [item for item in fake.versions if not item["attributes"]["enabled"]]
    with pytest.raises(DeliveryError, match="no enabled version: give it a value first"):
        azure.secret_version(client, "kv-token", SECRET_URL)
    # an address of another shape never gets the token
    before = len(fake.calls)
    for address in ("https://collector.example.com/secrets/x", f"https://{VAULT_HOST}.example.com/secrets/x", f"{SECRET_URL}/1111new", "http://shop3f6ckv.vault.azure.net/secrets/x"):
        with pytest.raises(DeliveryError) as refused:
            azure.read_secret(client, "kv-token", address)
        assert refused.value.code == ErrorCode.SECRET_UNAVAILABLE
    with pytest.raises(DeliveryError) as refused:
        azure.registry_token(client, "entra", "collector.example.com")
    assert refused.value.code == ErrorCode.REGISTRY_NOT_ALLOWED and len(fake.calls) == before


def test_the_azure_cli_runs_with_a_fixed_argument_list_and_none_of_the_services_own_settings():
    seen = []

    def runner(argv, stdin, env, timeout):
        seen.append((argv, stdin, env))
        return Completed(0, "", "")
    base = {"PATH": "/usr/bin", "HOME": "/home/appdelivery", "APPDELIVERY_DEV_INFRA_TOKEN": "cloudinfra-token", "IDENTITY_ENDPOINT": "http://localhost:1/msi",
            "IDENTITY_HEADER": "h", "APPDELIVERY_TOKEN_PEPPER": "pepper"}
    remote.az_login_identity(IDENTITY["client_id"], "/data/azure/azure-dev", runner, base)
    remote.az_bastion_ssh((SUBSCRIPTION, "hub", "hub-bastion"), vm("shop-web-1"), "IN_ACTION=…", "/data/azure/azure-dev", runner, 300, base)
    (login, _, env), (ssh, stdin, _) = seen
    assert login == ["az", "login", "--identity", "--client-id", IDENTITY["client_id"], "--allow-no-subscriptions", "--output", "none"]
    assert ssh == ["az", "network", "bastion", "ssh", "--subscription", SUBSCRIPTION, "--resource-group", "hub", "--name", "hub-bastion", "--target-resource-id",
                   vm("shop-web-1"), "--auth-type", "AAD", "--", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                   "sudo bash -s"] and stdin == "IN_ACTION=…"
    assert env["AZURE_CONFIG_DIR"] == "/data/azure/azure-dev" and env["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] == "no" and env["IDENTITY_HEADER"] == "h"
    assert not [name for name in env if name.startswith("APPDELIVERY")], "the cloudinfra tokens and the pepper stay in the service"


# ---- machines

def test_a_machine_release_goes_through_the_bastion_as_the_managed_identity_and_pulls_with_the_pull_identity(tmp_path):
    world = AzureWorld(tmp_path)
    response = world.deploy()
    assert response.status == "RUNNING", response.error
    logins = [call for call in world.az.calls if call["argv"][:2] == ["az", "login"]]
    assert logins and all(IDENTITY["client_id"] in call["argv"] and call["env"]["AZURE_CONFIG_DIR"] == str(tmp_path / "az" / ENVIRONMENT) for call in logins)
    sessions = [call["argv"] for call in world.az.calls if call["argv"][:4] == ["az", "network", "bastion", "ssh"]]
    assert [argv[argv.index("--target-resource-id") + 1] for argv in sessions] == [vm("shop-web-1"), vm("shop-web-2")]
    for name in ("shop-web-1", "shop-web-2"):
        host = world.az.host(vm(name))
        app = host.container("app")
        assert app["image"] == AZ_V1 and f"DB_PASSWORD={SECRET_VALUE}" in app["env"] and "DB_HOST=10.20.0.5" in app["env"]
        assert (host.state / "login_user").read_text().strip() == azure.REGISTRY_USER
        assert (host.state / "login_password").read_text() == f"acr-refresh:token:{PULL['client_id'][:8]}:{azure.REGISTRY}", "the pull identity's registry token only"
    vault = [call for call in world.azure.calls if call[1] == VAULT_HOST]
    assert vault and all(call[4] == f"Bearer token:{IDENTITY['client_id'][:8]}:{azure.KEY_VAULT}" for call in vault), "secrets are read as the environment"
    exchanges = [call for call in world.azure.calls if call[1] == REGISTRY]
    assert len(exchanges) == 2 and all(call[3]["access_token"] == [f"token:{PULL['client_id'][:8]}:{azure.REGISTRY}"] for call in exchanges)
    assert SECRET_VALUE not in json.dumps(world.state.get_release(response.release_id))
    # a second release, its rollback and its removal go the same way
    second = world.deploy(AZ_V2)
    assert second.status == "RUNNING" and world.az.host(vm("shop-web-1")).container("app")["image"] == AZ_V2
    assert world.delivery.rollback(second.release_id, world.team).status == "ROLLED_BACK"
    assert world.az.host(vm("shop-web-2")).container("app")["image"] == AZ_V1


def test_a_login_role_not_yet_applied_is_waited_for_and_the_clis_own_refusal_is_no_script_failure(tmp_path):
    world = AzureWorld(tmp_path)
    world.az.refusals = [Completed(255, "", "appdelivery@shop-web-1: Permission denied (publickey).\r\n")] * 3
    response = world.deploy()
    assert response.status == "RUNNING", response.error
    assert world.clock.now == 90, "three refusals, thirty seconds apart"
    world = AzureWorld(tmp_path / "second")
    world.az.refusals = [Completed(1, "", "ERROR: (ResourceNotFound) The Resource 'Microsoft.Network/bastionHosts/hub-bastion' was not found.\n")]
    response = world.deploy()
    assert response.status == "FAILED" and response.targets[0].error["code"] == ErrorCode.MACHINE_UNREACHABLE, response.targets[0].error


# ---- services and jobs on Container Apps

def service_release(world, image=AZ_V1, **overrides):
    return world.delivery.deploy(world.release(image, target={"service": True}, **overrides), world.team)


def test_a_service_release_is_a_revision_that_gets_the_traffic_only_once_healthy(tmp_path):
    world = AzureWorld(tmp_path, service=True)
    first = service_release(world)
    assert first.status == "RUNNING", first.error
    revision = f"shop-web--{first.release_id}"
    assert world.azure.serving() == revision
    patches = [call[3] for call in world.arm("PATCH")]
    assert patches[0]["properties"]["configuration"]["ingress"]["traffic"] == [{"revisionName": "shop-web--initial", "weight": 100}], "held where it was"
    assert patches[-1]["properties"]["configuration"]["ingress"]["traffic"] == [{"revisionName": revision, "weight": 100}]
    configuration = world.azure.app["properties"]["configuration"]
    assert configuration["secrets"] == [{"name": f"{first.release_id}-db-password", "keyVaultUrl": f"{SECRET_URL}/1111new", "identity": RUNTIME}]
    assert configuration["registries"] == [{"server": REGISTRY, "identity": RUNTIME}] and configuration["ingress"]["targetPort"] == 8080
    [container] = world.azure.revisions[revision]["properties"]["template"]["containers"]
    assert {"name": "DB_PASSWORD", "secretRef": f"{first.release_id}-db-password"} in container["env"] and container["resources"] == {"cpu": 0.5, "memory": "1Gi"}
    assert container["probes"] == [{"type": "Startup", "httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 6, "timeoutSeconds": 5, "failureThreshold": 10}]
    assert not world.azure.revisions["shop-web--initial"]["properties"]["active"], "a revision that serves nothing stops costing"
    assert not [call for call in world.azure.calls if call[1] == VAULT_HOST and not call[2].endswith("/versions")], "no secret value passed through"
    # the next release keeps the serving one's references, so its revision still starts after a rollback
    second = service_release(world, AZ_V2)
    assert second.status == "RUNNING" and world.azure.serving() == f"shop-web--{second.release_id}"
    assert [item["name"] for item in world.azure.app["properties"]["configuration"]["secrets"]] == [f"{first.release_id}-db-password", f"{second.release_id}-db-password"]
    # a release that fails its probe never serves, and stops
    world.azure.unhealthy.add(AZ_V3)
    third = service_release(world, AZ_V3)
    assert third.status != "RUNNING" and world.azure.serving() == f"shop-web--{second.release_id}"
    assert not world.azure.revisions[f"shop-web--{third.release_id}"]["properties"]["active"]
    # rollback activates the previous release's revision and moves the traffic back
    assert world.delivery.rollback(second.release_id, world.team).status == "ROLLED_BACK"
    assert world.azure.serving() == revision and world.azure.revisions[revision]["properties"]["active"]
    assert not world.azure.revisions[f"shop-web--{second.release_id}"]["properties"]["active"]
    # remove puts the stand-in back on its port, with no references
    assert world.delivery.get(first.release_id, world.team).status == "RUNNING"
    assert world.delivery.remove(first.release_id, world.team).status == "REMOVED"
    serving = world.azure.serving()
    assert "--rel-" not in serving and world.azure.revisions[serving]["properties"]["template"]["containers"][0]["image"] == STAND_IN
    assert world.azure.app["properties"]["configuration"]["secrets"] == [] and world.azure.app["properties"]["configuration"]["ingress"]["targetPort"] == 80


def test_a_service_keeps_its_port_while_a_release_serves_and_a_new_grant_is_waited_for(tmp_path):
    world = AzureWorld(tmp_path, service=True)
    world.azure.denied = 2  # Azure has not applied the role assignment cloudinfra just made
    first = service_release(world)
    assert first.status == "RUNNING" and world.clock.now >= 30
    moved = service_release(world, AZ_V2, port=9090)
    assert moved.status != "RUNNING" and "one port per app" in json.dumps(moved.targets[0].error or moved.error)
    assert world.azure.serving() == f"shop-web--{first.release_id}"


def test_a_job_release_replaces_its_container_and_starts_runs_with_the_jobs_own_template(tmp_path):
    world = AzureWorld(tmp_path, job=True)
    response = world.delivery.deploy(world.release(target={"job": True}), world.team)
    assert response.status == "RUNNING", response.error
    job = world.azure.job
    assert job["tags"] == {"cloudinfra-request": world.answer["request_id"], "appdelivery-release": response.release_id}
    [container] = job["properties"]["template"]["containers"]
    assert container["image"] == AZ_V1 and container["resources"] == {"cpu": 0.5, "memory": "1Gi"} and "probes" not in container
    assert job["properties"]["configuration"]["secrets"] == [{"name": f"{response.release_id}-db-password", "keyVaultUrl": f"{SECRET_URL}/1111new", "identity": JOB_RUNTIME}]
    assert job["properties"]["configuration"]["registries"] == [{"server": REGISTRY, "identity": JOB_RUNTIME}]
    run = world.delivery.start_run(response.release_id, world.team)
    assert (run.status, run.tasks, run.execution) == ("RUNNING", 2, "shop-nightly-0000001")
    world.azure.runs[run.execution]["properties"].update(status="Succeeded", endTime="2026-11-05T10:03:00Z")
    done = world.delivery.get_run(response.release_id, run.execution, world.team)
    assert (done.status, done.succeeded, done.failed, done.finished_at) == ("SUCCEEDED", 2, 0, "2026-11-05T10:03:00Z")
    assert world.delivery.remove(response.release_id, world.team).status == "REMOVED"
    assert world.azure.job["properties"]["template"]["containers"][0]["image"] == JOB_STAND_IN and world.azure.job["tags"]["appdelivery-release"] == "none"


def test_a_cluster_on_azure_is_reached_with_the_managed_identitys_token_for_aks(tmp_path):
    world = AzureWorld(tmp_path)
    deployer = world.delivery.clouds["azure"].clusters(ENVIRONMENT)
    assert deployer.token() == f"token:{IDENTITY['client_id'][:8]}:{azure.AKS}"
    assert deployer.limits == world.config.environments[ENVIRONMENT].workload


def test_builds_and_provenance_on_azure_wait_for_step_6c(tmp_path):
    world = AzureWorld(tmp_path)
    cloud = world.delivery.clouds["azure"]
    with pytest.raises(DeliveryError) as refused:
        cloud.builds(ENVIRONMENT)
    assert refused.value.code == ErrorCode.BUILD_UNAVAILABLE
    with pytest.raises(DeliveryError) as refused:
        cloud.provenance(world.release(), {})
    assert refused.value.code == ErrorCode.PROVENANCE_FAILED


# ---- what changed beside

def test_a_registry_login_for_cosign_takes_the_clouds_user_name(tmp_path):
    attest.docker_config(str(tmp_path), REGISTRY, "acr-refresh", azure.REGISTRY_USER)
    config = json.loads((tmp_path / "config.json").read_text())
    assert base64.b64decode(config["auths"][REGISTRY]["auth"]).decode() == f"{azure.REGISTRY_USER}:acr-refresh"
    other = tmp_path / "other"
    other.mkdir()
    attest.docker_config(str(other), "ghcr.io", "token", azure.REGISTRY_USER)
    assert json.loads((other / "config.json").read_text()) == {"auths": {}}


def test_a_state_made_before_targets_had_a_cloud_id_gets_the_column(tmp_path):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE targets (release_id TEXT NOT NULL, position INTEGER NOT NULL, resource_id TEXT NOT NULL, name TEXT NOT NULL, zone TEXT NOT NULL, "
                   "status TEXT NOT NULL, started_at TEXT, finished_at TEXT, error_json TEXT, log TEXT, PRIMARY KEY (release_id, resource_id))")
        db.execute("INSERT INTO targets VALUES ('rel-000000000001', 0, 'cmp-1', 'web-1', 'us-central1-a', 'DONE', NULL, NULL, NULL, NULL)")
    StateManager(path)
    StateManager(path)  # and a second start changes nothing
    with sqlite3.connect(path) as db:
        assert [row[1] for row in db.execute("PRAGMA table_info(targets)")][-1] == "provider_id"
        assert db.execute("SELECT provider_id FROM targets").fetchall() == [(None,)]


def test_the_service_status_names_each_environments_cloud_and_identity(tmp_path):
    world = AzureWorld(tmp_path)
    environments = world.delivery.service_status("test")["environments"]
    assert environments[ENVIRONMENT]["provider"] == "azure" and environments[ENVIRONMENT]["service_account"] == f"managed identity {IDENTITY['object_id']}"
    assert environments["dev"]["provider"] == "gcp"
