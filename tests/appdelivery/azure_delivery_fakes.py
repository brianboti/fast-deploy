"""Fakes for the delivery service on Azure (docs/research/RESEARCH-2026-11-03-azure.md, section 7): a configuration with
an Azure environment, cloudinfra's answer for an Azure request, an Azure that answers the managed identity endpoint, Key
Vault, a registry's token exchange and Container Apps from memory, and an `az` that runs the real host script on a fake
machine per resource id. Self-contained, as the other fakes here are."""
import base64
import copy
import itertools
from urllib.parse import parse_qs

import httpx
from delivery_fakes import REQUEST_ID, config_dict, infra_response
from host_fakes import FakeHost

from appdelivery.remote import Completed

SUBSCRIPTION = "0000aaaa-0000-4000-8000-0000000000aa"
GROUP = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/shop-3f6c0000"
BASTION = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/hub/providers/Microsoft.Network/bastionHosts/hub-bastion"
BASTION_SUBNET = "10.200.0.0/26"
IDENTITY = {"client_id": "11111111-1111-4111-8111-111111111111", "object_id": "22222222-2222-4222-8222-222222222222"}
PULL = {"client_id": "33333333-3333-4333-8333-333333333333", "object_id": "44444444-4444-4444-8444-444444444444"}
REGISTRY = "platformimages.azurecr.io"
AZ_V1 = f"{REGISTRY}/apps/shop/web@sha256:" + "4" * 64
AZ_V2 = f"{REGISTRY}/apps/shop/web@sha256:" + "5" * 64
AZ_V3 = f"{REGISTRY}/apps/shop/web@sha256:" + "6" * 64
VAULT_HOST = "shop3f6ckv.vault.azure.net"
SECRET_URL = f"https://{VAULT_HOST}/secrets/shop-db-password-1a2b3c"
SECRET_ID = f"{GROUP}/providers/Microsoft.KeyVault/vaults/shop3f6ckv/secrets/shop-db-password-1a2b3c"
SECRET_VALUE = "az-s3cr3t-Never-Kept"
APP_ID = f"{GROUP}/providers/Microsoft.App/containerApps/shop-web"
JOB_ID = f"{GROUP}/providers/Microsoft.App/jobs/shop-nightly"
RUNTIME = f"{GROUP}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/shop-web-run"
JOB_RUNTIME = f"{GROUP}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/shop-nightly-run"
STAND_IN, JOB_STAND_IN = "mcr.microsoft.com/k8se/quickstart:latest", "mcr.microsoft.com/k8se/quickstart-jobs:latest"
ENVIRONMENT = "azure-dev"


def vm(name: str) -> str:
    return f"{GROUP}/providers/Microsoft.Compute/virtualMachines/{name}"


def azure_config_dict(cli_directory: str, **environment) -> dict:
    data = config_dict()
    data["azure"] = {"bastion": BASTION, "bastion_subnet": BASTION_SUBNET, "cli_directory": cli_directory}
    data["environments"][ENVIRONMENT] = {"provider": "azure", "token_env": "APPDELIVERY_AZURE_DEV_INFRA_TOKEN", "managed_identity": dict(IDENTITY),
                                         "pull_identity": dict(PULL), "allowed_registries": [f"{REGISTRY}/apps"], **environment}
    data["principals"][0]["environments"] = ["dev", "prod", ENVIRONMENT]
    return data


def azure_answer(service: bool = False, job: bool = False) -> dict:
    """cloudinfra's answer for an Azure request of shop-team: a network whose subnet admits the Bastion on port 22 and has NAT,
    two container hosts in zone 1, the database's password in the request's vault, and the environment's managed identity
    granted admin; with a container app or a Container Apps job when asked."""
    data = infra_response()
    data.update(provider="azure", region="denmarkeast", zone="1")
    network = data["resources"]["network"][0]
    network.update(provider="azure", region="denmarkeast", cidr="10.10.0.0/25", nat=True, subnet=f"{GROUP}/providers/Microsoft.Network/virtualNetworks/shop-net/subnets/shop-subnet",
                   firewall_rules=[{"name": "allow-internal", "direction": "ingress", "ports": [], "sources": ["10.10.0.0/25"]},
                                   {"name": "allow-bastion-ssh", "direction": "ingress", "ports": [22], "sources": [BASTION_SUBNET]},
                                   {"name": "allow-public", "direction": "ingress", "ports": [8080], "sources": ["0.0.0.0/0"]}])
    for machine in data["resources"]["compute"]:
        machine.update(provider="azure", region="denmarkeast", zone="1", os_image="ubuntu-24_04-lts", provider_id=vm(machine["name"]), machine_type="Standard_B1s")
    secret = data["resources"]["secret"][0]
    secret.update(provider="azure", region="denmarkeast", resource_name=SECRET_URL, provider_id=SECRET_ID)
    data["endpoints"]["secret_names"] = [SECRET_URL]
    grant = data["access"][0]
    grant["member"] = f"serviceAccount:{IDENTITY['object_id']}"
    if service:
        data["resources"]["service"] = [{"id": "svc-777777777777", "kind": "service", "name": "shop-web", "provider": "azure", "region": "canadacentral", "status": "READY",
                                         "provider_id": APP_ID, "runtime_service_account": RUNTIME, "secrets": [SECRET_ID], "url": "https://shop-web.internal.example.azurecontainerapps.io",
                                         "revision": "shop-web--initial"}]
        grant["resources"].append("svc-777777777777")
    if job:
        data["resources"]["job"] = [{"id": "job-888888888888", "kind": "job", "name": "shop-nightly", "provider": "azure", "region": "canadacentral", "status": "READY",
                                     "provider_id": JOB_ID, "runtime_service_account": JOB_RUNTIME, "secrets": [SECRET_ID], "schedule": None}]
        grant["resources"].append("job-888888888888")
    assert data["request_id"] == REQUEST_ID
    return copy.deepcopy(data)


def merge(target: dict, patch: dict) -> None:
    """JSON merge patch, as Resource Manager's PATCH on Container Apps applies it: objects merge, arrays and values replace."""
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class FakeAzure:
    """The Azure a release on Azure meets, answering from memory and recording every call (method, host, path, body)."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.versions = [{"id": f"{SECRET_URL}/0000dis", "attributes": {"enabled": False, "created": 1}},
                         {"id": f"{SECRET_URL}/1111new", "attributes": {"enabled": True, "created": 3}},
                         {"id": f"{SECRET_URL}/0000old", "attributes": {"enabled": True, "created": 2}}]
        self.unhealthy: set[str] = set()  # images whose revisions fail their startup probe
        self.denied = 0  # the next ARM calls answered 403, as a role assignment Azure has not applied yet
        self.executions = itertools.count(1)
        container = {"name": "app", "image": STAND_IN, "resources": {"cpu": 0.5, "memory": "1Gi"}}
        self.app = {"id": APP_ID, "name": "shop-web", "properties": {
            "provisioningState": "Succeeded", "latestRevisionName": "shop-web--initial", "latestReadyRevisionName": "shop-web--initial",
            "configuration": {"activeRevisionsMode": "Multiple", "ingress": {"external": True, "targetPort": 80, "traffic": [{"latestRevision": True, "weight": 100}]},
                              "secrets": [], "registries": []},
            "template": {"containers": [container], "scale": {"minReplicas": 1, "maxReplicas": 5}}}}
        self.revisions = {"shop-web--initial": self._revision("shop-web--initial", self.app["properties"]["template"])}
        self.job = {"id": JOB_ID, "name": "shop-nightly", "tags": {"cloudinfra-request": REQUEST_ID}, "properties": {
            "provisioningState": "Succeeded", "configuration": {"triggerType": "Manual", "manualTriggerConfig": {"parallelism": 2, "replicaCompletionCount": 2},
                                                                "secrets": [], "registries": []},
            "template": {"containers": [{"name": "job", "image": JOB_STAND_IN, "resources": {"cpu": 0.5, "memory": "1Gi"}}]}}}
        self.runs: dict[str, dict] = {}

    def _revision(self, name: str, template: dict) -> dict:
        image = template["containers"][0]["image"]
        failing = image in self.unhealthy
        return {"name": name, "properties": {"active": True, "provisioningState": "Provisioned", "healthState": "Unhealthy" if failing else "Healthy",
                                             "runningState": "Failed" if failing else "Running", "template": copy.deepcopy(template),
                                             "provisioningError": "startup probe failed" if failing else None}}

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def ingress(self) -> dict:
        return self.app["properties"]["configuration"]["ingress"]

    def serving(self) -> str | None:
        for item in self.ingress()["traffic"]:
            if item["weight"] == 100:
                return item.get("revisionName") or self.app["properties"]["latestReadyRevisionName"]
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            body = parse_qs(request.content.decode()) if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded") else __import__("json").loads(request.content)
        host, path = request.url.host, request.url.path
        self.calls.append((request.method, host, path, body, request.headers.get("authorization"), dict(request.url.params)))
        if host == "169.254.169.254":
            assert request.headers.get("metadata") == "true"
            params = request.url.params
            return httpx.Response(200, json={"access_token": f"token:{params['client_id'][:8]}:{params['resource']}", "expires_on": "4102444800"})
        if host == VAULT_HOST:
            if path.endswith("/versions"):
                return httpx.Response(200, json={"value": self.versions})
            return httpx.Response(200, json={"value": SECRET_VALUE, "id": f"{SECRET_URL}/1111new"})
        if host == REGISTRY and path == "/oauth2/exchange":
            return httpx.Response(200, json={"refresh_token": f"acr-refresh:{body['access_token'][0]}"})
        if host == "management.azure.com":
            if self.denied:
                self.denied -= 1
                return httpx.Response(403, json={"error": {"code": "AuthorizationFailed"}})
            return self.arm(request.method, path, body)
        return httpx.Response(599, json={"error": f"no fake for {host}"})

    def arm(self, method: str, path: str, body) -> httpx.Response:
        if path.startswith(APP_ID):
            rest = path[len(APP_ID):]
            if rest == "" and method == "GET":
                return httpx.Response(200, json=self.app)
            if rest == "" and method == "PATCH":
                template = (body.get("properties") or {}).get("template")
                merge(self.app, body)
                if template is not None:  # a revision-scope change makes a revision
                    name = f"shop-web--{template.get('revisionSuffix') or 'auto'}"
                    self.revisions[name] = self._revision(name, self.app["properties"]["template"])
                    self.app["properties"]["latestRevisionName"] = name
                    if self.revisions[name]["properties"]["healthState"] == "Healthy":
                        self.app["properties"]["latestReadyRevisionName"] = name
                return httpx.Response(202)
            if rest == "/revisions" and method == "GET":
                return httpx.Response(200, json={"value": list(self.revisions.values())})
            name = rest.removeprefix("/revisions/").split("/")[0]
            if name not in self.revisions:
                return httpx.Response(404, json={"error": {"code": "ContainerAppRevisionNotFound"}})
            if rest.endswith("/deactivate"):
                self.revisions[name]["properties"]["active"] = False
                return httpx.Response(200)
            if rest.endswith("/activate"):
                self.revisions[name]["properties"]["active"] = True
                return httpx.Response(200)
            return httpx.Response(200, json=self.revisions[name])
        if path.startswith(JOB_ID):
            rest = path[len(JOB_ID):]
            if rest == "" and method == "GET":
                return httpx.Response(200, json=self.job)
            if rest == "" and method == "PATCH":
                merge(self.job, body)
                return httpx.Response(202)
            if rest == "/start" and method == "POST":
                assert body == {}, "a start never carries another image or command"
                name = f"shop-nightly-{next(self.executions):07d}"
                self.runs[name] = {"name": name, "properties": {"status": "Running", "startTime": "2026-11-05T10:00:00Z", "endTime": None}}
                return httpx.Response(202, json={"name": name, "id": f"{JOB_ID}/executions/{name}"})
            name = rest.removeprefix("/executions/")
            if name in self.runs:
                return httpx.Response(200, json=self.runs[name])
            return httpx.Response(404, json={"error": {"code": "NotFound"}})
        return httpx.Response(404, json={"error": {"code": "ResourceNotFound"}})


class FakeAz:
    """The Azure CLI: `az login --identity` signs in; `az network bastion ssh` runs the host script for real on the fake machine
    of the target resource id. Every call is recorded; refusals can be queued."""

    def __init__(self, root):
        self.root, self.hosts, self.calls, self.refusals = root, {}, [], []

    def host(self, machine_id: str) -> FakeHost:
        if machine_id not in self.hosts:
            self.hosts[machine_id] = FakeHost(self.root / machine_id.rsplit("/", 1)[-1])
        return self.hosts[machine_id]

    def __call__(self, argv, stdin=None, env=None, timeout=120):
        self.calls.append({"argv": list(argv), "env": dict(env or {}), "stdin": stdin})
        if argv[:2] == ["az", "login"]:
            return Completed(0, "", "")
        assert argv[:4] == ["az", "network", "bastion", "ssh"], argv
        if self.refusals:
            return self.refusals.pop(0)
        machine_id = argv[argv.index("--target-resource-id") + 1]
        done = self.host(machine_id).run("deploy", {}, raw=stdin)
        return Completed(done.returncode, done.stdout, done.stderr)


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()
