"""A fake Azure for the unit tests: Resource Manager, Microsoft Graph and a blob service, answered from memory, with
planted failures. It grows with each kind of the adapter (docs/research/RESEARCH-2026-11-03-azure.md, section 10).

No test here touches a cloud. The fake replaces AzureApi.call, so the adapter's addresses, bodies, paging and waits run
as in production, and `calls` records every one for assertions. Resource Manager is modelled as Azure documents it:
every resource is a PUT of the id the caller chose, read back with its provisioningState; a machine's PUT makes its
interface, public address and OS disk with it, and its delete takes those whose deleteOption is Delete; a group's delete
takes everything in it.
"""
import base64
import copy
import ipaddress
import itertools
import re
import uuid
from urllib.parse import parse_qsl, unquote

import yaml
from gcp_fakes import config_dict

from cloudinfra.config import Config
from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.orchestration.provisioner import InlineExecutor, Orchestrator
from cloudinfra.providers import ProviderRegistry
from cloudinfra.providers.azure.api import API_VERSIONS, AzureApi, AzureResponse, translate_error
from cloudinfra.providers.azure.provider import AzureProvider
from cloudinfra.state.state_manager import StateManager

SUBSCRIPTION = "00000000-0000-4000-8000-000000000001"
TENANT = "00000000-0000-4000-8000-0000000000aa"
ARM = "https://management.azure.com"
GRAPH = "https://graph.microsoft.com"
HUB = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/hub/providers/Microsoft.Network"
BASTION = f"{HUB}/bastionHosts/hub-bastion"
DEFAULT_SUBNET = f"{HUB}/virtualNetworks/hub/subnets/workloads"
REGISTRY = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/hub/providers/Microsoft.ContainerRegistry/registries/platformimages"
VAULT_AUDIENCE = "https://vault.azure.net"
REGISTRY_LOGIN = "platformimages-h7c2.azurecr.io"  # a login server with a label Azure added: more than the registry's name
# people and groups in the fake directory: address -> object id
USERS = {"ada@example.com": "11111111-1111-4111-8111-111111111111", "grace@example.com": "22222222-2222-4222-8222-222222222222"}
GUESTS = {"linus@partner.example.com": "33333333-3333-4333-8333-333333333333"}  # a guest: their address is their mail, not their sign-in name
GROUPS = {"data-team@example.com": "44444444-4444-4444-8444-444444444444"}
AZURE_VM_REQUEST = {"provider": "azure", "region": "eastus2", "network": {}, "compute": {"cpu": 2, "memory_gb": 8}}
AZURE_FULL_REQUEST = {"provider": "azure", "region": "eastus2", "name": "analytics", "labels": {"team": "data"},
                      "network": {"public_access": True, "allowed_ports": [443], "allowed_sources": ["203.0.113.0/24"]},
                      "compute": {"count": 2, "cpu": 4, "memory_gb": 16, "os": "ubuntu", "public_ip": True},
                      "storage": {"type": "persistent", "size_gb": 100, "performance": "ssd"}}


def azure_settings(**overrides):
    return {"subscription_id": SUBSCRIPTION, "allowed_regions": ["eastus2", "westus"], **overrides}


def make_azure_config(tmp_path=None, gcp=False, azure=None, **overrides):
    """The tests' configuration with an Azure block (and Google's beside it when gcp is true)."""
    providers = {"azure": azure_settings(**(azure or {}))}
    if gcp:
        providers["gcp"] = config_dict()["providers"]["gcp"]
    data = config_dict(providers=providers, **overrides)
    if tmp_path is not None:
        data["database"] = str(tmp_path / "cloudinfra.db")
    return Config.model_validate(data)


def usage(name, current, limit):
    return {"name": {"value": name, "localizedValue": name}, "currentValue": current, "limit": limit, "unit": "Count"}


def refusal(status, code, message="refused by the fake"):
    """An Azure refusal as the client turns it into a structured error."""
    return translate_error(status, {"error": {"code": code, "message": message}})


class FakeAzureApi(AzureApi):
    """Azure's REST surface, in memory. `fail_on` holds (method, url fragment) pairs that raise a provider error once
    reached; `refuse` maps (method, url fragment) to Azure refusals handed out one per call, in order."""

    def __init__(self):
        self.now = 0.0  # the fake's own clock: a sleep moves it on, so a wait that never ends runs out instead of hanging a test
        # a token says which audience it was asked for, so a test sees which one went where
        super().__init__(SUBSCRIPTION, token=lambda audience=None: "fake-token" if audience in (None, f"{ARM}/") else f"fake-token:{audience}",
                         sleep=self._sleep, clock=lambda: self.now)
        self.calls = []  # (method, url, body, query parameters)
        self.audiences = []  # (url, audience) of calls made with a token for another audience than Resource Manager's
        self.fail_on = []
        self.refuse: dict[tuple[str, str], list[InfrastructureError]] = {}
        self.page_size = 2
        self.usages = {"eastus2": [usage("availabilitySets", 0, 2500), usage("cores", 6, 20), usage("virtualMachines", 3, 25000),
                                   usage("standardDSv5Family", 6, 50), usage("standardNCADSA100v4Family", 0, 0), usage("PremiumDiskCount", 1, 1000)]}
        self.network_usages = {"eastus2": [usage("VirtualNetworks", 1, 1000), usage("StandardSkuPublicIpAddresses", 2, 1000)]}
        self.resources: dict[str, dict] = {}  # lowercase id -> the resource as Azure answers it
        self.blobs: dict[str, list[str]] = {}  # storage account name -> blob names in its container
        self.cpu_percent: dict[str, float] = {}  # lowercase machine id -> its Percentage CPU samples' value
        self.graph_denied = False
        self.order: list[tuple[str, str]] = []  # (method, id) of every write that reached the fake, in order
        self.addresses = itertools.count(4)  # private addresses handed out on a subnet: 10.x.y.4, .5…
        self.public = itertools.count(10)
        self.ticks = itertools.count(1)
        self.vault_data: dict[str, dict[str, list[dict]]] = {}  # vault name -> secret name -> its versions ({value, enabled}), on the vault's own API
        self.deleted_secrets: dict[str, set[str]] = {}  # vault name -> secrets deleted and not purged
        self.deleted_vaults: set[str] = set()  # vault names deleted and not purged
        self.repositories: set[str] = set()  # repositories in the operator's registry
        self.registry_tokens: list[tuple[str, str]] = []  # (registry API path, bearer) of every call to the registry's own API
        self.passwords: dict[str, str] = {}  # flexible server name -> the administrator password it was made with (never answered back)
        self.catalog_page = 2  # the registry answers its catalog this many names at a time, with a Link to the next page
        # step 5: what is inside each cluster's own API (endpoint -> namespace -> labels; (endpoint, path) -> object), and its planted failure
        self.kube_namespaces: dict[str, dict[str, dict]] = {}
        self.kube_objects: dict[tuple[str, str], dict] = {}
        self.kube_fail = None
        self.role_lag = 0  # AKS refuses this many cluster creates as LinkedAuthorizationFailed, as while a role on the subnet is not yet seen

    def _sleep(self, seconds):
        self.now += seconds

    # ---- one call
    def call(self, method, url, json=None, params=None, audience=None, headers=None, data=None, bearer=None):
        path, _, query = url.partition("?")
        params = {**dict(parse_qsl(query)), **dict(params or {})}
        self.calls.append((method, url, json, params))
        if audience is not None:
            self.audiences.append((url, audience))
        for fail_method, fragment in self.fail_on:
            if method == fail_method and fragment in url:
                raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"planted failure on {method} {fragment}", {"http_status": 400, "planted": True})
        for (refuse_method, fragment), errors in self.refuse.items():  # a fragment ending in $ matches the end of the address only
            matched = path.endswith(fragment[:-1]) if fragment.endswith("$") else fragment in url
            if method == refuse_method and matched and errors:
                raise errors.pop(0)
        if path.startswith(GRAPH):
            return self._graph(method, unquote(path[len(GRAPH):]), params)
        found = re.match(r"^https://([a-z0-9]{3,24})\.blob\.core\.windows\.net/data$", path)
        if found:
            return self._blob_service(found.group(1), method, params, headers or {})
        found = re.match(r"^https://([a-z0-9-]{3,24})\.vault\.azure\.net(/.*)$", path)
        if found:
            assert audience == VAULT_AUDIENCE and params.get("api-version"), (url, audience)
            return self._vault_service(found.group(1), method, found.group(2), json)
        found = re.match(r"^https://([a-z0-9-]{5,60})\.azurecr\.io(/.*)$", path)
        if found:
            return self._registry_service(found.group(1), method, found.group(2), params, data, bearer)
        assert path.startswith(ARM), f"the adapter called outside Resource Manager: {url}"
        path = path[len(ARM):]
        for pattern, handler in ((r"^/subscriptions/([^/]+)/providers/Microsoft\.Compute/locations/([^/]+)/usages$", self._usages),
                                 (r"^/subscriptions/([^/]+)/providers/Microsoft\.Network/locations/([^/]+)/usages$", self._network_usages)):
            matched = re.match(pattern, path)
            if matched:
                return handler(matched, method, json or {}, params)
        assert path.startswith(f"/subscriptions/{SUBSCRIPTION}"), path
        assert params.get("api-version"), f"no api-version on {method} {path}"
        return self._arm(method, path, json, params)

    # ---- Resource Manager
    def _arm(self, method, path, body, params):
        key = path.lower()
        if method == "GET":
            return self._get(path, key, params)
        if method == "PUT":
            self.order.append(("PUT", path))
            return self._put(path, key, copy.deepcopy(body) or {}, params)
        if method == "PATCH":
            self.order.append(("PATCH", path))
            return self._patch(path, key, body or {})
        if method == "DELETE":
            self.order.append(("DELETE", path))
            return self._delete(path, key)
        if method == "POST":
            self.order.append(("POST", path))
            return self._post(path, key)
        raise AssertionError(f"the fake has no {method}")

    def _type(self, path):
        """The resource type of an id: Microsoft.Compute/virtualMachines, Microsoft.Compute/virtualMachines/extensions…"""
        parts = path.split("/")
        if "providers" not in parts:
            return "Microsoft.Resources/resourceGroups" if len(parts) == 5 else "Microsoft.Resources/subscriptions"
        at = len(parts) - 1 - parts[::-1].index("providers")
        rest = parts[at + 1:]
        return "/".join([rest[0], *rest[1::2]])

    def _expected_version(self, kind):
        namespace = kind.split("/")[0]
        if kind == "Microsoft.Compute/disks":
            return API_VERSIONS["Microsoft.Compute/disks"]
        if kind.startswith("Microsoft.Network/privateDnsZones"):
            return API_VERSIONS["Microsoft.Network/privateDnsZones"]
        if kind == "Microsoft.Authorization/locks":
            return API_VERSIONS["Microsoft.Authorization/locks"]
        return API_VERSIONS.get(namespace)

    def _get(self, path, key, params):
        if key == f"/subscriptions/{SUBSCRIPTION}":
            return AzureResponse(200, {}, {"id": path, "subscriptionId": SUBSCRIPTION, "tenantId": TENANT})
        listing = self._list(path, key, params)
        if listing is not None:
            return listing
        found = self.resources.get(key)
        if found is None:
            raise refusal(404, "ResourceNotFound", f"{path} was not found")
        assert params["api-version"] == self._expected_version(found["type"]), (path, params["api-version"])
        answer = copy.deepcopy(found)
        if found["type"] == "Microsoft.Network/networkSecurityGroups":
            answer["properties"]["securityRules"] = self._nsg_rules(key)
        if found["type"] == "Microsoft.Compute/virtualMachines" and params.get("$expand") == "instanceView":
            answer["properties"]["instanceView"] = {"statuses": [{"code": "ProvisioningState/succeeded", "time": found["_started"]},
                                                                 {"code": f"PowerState/{found['_power']}"}]}
        return AzureResponse(200, {}, self._public(answer))

    @staticmethod
    def _public(resource):
        return {name: value for name, value in resource.items() if not name.startswith("_")}

    def _list(self, path, key, params):
        """The collections the adapter reads; None for a single resource."""
        parent = key.rsplit("/", 1)[0]
        if key not in self.resources and parent in self.resources and not key.endswith(("/vmsizes", "/resources")):
            children = [self._public(item) for name, item in self.resources.items() if name.startswith(key + "/") and name.count("/") == key.count("/") + 1]
            return AzureResponse(200, {}, {"value": children})  # a resource's child collection: its zone's links, a vault's secrets
        typed = re.match(r"^(/subscriptions/[^/]+/resourcegroups/[^/]+)/providers/([^/]+/[^/]+)$", key)
        if typed and key not in self.resources:  # the resources of one type in a group: a group's identities
            group, kind = typed.groups()
            if group not in self.resources:
                raise refusal(404, "ResourceGroupNotFound", f"{path} was not found")
            return AzureResponse(200, {}, {"value": [self._public(item) for name, item in self.resources.items()
                                                     if name.startswith(group + "/providers/") and item["type"].lower() == kind]})
        if re.match(r"^/subscriptions/[^/]+/resourcegroups/[^/]+/resources$", key):
            group = key[: -len("/resources")]
            if group not in self.resources:
                raise refusal(404, "ResourceGroupNotFound", f"{path} was not found")
            items = [self._public(item) for name, item in self.resources.items() if name.startswith(group + "/providers/") and item["_top"]]
            return AzureResponse(200, {}, {"value": items})
        for kind in ("Microsoft.Compute/virtualMachines", "Microsoft.Network/networkInterfaces", "Microsoft.Network/publicIPAddresses"):
            if key == f"/subscriptions/{SUBSCRIPTION}/providers/{kind}".lower():
                return AzureResponse(200, {}, {"value": [self._public(item) for item in self.resources.values() if item["type"] == kind]})
        if key.endswith("/vmsizes"):
            machine = self.resources[key[: -len("/vmsizes")]]
            family = machine["properties"]["hardwareProfile"]["vmSize"].split("_")[1][0]  # the same letter: the same hardware, in this fake
            return AzureResponse(200, {}, {"value": [{"name": name} for name in SIZES_BY_LETTER.get(family, [])]})
        if key.endswith("/providers/microsoft.insights/metrics"):
            machine = key[: -len("/providers/microsoft.insights/metrics")]
            assert params["api-version"] == API_VERSIONS["Microsoft.Insights"] and params["metricnames"] == "Percentage CPU" and params["interval"] == "PT1M"
            value = self.cpu_percent.get(machine)
            aggregation = params["aggregation"].lower()
            data = [{"timeStamp": "2026-11-04T10:00:00Z", aggregation: value}, {"timeStamp": "2026-11-04T10:01:00Z", aggregation: value / 2}] if value is not None else []
            return AzureResponse(200, {}, {"value": [{"name": {"value": "Percentage CPU"}, "timeseries": [{"data": data}] if data else [], "errorCode": "Success"}]})
        return None

    def _put(self, path, key, body, params):
        kind = self._type(path)
        assert params["api-version"] == self._expected_version(kind), (path, params["api-version"])
        if kind != "Microsoft.Resources/resourceGroups":
            group = "/".join(key.split("/")[:5])
            if group not in self.resources:
                raise refusal(404, "ResourceGroupNotFound", f"Resource group '{path.split('/')[4]}' could not be found.")
        name = path.rsplit("/", 1)[-1]
        existing = self.resources.get(key)
        properties = {**((existing or {}).get("properties") or {}), **(body.get("properties") or {}), "provisioningState": "Succeeded"}
        resource = {**(existing or {}), **body, "id": path, "name": name, "type": kind, "properties": properties, "_top": kind.count("/") == 1}
        if kind == "Microsoft.Authorization/roleAssignments":
            resource["properties"]["scope"] = path.split("/providers/Microsoft.Authorization/")[0]
            # Azure keeps one assignment of a role per member and scope, whatever its condition: another name for the same is
            # refused, and a PUT of the same name changes its condition (the ACR ABAC page's own advice on prefixes)
            twin = next((item for item in self.resources.values() if item["type"] == kind and item["id"].lower() != key
                         and (item["properties"]["scope"].lower(), item["properties"]["principalId"], item["properties"]["roleDefinitionId"])
                         == (resource["properties"]["scope"].lower(), properties["principalId"], properties["roleDefinitionId"])), None)
            if twin is not None:
                raise refusal(409, "RoleAssignmentExists", "The role assignment already exists.")
            resource["_top"] = False
        if kind == "Microsoft.ManagedIdentity/userAssignedIdentities" and existing is None:
            properties.update(principalId=str(uuid.uuid4()), clientId=str(uuid.uuid4()), tenantId=TENANT)
        if kind == "Microsoft.Network/publicIPAddresses" and not properties.get("ipAddress"):
            properties["ipAddress"] = f"20.1.2.{next(self.public)}"
        if kind == "Microsoft.Compute/virtualMachines":
            self._machine(resource, existing)
        if kind in ("Microsoft.DBforPostgreSQL/flexibleServers", "Microsoft.DBforMySQL/flexibleServers"):
            self.passwords[name] = properties.pop("administratorLoginPassword", "")  # never answered back, as Azure does
            engine = "postgres" if "PostgreSQL" in kind else "mysql"
            properties.update(state="Ready", fullyQualifiedDomainName=f"{name}.{engine}.database.azure.com")
        if kind == "Microsoft.Cache/redisEnterprise":
            properties.update(resourceState="Running", hostName=f"{name}.{body.get('location', 'eastus2')}.redis.azure.net")
        if kind == "Microsoft.KeyVault/vaults":
            if name in self.deleted_vaults:
                raise refusal(409, "ConflictError", f"A vault with the same name already exists in deleted state: {name}")
            properties["vaultUri"] = f"https://{name}.vault.azure.net/"
            self.vault_data.setdefault(name, {})
        if kind == "Microsoft.Network/networkSecurityGroups/securityRules":
            nsg = key.rsplit("/securityrules/", 1)[0]
            clash = [rule for rule in self._nsg_rules(nsg) if rule["name"].lower() != name.lower()
                     and (rule["properties"]["priority"], rule["properties"]["direction"]) == (properties["priority"], properties["direction"])]
            if clash:
                raise refusal(400, "SecurityRuleConflict", f"Security rule {name} has the same priority as {clash[0]['name']}")
        if kind in STEP5_PUTS:
            getattr(self, STEP5_PUTS[kind])(path, key, name, resource, body, existing)
        if kind == "Microsoft.Network/privateEndpoints":
            target = properties["privateLinkServiceConnections"][0]["properties"]["privateLinkServiceId"]
            assert target.lower() in self.resources, f"an endpoint for {target}, which does not exist"
            resource["_delete_with"] = target.lower()
        self.resources[key] = resource
        return AzureResponse(200 if existing else 201 if kind == "Microsoft.Authorization/roleAssignments" else 200, {}, self._public(resource))

    # ---- step 5's resources, each checked as Azure checks it
    def _nsg_rules(self, nsg_key):
        nsg = self.resources[nsg_key]
        rules = {rule["name"].lower(): rule for rule in nsg["properties"].get("securityRules") or []}
        for name, item in self.resources.items():
            if item["type"] == "Microsoft.Network/networkSecurityGroups/securityRules" and name.startswith(nsg_key + "/securityrules/"):
                rules[item["name"].lower()] = {"name": item["name"], "properties": item["properties"]}
        return list(rules.values())

    def _subnet(self, subnet_id):
        """(the virtual network, the subnet) of a subnet id, or None for one outside the fake (the operator's)."""
        network = self.resources.get(subnet_id.lower().split("/subnets/")[0])
        if network is None:
            return None
        name = subnet_id.rsplit("/", 1)[-1]
        found = next((item for item in network["properties"]["subnets"] if item["name"] == name), None)
        if found is None:
            raise refusal(400, "InvalidResourceReference", f"Subnet {subnet_id} was not found")
        return network, found

    def _has_role(self, scope, principal, role):
        return any(item["type"] == "Microsoft.Authorization/roleAssignments" and item["properties"]["scope"].lower() == scope.lower()
                   and item["properties"]["principalId"] == principal and item["properties"]["roleDefinitionId"].endswith(role)
                   for item in self.resources.values())

    def _cluster(self, path, key, name, resource, body, existing):
        properties = resource["properties"]
        identities = list((body.get("identity") or {}).get("userAssignedIdentities") or {})
        assert len(identities) == 1, "one control plane identity"
        control = self.resources.get(identities[0].lower())
        if control is None:
            raise refusal(400, "InvalidParameter", f"The user-assigned identity {identities[0]} was not found")
        pool = properties["agentPoolProfiles"][0]
        assert pool["mode"] == "System" and pool["count"] >= 2 and not pool["vmSize"].startswith("Standard_B"), pool
        if self.role_lag:
            self.role_lag -= 1
            raise refusal(400, "LinkedAuthorizationFailed", "The client has permission to perform action on scope, however the linked subscription was not found")
        if not self._has_role(pool["vnetSubnetID"], control["properties"]["principalId"], "4d97b98b-1d4f-4787-a291-c67834d212e7"):
            raise refusal(400, "LinkedAuthorizationFailed", "The cluster identity does not have authorization to perform action "
                          "'Microsoft.Network/virtualNetworks/subnets/join/action' on the subnet")
        _, subnet = self._subnet(pool["vnetSubnetID"])
        if properties["networkProfile"]["outboundType"] == "userAssignedNATGateway" and not subnet["properties"].get("natGateway"):
            raise refusal(400, "InvalidParameter", "outboundType userAssignedNATGateway needs a NAT gateway on the subnet")
        if existing is not None:
            return
        location = body["location"]
        properties.update(fqdn=f"{properties['dnsPrefix']}-k8s4dnz1.hcp.{location}.azmk8s.io", currentKubernetesVersion=properties.get("kubernetesVersion") or "1.33.3",
                          powerState={"code": "Running"},
                          oidcIssuerProfile={**properties["oidcIssuerProfile"], "issuerURL": f"https://{location}.oic.prod-aks.azure.com/{TENANT}/{uuid.uuid4()}/"},
                          identityProfile={"kubeletidentity": {"objectId": str(uuid.uuid4()), "clientId": str(uuid.uuid4()), "resourceId":
                                           f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{properties['nodeResourceGroup']}/providers/"
                                           f"Microsoft.ManagedIdentity/userAssignedIdentities/{name}-agentpool"}})

    def _environment(self, path, key, name, resource, body, existing):
        subnet_id = resource["properties"]["vnetConfiguration"]["infrastructureSubnetId"]
        _, subnet = self._subnet(subnet_id)
        if ipaddress.ip_network(subnet["properties"]["addressPrefix"]).prefixlen > 27:
            raise refusal(400, "ManagedEnvironmentSubnetTooSmall", "A workload profiles environment's subnet is a /27 or larger")
        if [item["properties"]["serviceName"] for item in subnet["properties"].get("delegations") or []] != ["Microsoft.App/environments"]:
            raise refusal(400, "ManagedEnvironmentSubnetDelegationError", "The subnet must be delegated to Microsoft.App/environments")
        if any(item["type"] == "Microsoft.App/managedEnvironments" and item["id"].lower() != key
               and item["properties"]["vnetConfiguration"]["infrastructureSubnetId"].lower() == subnet_id.lower() for item in self.resources.values()):
            raise refusal(400, "ManagedEnvironmentSubnetIsInUse", "The subnet holds another environment")
        resource["properties"].setdefault("defaultDomain", f"{name}-x7k2.{body['location']}.azurecontainerapps.io")
        resource["properties"].setdefault("staticIp", "10.10.0.140")

    def _in_environment(self, resource, body):
        environment = resource["properties"]["environmentId"]
        if environment.lower() not in self.resources:
            raise refusal(400, "ManagedEnvironmentNotFound", f"Environment {environment} was not found")
        for identity in (body.get("identity") or {}).get("userAssignedIdentities") or {}:
            if identity.lower() not in self.resources:
                raise refusal(400, "InvalidParameter", f"The user-assigned identity {identity} was not found")
        return self.resources[environment.lower()]

    def _container_app(self, path, key, name, resource, body, existing):
        environment = self._in_environment(resource, body)
        properties = resource["properties"]
        ingress = properties["configuration"].get("ingress")
        if ingress is not None:
            ingress["fqdn"] = f"{name}.{environment['properties']['defaultDomain']}"
        revision = f"{name}--{next(self.ticks):07d}"
        properties.update(latestRevisionName=revision, latestReadyRevisionName=revision, runningStatus="Running")

    def _job(self, path, key, name, resource, body, existing):
        self._in_environment(resource, body)

    def _auth_config(self, path, key, name, resource, body, existing):
        assert name == "current", name
        if key.rsplit("/authconfigs/", 1)[0] not in self.resources:
            raise refusal(404, "ContainerAppNotFound", f"{path} has no app")

    def _federated(self, path, key, name, resource, body, existing):
        identity = key.rsplit("/federatedidentitycredentials/", 1)[0]
        if identity not in self.resources:
            raise refusal(404, "ResourceNotFound", f"The identity of {path} was not found")
        others = [item for item_key, item in self.resources.items() if item_key.startswith(identity + "/federatedidentitycredentials/") and item_key != key]
        if len(others) >= 20:
            raise refusal(400, "BadRequest", "An identity holds at most 20 federated identity credentials")
        pair = (resource["properties"]["issuer"], resource["properties"]["subject"])
        if any((item["properties"]["issuer"], item["properties"]["subject"]) == pair for item in others):
            raise refusal(409, "Conflict", "A federated identity credential with this issuer and subject already exists")
        assert resource["properties"]["audiences"] == ["api://AzureADTokenExchange"]
        resource["_top"] = False

    def _load_balancer(self, path, key, name, resource, body, existing):
        properties = resource["properties"]
        address = properties["frontendIPConfigurations"][0]["properties"]["publicIPAddress"]["id"]
        if address.lower() not in self.resources:
            raise refusal(400, "InvalidResourceReference", f"Public address {address} was not found")
        for member in properties["backendAddressPools"][0]["properties"]["loadBalancerBackendAddresses"]:
            network = member["properties"]["virtualNetwork"]["id"]
            if network.startswith(HUB):
                continue
            found = self.resources.get(network.lower())
            if found is None:
                raise refusal(400, "InvalidResourceReference", f"Virtual network {network} was not found")
            address = ipaddress.ip_address(member["properties"]["ipAddress"])
            if not any(address in ipaddress.ip_network(prefix) for prefix in found["properties"]["addressSpace"]["addressPrefixes"]):
                raise refusal(400, "InvalidIPAddressInBackendAddressPool", f"{address} is not in virtual network {found['name']}")

    def _cdn(self, path, key, name, resource, body, existing):
        kind = resource["type"]
        profile = key.split("/providers/microsoft.cdn/profiles/", 1)[1].split("/", 1)[0]
        if kind == "Microsoft.Cdn/profiles":
            assert body["location"] == "global" and body["sku"]["name"] == "Standard_AzureFrontDoor", body
            resource["properties"].setdefault("frontDoorId", str(uuid.uuid4()))
            return
        resource["_top"] = False
        parent = key.rsplit("/", 2)[0]
        if parent not in self.resources:
            raise refusal(404, "ParentResourceNotFound", f"The parent of {path} was not found")
        properties = resource["properties"]
        if kind == "Microsoft.Cdn/profiles/afdEndpoints":
            properties.setdefault("hostName", f"{name}-g7d2h4b8c6e0.z01.azurefd.net")
        elif kind == "Microsoft.Cdn/profiles/customDomains":
            properties.setdefault("validationProperties", {"validationToken": f"token-for-{name}-in-{profile}", "expirationDate": "2026-11-12T00:00:00Z"})
            properties.setdefault("domainValidationState", "Pending")
        elif kind == "Microsoft.Cdn/profiles/afdEndpoints/routes":
            for reference in [item["id"] for item in properties["customDomains"]] + [properties["originGroup"]["id"]]:
                if reference.lower() not in self.resources:
                    raise refusal(400, "BadRequest", f"{reference} was not found")

    def _machine(self, resource, existing):
        """A machine's PUT makes its interface, its public address and its OS disk, as Azure does."""
        properties = resource["properties"]
        if existing is not None:
            return
        properties["vmId"] = str(uuid.uuid4())
        properties["timeCreated"] = "2026-11-04T09:00:00Z"
        resource["_power"], resource["_started"] = "running", f"2026-11-04T09:0{next(self.ticks) % 10}:00Z"
        group = "/".join(resource["id"].split("/")[:5])
        properties["networkProfile"]["networkInterfaces"] = []
        for configuration in properties["networkProfile"]["networkInterfaceConfigurations"]:
            ip_configuration = configuration["properties"]["ipConfigurations"][0]["properties"]
            subnet = ip_configuration["subnet"]["id"]
            prefix = self._prefix(subnet)
            # the fake names a machine's parts its own way, so that the adapter must read them from the machine
            nic_id = f"{group}/providers/Microsoft.Network/networkInterfaces/{configuration['name']}-{next(self.ticks):03d}"
            properties["networkProfile"]["networkInterfaces"].append({"id": nic_id, "properties": {"primary": True, "deleteOption": configuration["properties"]["deleteOption"]}})
            nic = {"privateIPAddress": f"{prefix}.{next(self.addresses)}", "subnet": {"id": subnet}}
            if ip_configuration.get("publicIPAddressConfiguration"):
                public = ip_configuration["publicIPAddressConfiguration"]
                ip_name = f"{public['name']}-{next(self.ticks):03d}"
                ip_id = f"{group}/providers/Microsoft.Network/publicIPAddresses/{ip_name}"
                self.resources[ip_id.lower()] = {"id": ip_id, "name": ip_name, "type": "Microsoft.Network/publicIPAddresses", "_top": True,
                                                 "_delete_with": resource["id"].lower() if public["properties"]["deleteOption"] == "Delete" else None,
                                                 "properties": {"ipAddress": f"20.1.2.{next(self.public)}", "provisioningState": "Succeeded"}}
                nic["publicIPAddress"] = {"id": ip_id}
            self.resources[nic_id.lower()] = {"id": nic_id, "name": nic_id.rsplit("/", 1)[-1], "type": "Microsoft.Network/networkInterfaces", "_top": True,
                                              "_delete_with": resource["id"].lower() if configuration["properties"]["deleteOption"] == "Delete" else None,
                                              "properties": {"ipConfigurations": [{"name": "primary", "properties": nic}], "virtualMachine": {"id": resource["id"]},
                                                             "provisioningState": "Succeeded"}}
        disk = properties["storageProfile"]["osDisk"]
        disk_id = f"{group}/providers/Microsoft.Compute/disks/{disk['name']}_{next(self.ticks):03d}"
        disk.setdefault("managedDisk", {})["id"] = disk_id
        self.resources[disk_id.lower()] = {"id": disk_id, "name": disk_id.rsplit("/", 1)[-1], "type": "Microsoft.Compute/disks", "_top": True,
                                           "_delete_with": resource["id"].lower() if disk.get("deleteOption") == "Delete" else None,
                                           "properties": {"diskSizeGB": disk["diskSizeGB"], "provisioningState": "Succeeded"}}

    def _prefix(self, subnet):
        network = self.resources.get(subnet.lower().split("/subnets/")[0])
        if network is None:
            return "10.250.0"  # the operator's subnet, outside the fake
        cidr = network["properties"]["subnets"][0]["properties"]["addressPrefix"]
        return cidr.split("/")[0].rsplit(".", 1)[0]

    def _patch(self, path, key, body):
        found = self.resources.get(key)
        if found is None:
            raise refusal(404, "ResourceNotFound", f"{path} was not found")
        properties = body.get("properties") or {}
        if "hardwareProfile" in properties:
            if found.get("_power") == "running" and properties["hardwareProfile"]["vmSize"] not in self._sizes(found):
                raise refusal(409, "OperationNotAllowed", "The requested VM size is not available in the current hardware cluster; deallocate the VM first")
            found["properties"]["hardwareProfile"] = properties["hardwareProfile"]
        if "storageProfile" in properties:
            found["properties"]["storageProfile"]["dataDisks"] = copy.deepcopy(properties["storageProfile"]["dataDisks"])
            for disk in found["properties"]["storageProfile"]["dataDisks"]:
                attached = self.resources.get(disk["managedDisk"]["id"].lower())
                if attached is None:
                    raise refusal(404, "NotFound", f"Disk {disk['managedDisk']['id']} was not found")
                if attached.get("zones") != found.get("zones"):
                    raise refusal(400, "BadRequest", "The disk is in another zone")
                attached["properties"]["diskState"] = "Attached"
                attached["_attached_to"] = found["id"].lower()
        return AzureResponse(200, {}, self._public(found))

    def _sizes(self, machine):
        letter = machine["properties"]["hardwareProfile"]["vmSize"].split("_")[1][0]
        return SIZES_BY_LETTER.get(letter, [])

    def _post(self, path, key):
        machine_key, _, action = key.rpartition("/")
        if "/deletedvaults/" in key and action == "purge":
            vault = key.split("/deletedvaults/", 1)[1].split("/", 1)[0]
            if vault not in self.deleted_vaults:
                raise refusal(404, "ResourceNotFound", f"Deleted vault {vault} was not found")
            self.deleted_vaults.discard(vault)
            self.vault_data.pop(vault, None)
            self.deleted_secrets.pop(vault, None)
            return AzureResponse(200, {}, None)
        machine = self.resources.get(machine_key)
        if machine is None:
            raise refusal(404, "ResourceNotFound", f"{path} was not found")
        if machine["type"].endswith("/flexibleServers"):
            assert action in ("stop", "start"), action
            machine["properties"]["state"] = "Stopped" if action == "stop" else "Ready"
            return AzureResponse(200, {}, None)
        if machine["type"] == "Microsoft.ContainerService/managedClusters":
            assert action == "listclusterusercredential", action
            config = {"apiVersion": "v1", "kind": "Config", "clusters": [{"name": machine["name"], "cluster": {
                "server": f"https://{machine['properties']['fqdn']}:443", "certificate-authority-data": base64.b64encode(b"-----BEGIN CERTIFICATE-----").decode()}}],
                      "users": [{"name": "clusterUser", "user": {"exec": {"command": "kubelogin", "args": ["get-token", "--server-id", "6dae42f8-4368-4678-94ff-3960e28e3630"]}}}]}
            return AzureResponse(200, {}, {"kubeconfigs": [{"name": "clusterUser", "value": base64.b64encode(yaml.safe_dump(config).encode()).decode()}]})
        if machine["type"] == "Microsoft.Cache/redisEnterprise/databases":
            assert action == "listkeys", action
            return AzureResponse(200, {}, {"primaryKey": f"primary-key-of-{machine_key.split('/')[-3]}", "secondaryKey": "secondary"})
        if action == "deallocate":
            machine["_power"] = "deallocated"
        elif action == "start":
            machine["_power"], machine["_started"] = "running", f"2026-11-04T1{next(self.ticks) % 10}:00:00Z"
        else:
            raise AssertionError(f"the fake has no action {action}")
        return AzureResponse(200, {}, None)

    def _delete(self, path, key):
        scopes = {name: name.split("/providers/microsoft.authorization/locks/", 1)[0] for name, item in self.resources.items()
                  if item["type"] == "Microsoft.Authorization/locks" and name != key}
        # a lock stops the delete of what it is on, of anything under it, and of the group it is in
        locked = [name for name, scope in scopes.items() if (key + "/").startswith(scope + "/") or scope.startswith(key + "/")]
        if locked and key in self.resources:
            raise refusal(409, "ScopeLocked", f"The scope '{path}' cannot perform delete operation because following scope(s) are locked: {locked[0]}")
        found = self.resources.pop(key, None)
        if found is None:
            return AzureResponse(204, {}, None)
        if found["type"] == "Microsoft.KeyVault/vaults":
            self.deleted_vaults.add(found["name"])  # soft delete: kept, with its secrets, until purged
        if found["type"] == "Microsoft.Compute/disks" and found.get("_attached_to") in self.resources:
            self.resources[key] = found
            raise refusal(409, "OperationNotAllowed", f"Disk {found['name']} is attached to a VM")
        if found["type"] == "Microsoft.Resources/resourceGroups":
            for name in [name for name in self.resources if name.startswith(key + "/")]:
                if self.resources[name]["type"] == "Microsoft.KeyVault/vaults":
                    self.deleted_vaults.add(self.resources[name]["name"])
                del self.resources[name]
        if found["type"] in ("Microsoft.Compute/virtualMachines", "Microsoft.DBforPostgreSQL/flexibleServers", "Microsoft.DBforMySQL/flexibleServers",
                             "Microsoft.Cache/redisEnterprise"):
            if found["type"] != "Microsoft.Compute/virtualMachines":
                assert not [item for item in self.resources.values() if item.get("_delete_with") == key], "its private endpoint goes before it"
            for name in [name for name, item in self.resources.items() if item.get("_delete_with") == key]:
                del self.resources[name]
            for item in self.resources.values():
                if item.get("_attached_to") == key:
                    item["_attached_to"] = None
                    item["properties"]["diskState"] = "Unattached"
        if found["type"] == "Microsoft.App/managedEnvironments":
            held = [item["name"] for item in self.resources.values() if item["type"] in ("Microsoft.App/containerApps", "Microsoft.App/jobs")
                    and item["properties"]["environmentId"].lower() == key]
            if held:
                self.resources[key] = found
                raise refusal(409, "ManagedEnvironmentHasApps", f"The environment still holds {held[0]}")
        if found["type"] == "Microsoft.Network/virtualNetworks":
            environments = [item["name"] for item in self.resources.values() if item["type"] == "Microsoft.App/managedEnvironments"
                            and item["properties"]["vnetConfiguration"]["infrastructureSubnetId"].lower().startswith(key + "/subnets/")]
            if environments:
                self.resources[key] = found
                raise refusal(400, "InUseSubnetCannotBeDeleted", f"A subnet is in use by the environment {environments[0]}")
            subnet = found["properties"]["subnets"][0]["name"]
            users = [item for item in self.resources.values() if item["type"] == "Microsoft.Network/networkInterfaces"
                     and str(((item["properties"].get("ipConfigurations") or [{}])[0].get("properties") or {}).get("subnet", {}).get("id", "")).lower()
                     == f"{key}/subnets/{subnet}".lower()]
            if users:
                self.resources[key] = found
                raise refusal(400, "InUseSubnetCannotBeDeleted", f"Subnet {subnet} is in use by {users[0]['id']}")
        for name in [name for name in self.resources if name.startswith(key + "/")]:  # a resource's children go with it
            del self.resources[name]
        return AzureResponse(200, {}, None)

    def _usages(self, found, method, body, params):
        subscription, region = found.groups()
        assert method == "GET" and subscription == SUBSCRIPTION and params["api-version"] == API_VERSIONS["Microsoft.Compute"]
        items = self.usages.get(region, [])
        start = int(params.get("$skiptoken", 0))
        answer = {"value": items[start:start + self.page_size]}
        if start + self.page_size < len(items):
            answer["nextLink"] = (f"{ARM}/subscriptions/{subscription}/providers/Microsoft.Compute/locations/{region}/usages"
                                  f"?api-version={params['api-version']}&$skiptoken={start + self.page_size}")
        return AzureResponse(200, {}, answer)

    def _network_usages(self, found, method, body, params):
        assert method == "GET" and params["api-version"] == API_VERSIONS["Microsoft.Network"]
        return AzureResponse(200, {}, {"value": self.network_usages.get(found.group(2), [])})

    # ---- Microsoft Graph
    def _graph(self, method, path, params):
        assert method == "GET" and path.startswith("/v1.0/")
        if self.graph_denied:
            raise refusal(403, "Authorization_RequestDenied", "Insufficient privileges to complete the operation.")
        found = re.match(r"^/v1\.0/users/(.+)$", path)
        if found:
            address = found.group(1).lower()
            if address not in USERS:
                raise refusal(404, "Request_ResourceNotFound", f"Resource '{address}' does not exist")
            return AzureResponse(200, {}, {"id": USERS[address], "userPrincipalName": address})
        mail = re.fullmatch(r"mail eq '([^']+)'", params.get("$filter", ""))
        assert mail, params
        directory = {**GUESTS} if path == "/v1.0/users" else GROUPS if path == "/v1.0/groups" else None
        assert directory is not None, path
        value = [{"id": directory[mail.group(1).lower()], "mail": mail.group(1)}] if mail.group(1).lower() in directory else []
        return AzureResponse(200, {}, {"value": value})

    # ---- a blob service's own API
    def _blob_service(self, account, method, params, headers):
        assert method == "GET" and params == {"restype": "container", "comp": "list", "maxresults": "1"} and headers.get("x-ms-version")
        if account not in self.blobs:
            raise refusal(404, "ResourceNotFound", "The specified resource does not exist.")
        listed = "".join(f"<Blob><Name>{name}</Name></Blob>" for name in self.blobs[account][:1])
        return AzureResponse(200, {}, None, f'<?xml version="1.0"?><EnumerationResults><Blobs>{listed}</Blobs></EnumerationResults>')

    # ---- a vault's own API
    def _vault_service(self, vault, method, path, body):
        if not any(item["type"] == "Microsoft.KeyVault/vaults" and item["name"] == vault for item in self.resources.values()):
            raise InfrastructureError(ErrorCode.PROVIDER_UNAVAILABLE, "Azure could not be reached (ConnectError)", {"url": path})
        secrets = self.vault_data.setdefault(vault, {})
        found = re.fullmatch(r"/(secrets|deletedsecrets)/([A-Za-z0-9-]+)", path)
        assert found, path
        collection, name = found.groups()
        vault_id = next(item["id"] for item in self.resources.values() if item["type"] == "Microsoft.KeyVault/vaults" and item["name"] == vault)
        arm = f"{vault_id}/secrets/{name}"
        if collection == "secrets" and method == "PUT":
            if name in self.deleted_secrets.get(vault, set()):
                raise refusal(409, "Conflict", f"Secret {name} is currently in a deleted but recoverable state")
            secrets.setdefault(name, []).append({"value": body["value"], "enabled": body["attributes"]["enabled"]})
            self.resources[arm.lower()] = {"id": arm, "name": name, "type": "Microsoft.KeyVault/vaults/secrets", "_top": False,
                                           "properties": {"attributes": body["attributes"], "provisioningState": "Succeeded"}}
            return AzureResponse(200, {}, {"id": f"https://{vault}.vault.azure.net/secrets/{name}/v{len(secrets[name])}", "attributes": body["attributes"]})
        if collection == "secrets" and method == "DELETE":
            if name not in secrets:
                raise refusal(404, "SecretNotFound", f"A secret with (name/id) {name} was not found in this key vault")
            self.deleted_secrets.setdefault(vault, set()).add(name)
            del secrets[name]
            self.resources.pop(arm.lower(), None)
            return AzureResponse(200, {}, {"recoveryId": f"https://{vault}.vault.azure.net/deletedsecrets/{name}"})
        if collection == "deletedsecrets" and method == "DELETE":
            if name not in self.deleted_secrets.get(vault, set()):
                raise refusal(404, "SecretNotFound", f"Deleted secret {name} was not found")
            self.deleted_secrets[vault].discard(name)
            return AzureResponse(204, {}, None)
        raise AssertionError(f"the fake vault has no {method} {path}")

    # ---- a registry's own API
    def _registry_service(self, registry, method, path, params, data, bearer):
        self.registry_tokens.append((path, bearer))
        if path == "/oauth2/exchange":
            assert method == "POST" and bearer == "" and data["grant_type"] == "access_token" and data["tenant"] == TENANT
            assert data["access_token"] == "fake-token:https://containerregistry.azure.net", "the registry's own audience, never Resource Manager's"
            return AzureResponse(200, {}, {"refresh_token": f"refresh-for-{registry}"})
        if path == "/oauth2/token":
            assert method == "POST" and bearer == "" and data["refresh_token"] == f"refresh-for-{registry}"
            return AzureResponse(200, {}, {"access_token": f"access:{data['scope']}"})
        if path == "/v2/_catalog":
            assert method == "GET" and bearer == "access:registry:catalog:*"
            names = sorted(self.repositories)
            if params.get("last"):
                names = [name for name in names if name > params["last"]]
            page = names[: min(int(params.get("n", 100)), self.catalog_page)]  # a registry may answer fewer than asked
            headers = {"link": f'</v2/_catalog?last={page[-1]}&n={params.get("n", 100)}>; rel="next"'} if len(names) > len(page) else {}
            return AzureResponse(200, headers, {"repositories": page})
        found = re.fullmatch(r"/acr/v1/(.+)", path)
        assert found and method == "DELETE", path
        repository = found.group(1)
        assert bearer == f"access:repository:{repository}:delete", bearer
        if repository not in self.repositories:
            raise refusal(404, "NAME_UNKNOWN", "repository name not known to registry")
        self.repositories.discard(repository)
        return AzureResponse(202, {}, None)

    # ---- what the tests look at
    def admits(self, nsg_id, source, destination, port):
        """Whether a security group admits one inbound TCP connection (source: an address or a service tag), as Azure
        evaluates its rules: the inbound rule of lowest priority that matches decides."""
        def matches(prefixes, value):
            address = ipaddress.ip_address(value) if value[0].isdigit() else None
            for prefix in prefixes:
                if prefix in ("*", value):
                    return True
                if prefix == "Internet" and address is not None and address.is_global:
                    return True
                if prefix[0].isdigit() and address is not None and address in ipaddress.ip_network(prefix):
                    return True
            return False
        for rule in sorted(self._nsg_rules(nsg_id.lower()), key=lambda rule: rule["properties"]["priority"]):
            properties = rule["properties"]
            if properties["direction"] != "Inbound" or properties["protocol"] not in ("*", "Tcp"):
                continue
            sources = properties.get("sourceAddressPrefixes") or [properties.get("sourceAddressPrefix")]
            destinations = properties.get("destinationAddressPrefixes") or [properties.get("destinationAddressPrefix")]
            ports = properties.get("destinationPortRanges") or [properties.get("destinationPortRange")]
            if matches(sources, source) and matches(destinations, destination) and ("*" in ports or str(port) in ports):
                return properties["access"] == "Allow"
        return False

    def of_type(self, kind):
        return {item["name"]: item for item in self.resources.values() if item["type"] == kind}

    def machine(self, name):
        return next(item for item in self.resources.values() if item["type"] == "Microsoft.Compute/virtualMachines" and item["name"] == name)

    def assignments(self):
        """(scope's last two segments, principal id, role GUID) of every role assignment, sorted."""
        return sorted(("/".join(item["properties"]["scope"].split("/")[-2:]), item["properties"]["principalId"], item["properties"]["roleDefinitionId"].rsplit("/", 1)[-1])
                      for item in self.resources.values() if item["type"] == "Microsoft.Authorization/roleAssignments")


STEP5_PUTS = {"Microsoft.ContainerService/managedClusters": "_cluster", "Microsoft.App/managedEnvironments": "_environment",
              "Microsoft.App/containerApps": "_container_app", "Microsoft.App/jobs": "_job", "Microsoft.App/containerApps/authConfigs": "_auth_config",
              "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials": "_federated",
              "Microsoft.Network/loadBalancers": "_load_balancer", "Microsoft.Cdn/profiles": "_cdn", "Microsoft.Cdn/profiles/afdEndpoints": "_cdn",
              "Microsoft.Cdn/profiles/originGroups": "_cdn", "Microsoft.Cdn/profiles/originGroups/origins": "_cdn", "Microsoft.Cdn/profiles/customDomains": "_cdn",
              "Microsoft.Cdn/profiles/afdEndpoints/routes": "_cdn", "Microsoft.Cdn/profiles/securityPolicies": "_cdn"}


class FakeAksKube:
    """A cluster's Kubernetes API, as far as the adapter uses it: the namespace and the admission policy, kept on the fake."""

    def __init__(self, fake: FakeAzureApi, endpoint: str, ca_certificate: str):
        assert endpoint.endswith(".azmk8s.io:443") and ca_certificate, "the adapter must know the server and CA from the cluster's kubeconfig"
        self.fake, self.endpoint = fake, endpoint

    def _maybe_fail(self):
        if self.fake.kube_fail is not None:
            error, self.fake.kube_fail = self.fake.kube_fail, None
            raise error

    def ensure_namespace(self, name, labels):
        self._maybe_fail()
        self.fake.kube_namespaces.setdefault(self.endpoint, {})[name] = dict(labels)

    def put(self, collection, body):
        self._maybe_fail()
        self.fake.kube_objects[(self.endpoint, f"{collection}/{body['metadata']['name']}")] = body

    def delete(self, path):
        self._maybe_fail()
        self.fake.kube_objects.pop((self.endpoint, path), None)


# the sizes the fake's hardware offers a running machine: the same family letter
SIZES_BY_LETTER = {"D": ["Standard_D2s_v5", "Standard_D4s_v5", "Standard_D8s_v5", "Standard_D16s_v5", "Standard_D2ls_v5", "Standard_D4ls_v5"],
                   "E": ["Standard_E2s_v5", "Standard_E4s_v5", "Standard_E8s_v5"], "B": ["Standard_B1s", "Standard_B1ms"]}


def make_azure_provider(config, fake=None):
    fake = fake or FakeAzureApi()
    return AzureProvider(config.providers.azure, api=fake, kube=lambda endpoint, ca: FakeAksKube(fake, endpoint, ca)), fake


def make_azure_orchestrator(tmp_path, config=None, fake=None):
    """An orchestrator over a fake Azure (and the fake Google Cloud when the configuration has both)."""
    from gcp_fakes import make_provider
    config = config or make_azure_config(tmp_path)
    provider, fake = make_azure_provider(config, fake)
    adapters = {"azure": provider}
    if config.providers.gcp is not None:
        adapters["gcp"], _ = make_provider(config)
    registry = ProviderRegistry(config, adapters)
    return Orchestrator(config, StateManager(config.database), registry, InlineExecutor(), sleep=lambda seconds: None), fake
