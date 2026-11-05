"""The Azure adapter's platform kinds (docs/research/RESEARCH-2026-11-03-azure.md, section 10, step 5): AKS clusters,
services and jobs on Container Apps in their network's environment, and load balancers, with Front Door for hostnames.
Every test runs against the fake Azure in azure_fakes.py."""
import base64
import datetime
import re

import pytest
from azure_fakes import AKS_SERVER, REGISTRY, REGISTRY_LOGIN, USERS, make_azure_config, make_azure_orchestrator, make_azure_provider, refusal
from gcp_fakes import principal
from test_infra_azure_services import operator_registry, plan_of, provision, puts

from cloudinfra.models import InfrastructureRequest, RequestStatus
from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.orchestration import planner
from cloudinfra.providers.azure.access import (
    AKS_RBAC_READER,
    AKS_RBAC_WRITER,
    APPS_OPERATOR,
    CLUSTER_USER,
    JOBS_CONTRIBUTOR,
    JOBS_READER,
    NETWORK_CONTRIBUTOR,
    REPOSITORY_READER,
    SECRETS_USER,
    prefixes_of,
)
from cloudinfra.providers.azure.apps import NOBODY
from cloudinfra.providers.azure.cluster import AzureCluster

CLIENT = "99999999-9999-4999-8999-999999999999"  # the operator's app registration for services' sign-in
WAF = "/subscriptions/00000000-0000-4000-8000-000000000001/resourceGroups/hub/providers/Microsoft.Network/FrontDoorWebApplicationFirewallPolicies/edge"
ADA, GRACE = USERS["ada@example.com"], USERS["grace@example.com"]
MEMBERS = [{"member": "user:ada@example.com", "level": "use"}, {"member": "user:grace@example.com", "level": "admin"}]


def request(**blocks) -> dict:
    return {"provider": "azure", "region": "eastus2", **blocks}


def roles_at(fake, scope_end: str) -> set[tuple[str, str]]:
    """(principal, role GUID) of every assignment whose scope ends so."""
    return {(item["properties"]["principalId"], item["properties"]["roleDefinitionId"].rsplit("/", 1)[-1]) for item in fake.resources.values()
            if item["type"] == "Microsoft.Authorization/roleAssignments" and item["properties"]["scope"].lower().endswith(scope_end.lower())}


def of_kind(fake, kind: str) -> list[dict]:
    return [item for item in fake.resources.values() if item["type"] == kind]


def only_the_hub_left(fake) -> bool:
    return all("/resourcegroups/hub" in name for name in fake.resources)


# ---- what validation refuses before any call

def test_what_azure_cannot_give_a_cluster_service_job_or_load_balancer_is_refused_with_the_reason():
    provider, fake = make_azure_provider(make_azure_config(azure={"cluster_registries": [f"{REGISTRY_LOGIN}/base"], "registry": REGISTRY}))

    def fields(**blocks):
        return [item.field for item in provider.validate(InfrastructureRequest.model_validate(request(**blocks)))]
    assert fields(network={}, cluster={}) == [] and fields(network={}, cluster={"registries": [f"{REGISTRY_LOGIN}/base"]}) == []
    assert fields(cluster={}) == ["cluster.network_id"]
    assert fields(network={}, cluster={"public": {}}) == ["cluster.public"]
    assert fields(network={"nat": False}, cluster={}) == ["network.nat"]
    assert fields(network={}, cluster={"registries": [f"{REGISTRY_LOGIN}/other"]}) == ["cluster.registries"]
    assert fields(network={}, cluster={"registries": ["us-docker.pkg.dev/proj-1234/apps"]}) == ["cluster.registries"], "Google's form is not Azure's"
    assert fields(network={}, cluster={"kubernetes_version": "1.31.2-gke.1"}) == ["cluster.kubernetes_version"]
    assert fields(network={}, cluster={"node_cpu": 8, "node_memory_gb": 20}) == ["cluster.node_cpu"]
    assert "preview" in provider.validate(InfrastructureRequest.model_validate(request(network={}, cluster={"public": {}})))[0].message
    with_sign_in, _ = make_azure_provider(make_azure_config(azure={"service_auth_client_id": CLIENT}))

    def app_fields(provider=with_sign_in, **blocks):
        return [item.field for item in provider.validate(InfrastructureRequest.model_validate(request(**blocks)))]
    assert app_fields(network={}, service={}) == [] and app_fields(network={}, job={"schedule": {"cron": "0 2 * * *"}}) == []
    assert app_fields(network={}, service={"cpu": 8}) == ["service.cpu"]
    assert app_fields(network={}, service={"memory_mb": 3000}) == ["service.memory_mb"], "1 vCPU comes with 2048 MiB on Container Apps"
    assert app_fields(network={}, service={"timeout_seconds": 300}) == ["service.timeout_seconds"], "asked for, so refused, not lowered"
    assert app_fields(network={}, service={"ingress": "all"}) == ["service.ingress"]
    assert app_fields(network={}, service={"public": True}) == ["service.public"], "the operator allows no public services"
    assert app_fields(provider=provider, network={}, service={}) == ["service.public"], "without an app registration nothing checks a caller"
    assert app_fields(service={}) == ["service.network_id"]
    assert app_fields(name="averyverylongrequest", network={}, service={"name": "frontend-api"}) == ["service.name"]
    assert app_fields(network={"cidr": "10.10.0.0/27"}, service={}) == ["network.cidr"]
    assert app_fields(network={}, job={"schedule": {"cron": "0 2 * * *", "time_zone": "Europe/Berlin"}}) == ["job.schedule.time_zone"]
    assert app_fields(network={}, job={"tasks": 4, "parallelism": 2}) == ["job.parallelism"]
    denying, _ = make_azure_provider(make_azure_config(azure={"service_auth_client_id": CLIENT, "deny_egress": True}))
    assert app_fields(provider=denying, network={}, job={}) == ["network"]
    domains, _ = make_azure_provider(make_azure_config(azure={"public_domains": ["example.com"]}))
    balancer = {"network": {}, "compute": {"cpu": 2, "memory_gb": 8}}
    assert app_fields(provider=domains, **balancer, load_balancer={"hostnames": ["shop.example.com"]}) == []
    assert app_fields(provider=domains, **balancer, load_balancer={"hostnames": ["shop.example.org"]}) == ["load_balancer.hostnames"]
    assert fake.calls == []


def test_the_plan_raises_aks_floors_and_says_what_container_apps_gives(tmp_path):
    config = make_azure_config(azure={"service_auth_client_id": CLIENT})
    provider, fake = make_azure_provider(config)
    body = request(name="shop", network={}, identity={"name": "worker"}, cluster={}, service={}, job={"tasks": 3}, compute={"cpu": 2, "memory_gb": 8},
                   load_balancer={})
    planned = plan_of(provider, config, body)
    by_kind = {item.kind: item for item in planned}
    cluster = by_kind["cluster"].spec
    assert (cluster["machine_type"], cluster["node_count"], cluster["cpu_quota"]) == ("Standard_D4s_v5", 2, "standardDSv5Family"), "2 vCPUs, 8 GB: the D family's 4"
    assert cluster["identities"] == [{"name": "shop-worker", "account": "worker"}] and by_kind["identity"].spec["kubernetes"] is True
    assert (by_kind["network"].spec["subnet_cidr"], by_kind["network"].spec["apps_subnet"]) == ("10.10.0.0/25", "10.10.0.128/27")
    assert by_kind["service"].spec["memory_mb"] == 2048 and by_kind["job"].spec["schedule"] is None
    warnings = planner.warnings(InfrastructureRequest.model_validate(body), planned, config, provider)
    for expected in ("its nodes are Standard_D4s_v5 (4 vCPUs, 16 GB)", "has 2 nodes at least, so it has 2", "comes with 2048 MiB of memory, not 512",
                     "240 seconds at most", "gives a task no index", "Container Apps environment, which is internal"):
        assert any(expected in item for item in warnings), expected
    lines = {line.kind: line for line in provider.estimate_cost(planned).lines}
    assert lines["cluster"].monthly_usd == pytest.approx(73 + 2 * (0.192 * 730 + 9.6 + 0), abs=12), "the Standard tier and two nodes"
    assert lines["service"].monthly_usd == 0 and lines["job"].monthly_usd == 0, "nothing while idle"
    assert lines["load_balancer"].monthly_usd == pytest.approx((0.025 + 0.005) * 730, abs=0.01)
    hostnames = plan_of(make_azure_provider(make_azure_config(azure={"public_domains": ["example.com"]}))[0], config,
                        {**body, "load_balancer": {"hostnames": ["shop.example.com"]}})
    fronted = next(item for item in hostnames if item.kind == "load_balancer")
    assert "Front Door" in fronted.provider_mapping and provider.estimate_cost([fronted]).monthly_usd == pytest.approx(lines["load_balancer"].monthly_usd + 35, abs=0.01)
    assert fake.calls == []


def test_a_clusters_nodes_count_against_the_family_quota(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, limits={"max_monthly_cost_usd": 10000}))
    response = provision(orchestrator, request(network={}, cluster={"node_count": 4, "node_cpu": 8, "node_memory_gb": 32}))
    assert response.status == RequestStatus.FAILED and response.error.details["cause"] == ErrorCode.QUOTA_EXCEEDED
    assert (response.error.details["metric"], response.error.details["needed"]) == ("CPUS", 32), "four nodes of eight vCPUs"
    assert fake.order == [], "refused before anything is made"


# ---- clusters

def cluster_config(tmp_path, **azure):
    return make_azure_config(tmp_path, azure={"registry": REGISTRY, "cluster_registries": [f"{REGISTRY_LOGIN}/base"],
                                              "cluster_authorized_networks": ["203.0.113.0/24"], **azure}, limits={"max_monthly_cost_usd": 2000})


def test_a_cluster_is_aks_with_entra_rbac_its_own_identities_and_the_requests_namespace(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=cluster_config(tmp_path))
    operator_registry(fake)
    who = principal(orchestrator.config)
    response = provision(orchestrator, request(name="ml", network={}, identity={"name": "trainer"}, access=MEMBERS,
                                               cluster={"registries": [f"{REGISTRY_LOGIN}/base"], "kubernetes_version": "1.33"}))
    assert response.status == RequestStatus.READY, response.error
    [cluster] = response.resources.cluster
    [vnet] = of_kind(fake, "Microsoft.Network/virtualNetworks")
    control = fake.of_type("Microsoft.ManagedIdentity/userAssignedIdentities")["ml-cluster-control"]["properties"]["principalId"]
    writes = [path.lower() for method, path in fake.order if method == "PUT"]
    granted = next(index for index, path in enumerate(writes) if path.endswith("/subnets/ml-subnet/providers/microsoft.authorization/roleassignments/"
                                                                                  + path.rsplit("/", 1)[-1]))
    made = next(index for index, path in enumerate(writes) if path.endswith("/managedclusters/ml-cluster"))
    assert granted < made, "the control plane's role on the subnet is there before AKS checks it"
    assert (control, NETWORK_CONTRIBUTOR) in roles_at(fake, "/subnets/ml-subnet")
    [body] = puts(fake, "managedClusters")
    properties, pool = body["properties"], body["properties"]["agentPoolProfiles"][0]
    assert body["sku"] == {"name": "Base", "tier": "Standard"} and list(body["identity"]["userAssignedIdentities"])[0].endswith("/ml-cluster-control")
    assert (pool["mode"], pool["count"], pool["vmSize"], pool["osSKU"], pool["enableNodePublicIP"]) == ("System", 2, "Standard_D4s_v5", "AzureLinux3", False)
    assert pool["vnetSubnetID"] == f"{vnet['id']}/subnets/ml-subnet" and pool["availabilityZones"] == ["1"]
    assert properties["networkProfile"] == {"networkPlugin": "azure", "networkPluginMode": "overlay", "networkPolicy": "cilium", "networkDataplane": "cilium",
                                            "outboundType": "userAssignedNATGateway", "loadBalancerSku": "standard", "podCidr": "10.244.0.0/16",
                                            "serviceCidr": "10.0.0.0/16", "dnsServiceIP": "10.0.0.10"}
    assert properties["aadProfile"]["managed"] and properties["aadProfile"]["enableAzureRBAC"] and properties["disableLocalAccounts"] is True
    assert properties["oidcIssuerProfile"] == {"enabled": True} and properties["securityProfile"] == {"workloadIdentity": {"enabled": True}}
    assert properties["autoUpgradeProfile"] == {"upgradeChannel": "patch", "nodeOSUpgradeChannel": "NodeImage"} and properties["kubernetesVersion"] == "1.33"
    assert properties["apiServerAccessProfile"] == {"authorizedIPRanges": ["203.0.113.0/24"]}
    assert properties["nodeResourceGroup"] == f"ml-{response.request_id[:8]}-cluster-nodes"
    # the kubelet pulls under the named prefix only
    kubelet = cluster.node_service_account
    [pull] = [item for item in of_kind(fake, "Microsoft.Authorization/roleAssignments") if item["properties"]["principalId"] == kubelet]
    assert pull["properties"]["roleDefinitionId"].endswith(REPOSITORY_READER) and prefixes_of(pull["properties"]["condition"]) == {"base"}
    # the identity's federated credential names the cluster's issuer and the request's namespace
    namespace = f"ml-{response.request_id[:6]}"
    [federated] = of_kind(fake, "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials")
    issuer = fake.of_type("Microsoft.ContainerService/managedClusters")["ml-cluster"]["properties"]["oidcIssuerProfile"]["issuerURL"]
    assert federated["properties"]["subject"] == f"system:serviceaccount:{namespace}:trainer" and federated["properties"]["issuer"] == issuer
    assert federated["id"].endswith("/userAssignedIdentities/ml-trainer/federatedIdentityCredentials/ml-cluster")
    [identity] = response.resources.identity
    assert identity.kubernetes_service_account == f"{namespace}/trainer"
    # what is in the cluster: the admission policy, then the namespace
    endpoint = cluster.endpoint
    assert AKS_SERVER.fullmatch(endpoint) and cluster.ca_certificate and cluster.namespace == namespace and cluster.subnet.endswith("/subnets/ml-subnet")
    assert {path.rsplit("/", 1)[-1] for (where, path) in fake.kube_objects if where == endpoint} == {"cloudinfra-no-public-entry"}
    assert len([key for key in fake.kube_objects if key[0] == endpoint]) == 2, "the policy and its binding"
    assert fake.kube_namespaces[endpoint][namespace]["pod-security.kubernetes.io/enforce"] == "baseline"
    # members: the cluster's user kubeconfig, and Azure RBAC at the request's namespace only
    assert {(ADA, CLUSTER_USER), (GRACE, CLUSTER_USER)} <= roles_at(fake, "/managedClusters/ml-cluster")
    assert roles_at(fake, f"/namespaces/{namespace}") == {(ADA, AKS_RBAC_READER), (GRACE, AKS_RBAC_WRITER), (identity.principal_id, AKS_RBAC_READER)}, \
        "the request's identity holds its grants at its level, as any member"
    assert (cluster.status.value, cluster.version) == ("READY", "1.33")
    # overlay traffic between pods of two nodes is not encapsulated: the subnet's security group admits the pod range to itself
    nsg = f"{vnet['id'].split('/providers/')[0]}/providers/Microsoft.Network/networkSecurityGroups/ml-net-nsg"
    assert fake.admits(nsg, "10.244.1.7", "10.244.0.9", 53) and not fake.admits(nsg, "10.244.1.7", "10.10.0.4", 22), "pods to pods, not pods to machines"
    writes = [path.lower() for method, path in fake.order if method == "PUT"]
    assert writes.index(f"{nsg}/securityRules/ml-cluster-pods".lower()) < writes.index(cluster.provider_id.lower()), "before the nodes start"
    connect = orchestrator.get_resource(cluster.id, who).resource
    assert connect.namespace == namespace
    destroyed = orchestrator.destroy(response.request_id, who, wait_seconds=45)
    assert destroyed.status == RequestStatus.DESTROYED, destroyed.error
    assert only_the_hub_left(fake), [name for name in fake.resources if "/resourcegroups/hub" not in name]
    assert not of_kind(fake, "Microsoft.Authorization/roleAssignments"), "the kubelet's pull on the operator's registry went before the cluster"


def test_an_identitys_connect_block_says_how_a_pod_acts_as_it(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=cluster_config(tmp_path))
    response = provision(orchestrator, request(name="ml", network={}, identity={"name": "trainer"}, cluster={}))
    [identity] = response.resources.identity
    info = orchestrator.registry.get("azure").connect_info(identity, response.resources.all())
    namespace = f"ml-{response.request_id[:6]}"
    assert info.commands["kubernetes"] == (f"kubectl create serviceaccount trainer -n {namespace} && kubectl annotate serviceaccount trainer -n {namespace} "
                                           f"azure.workload.identity/client-id={identity.client_id}")
    assert any("azure.workload.identity/use" in note for note in info.notes)


def test_a_cluster_create_is_tried_again_while_aks_does_not_see_the_role_on_the_subnet(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=cluster_config(tmp_path))
    fake.role_lag = 2
    response = provision(orchestrator, request(network={}, cluster={}))
    assert response.status == RequestStatus.READY and len(puts(fake, "managedClusters")) == 3


@pytest.mark.parametrize("failure", ["kube", "registry"])
def test_a_cluster_that_fails_half_way_leaves_nothing_behind(tmp_path, failure):
    config = cluster_config(tmp_path, cluster_registries=["otherregistry.azurecr.io/base"]) if failure == "registry" else cluster_config(tmp_path)
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=config)
    operator_registry(fake)
    if failure == "kube":
        fake.kube_fail = InfrastructureError(ErrorCode.PROVIDER_PERMISSION_DENIED, "The cluster's API answered 403", {"http_status": 403})
    registries = ["otherregistry.azurecr.io/base"] if failure == "registry" else []
    response = provision(orchestrator, request(network={}, identity={"name": "trainer"}, cluster={"registries": registries}))
    assert response.status == RequestStatus.FAILED
    if failure == "registry":
        assert "is not in the operator's registry" in response.error.message
    assert only_the_hub_left(fake) and not of_kind(fake, "Microsoft.Authorization/roleAssignments")


def test_the_default_cluster_client_asks_entra_for_aks_s_own_audience(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ca")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1) \
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True) \
        .sign(key, hashes.SHA256())
    ca = base64.b64encode(certificate.public_bytes(serialization.Encoding.PEM)).decode()
    provider, fake = make_azure_provider(make_azure_config())
    client = AzureCluster(fake, provider.settings, provider.groups, provider.access, provider.registries, provider.network, provider.tenant, provider.ceiling).kube("c.hcp.eastus2.azmk8s.io:443", ca)
    assert client.base == "https://c.hcp.eastus2.azmk8s.io:443" and client.provider == "azure"
    assert client.token() == "fake-token:6dae42f8-4368-4678-94ff-3960e28e3630"


# ---- services and jobs

def apps_config(tmp_path, **azure):
    return make_azure_config(tmp_path, azure={"service_auth_client_id": CLIENT, **azure})


def test_services_and_jobs_run_in_their_networks_internal_environment_with_sign_in_and_their_own_identity(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=apps_config(tmp_path))
    who = principal(orchestrator.config)
    response = provision(orchestrator, request(name="shop", network={}, secret={"name": "api-key"}, access=MEMBERS,
                                               service={"name": "web", "secrets": ["api-key"], "min_instances": 1, "max_instances": 5, "concurrency": 50},
                                               job={"name": "nightly", "schedule": {"cron": "0 2 * * *"}, "tasks": 2, "max_retries": 1}))
    assert response.status == RequestStatus.READY, response.error
    [network] = of_kind(fake, "Microsoft.Network/virtualNetworks")
    subnets = {item["name"]: item["properties"] for item in network["properties"]["subnets"]}
    assert subnets["shop-subnet"]["addressPrefix"] == "10.10.0.0/25" and subnets["shop-net-apps"]["addressPrefix"] == "10.10.0.128/27"
    assert subnets["shop-net-apps"]["delegations"] == [{"name": "apps", "properties": {"serviceName": "Microsoft.App/environments"}}]
    assert subnets["shop-net-apps"]["natGateway"] == subnets["shop-subnet"]["natGateway"] and subnets["shop-net-apps"]["defaultOutboundAccess"] is False
    [environment] = puts(fake, "managedEnvironments")
    assert environment["properties"]["vnetConfiguration"] == {"infrastructureSubnetId": f"{network['id']}/subnets/shop-net-apps", "internal": True}
    assert environment["properties"]["workloadProfiles"] == [{"name": "Consumption", "workloadProfileType": "Consumption"}]
    assert environment["properties"]["zoneRedundant"] is True and environment["tags"]["cloudinfra-request"] == response.request_id
    # the service
    [service] = response.resources.service
    [app] = puts(fake, "containerApps")
    configuration, template = app["properties"]["configuration"], app["properties"]["template"]
    assert app["properties"]["environmentId"].endswith("/managedEnvironments/shop-net-apps") and app["properties"]["workloadProfileName"] == "Consumption"
    assert configuration["ingress"]["external"] is True and configuration["ingress"]["allowInsecure"] is False and configuration["activeRevisionsMode"] == "Multiple"
    assert template["containers"][0]["resources"] == {"cpu": 1, "memory": "2Gi"}
    assert template["scale"] == {"minReplicas": 1, "maxReplicas": 5, "rules": [{"name": "http", "http": {"metadata": {"concurrentRequests": "50"}}}]}
    assert list(app["identity"]["userAssignedIdentities"]) == [service.runtime_service_account] and service.runtime_service_account.endswith("/shop-web-run")
    assert service.url.startswith("https://shop-web.") and service.revision.startswith("shop-web--") and service.memory_mb == 2048
    auth = fake.resources[f"{service.provider_id}/authConfigs/current".lower()]["properties"]
    entra = auth["identityProviders"]["azureActiveDirectory"]
    assert auth["globalValidation"] == {"unauthenticatedClientAction": "Return401"} and entra["registration"]["clientId"] == CLIENT
    assert entra["validation"]["allowedAudiences"] == [f"api://{CLIENT}"]
    assert sorted(entra["validation"]["defaultAuthorizationPolicy"]["allowedPrincipals"]["identities"]) == sorted([ADA, GRACE]), "nobody's stand-in is gone"
    runtime = fake.of_type("Microsoft.ManagedIdentity/userAssignedIdentities")["shop-web-run"]["properties"]["principalId"]
    assert roles_at(fake, "/secrets/shop-api-key") >= {(runtime, SECRETS_USER)}
    assert roles_at(fake, "/containerApps/shop-web") == {(GRACE, APPS_OPERATOR)}, "use calls it through sign-in; admin also runs its revisions, never its sign-in"
    # the job
    [job] = response.resources.job
    [body] = puts(fake, "jobs")
    assert body["properties"]["configuration"] == {"triggerType": "Schedule", "replicaTimeout": 600, "replicaRetryLimit": 1,
                                                   "scheduleTriggerConfig": {"cronExpression": "0 2 * * *", "parallelism": 2, "replicaCompletionCount": 2}}
    assert job.schedule == {"cron": "0 2 * * *", "time_zone": "UTC"} and job.scheduler is None and job.scheduler_service_account is None
    assert roles_at(fake, "/jobs/shop-nightly") == {(ADA, JOBS_READER), (GRACE, JOBS_CONTRIBUTOR)}, "a start may override the image: admin's"
    # a revoke takes the member off the allow-list, and the last one leaves nobody's stand-in
    orchestrator.manage_access(response.request_id, "revoke", "user:ada@example.com", who)
    orchestrator.manage_access(response.request_id, "revoke", "user:grace@example.com", who)
    allowed = fake.resources[f"{service.provider_id}/authConfigs/current".lower()]["properties"]["identityProviders"]["azureActiveDirectory"]["validation"]
    assert allowed["defaultAuthorizationPolicy"]["allowedPrincipals"]["identities"] == [NOBODY]
    assert roles_at(fake, "/containerApps/shop-web") == set() and roles_at(fake, "/jobs/shop-nightly") == set()
    # the environment goes before the network, and the runtime identities with their grants
    destroyed = orchestrator.destroy(response.request_id, who, wait_seconds=45)
    assert destroyed.status == RequestStatus.DESTROYED, destroyed.error
    deletes = [path.lower() for method, path in fake.order if method == "DELETE"]
    assert deletes.index(next(path for path in deletes if path.endswith("/managedenvironments/shop-net-apps"))) < \
        deletes.index(next(path for path in deletes if path.endswith("/virtualnetworks/shop-net")))
    assert fake.resources == {}


def test_a_public_service_has_no_sign_in_and_admin_still_changes_it(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=apps_config(tmp_path, allow_public_services=True))
    response = provision(orchestrator, request(name="docs", network={}, service={"public": True}, access=MEMBERS))
    assert response.status == RequestStatus.READY and puts(fake, "authConfigs") == []
    [service] = response.resources.service
    assert roles_at(fake, "/containerApps/docs-service") == {(GRACE, APPS_OPERATOR)}
    assert "curl https://docs-service." in orchestrator.registry.get("azure").connect_info(service, response.resources.all()).commands["call"]


def test_a_services_allow_list_holds_thirteen_members_and_says_to_grant_a_group(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=apps_config(tmp_path))
    response = provision(orchestrator, request(name="api", network={}, service={}))
    [service] = response.resources.service
    apps = orchestrator.registry.get("azure").apps
    for number in range(13):
        apps.allow(service, {"id": f"aaaaaaaa-0000-4000-8000-{number:012d}", "type": "User"}, True)
    with pytest.raises(InfrastructureError) as caught:
        apps.allow(service, {"id": "bbbbbbbb-0000-4000-8000-000000000000", "type": "User"}, True)
    assert caught.value.code == ErrorCode.INVALID_ACCESS and "grant a group" in caught.value.message
    apps.allow(service, {"id": "cccccccc-0000-4000-8000-000000000000", "type": "Group"}, True)
    policy = fake.resources[f"{service.provider_id}/authConfigs/current".lower()]["properties"]["identityProviders"]["azureActiveDirectory"]["validation"]
    assert policy["defaultAuthorizationPolicy"]["allowedPrincipals"]["groups"] == ["cccccccc-0000-4000-8000-000000000000"]


def test_a_service_on_a_network_made_without_room_for_container_apps_is_refused_with_why(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=apps_config(tmp_path))
    first = provision(orchestrator, request(name="base", network={}))
    [network] = first.resources.network
    second = provision(orchestrator, request(name="late", service={"network_id": network.id}))
    assert second.status == RequestStatus.FAILED and "has no subnet for Container Apps" in second.error.message
    assert puts(fake, "managedEnvironments") == [] and not of_kind(fake, "Microsoft.ManagedIdentity/userAssignedIdentities")


def test_a_network_shared_by_a_later_request_shares_its_environment(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=apps_config(tmp_path))
    who = principal(orchestrator.config)
    first = provision(orchestrator, request(name="base", network={}, job={}))
    [network] = first.resources.network
    second = provision(orchestrator, request(name="late", job={"network_id": network.id}))
    assert second.status == RequestStatus.READY, second.error
    assert len(of_kind(fake, "Microsoft.App/managedEnvironments")) == 1
    [late] = second.resources.job
    assert fake.resources[late.provider_id.lower()]["properties"]["environmentId"].lower().endswith("/managedenvironments/base-net-apps")
    assert orchestrator.destroy(second.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED
    assert len(of_kind(fake, "Microsoft.App/managedEnvironments")) == 1, "the environment is the network's"
    assert orchestrator.destroy(first.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED and fake.resources == {}


def test_a_role_azure_has_not_replicated_yet_is_waited_for_on_the_runtimes_secret(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=apps_config(tmp_path))
    fake.refuse[("PUT", "/secrets/token/providers/Microsoft.Authorization/roleAssignments/")] = [refusal(400, "PrincipalNotFound")]
    response = provision(orchestrator, request(name="w", network={}, secret={"name": "token"}, job={"secrets": ["token"]}))
    assert response.status == RequestStatus.READY, response.error


# ---- load balancers

def balancer_config(tmp_path, **azure):
    return make_azure_config(tmp_path, azure={"public_domains": ["example.com"], **azure}, allow_public_exposure=True)


def test_a_tcp_load_balancer_passes_connections_to_the_machines_by_their_private_addresses(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=balancer_config(tmp_path))
    who = principal(orchestrator.config)
    network = {"public_access": True, "allowed_ports": [443], "allowed_sources": ["203.0.113.0/24"]}
    response = provision(orchestrator, request(name="db", network=network, compute=[{"group": "pg", "count": 2, "cpu": 2, "memory_gb": 8},
                                                                               {"group": "ops", "cpu": 2, "memory_gb": 8, "public_ip": True}],
                                               load_balancer={"type": "tcp", "port": 5432, "backend_group": "pg"}))
    assert response.status == RequestStatus.READY, response.error
    [balancer] = response.resources.load_balancer
    [address] = puts(fake, "publicIPAddresses")[-1:]
    assert address["sku"] == {"name": "Standard", "tier": "Regional"} and address["zones"] == ["1", "2", "3"]
    [body] = puts(fake, "loadBalancers")
    properties = body["properties"]
    assert properties["backendAddressPools"] == [{"name": "machines"}], "members go by the pool's own call: Azure keeps none named here"
    [pool] = puts(fake, "backendAddressPools")
    members = pool["properties"]["loadBalancerBackendAddresses"]
    [kept] = of_kind(fake, "Microsoft.Network/loadBalancers/backendAddressPools")
    assert len(kept["properties"]["loadBalancerBackendAddresses"]) == 2, "and the pool holds them afterwards"
    [network] = of_kind(fake, "Microsoft.Network/virtualNetworks")
    behind = sorted(machine.internal_ip for machine in response.resources.compute if machine.group == "pg")
    [outside] = [machine.internal_ip for machine in response.resources.compute if machine.group == "ops"]
    assert sorted(member["properties"]["ipAddress"] for member in members) == behind
    assert {member["properties"]["virtualNetwork"]["id"] for member in members} == {network["id"]}
    assert properties["probes"][0]["properties"] == {"protocol": "Tcp", "port": 5432, "intervalInSeconds": 5, "probeThreshold": 2}
    rule = properties["loadBalancingRules"][0]["properties"]
    assert (rule["frontendPort"], rule["backendPort"], rule["disableOutboundSnat"], rule["enableTcpReset"]) == (5432, 5432, True, True)
    [security] = puts(fake, "securityRules")
    assert security["properties"]["sourceAddressPrefix"] == "Internet" and security["properties"]["priority"] == 300
    assert security["properties"]["destinationPortRanges"] == ["5432"] and security["properties"]["destinationAddressPrefixes"] == behind
    nsg = next(path for method, path in fake.order if method == "PUT" and "/securityRules/" in path).split("/securityRules/")[0]
    assert fake.admits(nsg, "52.168.10.20", behind[0], 5432) and not fake.admits(nsg, "52.168.10.20", outside, 5432), \
        "the port is open to the balanced machines only, not to a machine with its own address beside them"
    assert balancer.address.startswith("20.1.2.") and balancer.certificate is None
    assert [item["kind"] for item in balancer.components] == ["public_ip", "load_balancer", "security_rule"]
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED and fake.resources == {}


def test_hostnames_put_front_door_with_managed_certificates_in_front_and_admit_only_its_backends(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=balancer_config(tmp_path, front_door_waf_policy=WAF))
    who = principal(orchestrator.config)
    response = provision(orchestrator, request(name="shop", network={}, compute={"cpu": 2, "memory_gb": 8},
                                               load_balancer={"port": 8080, "health_check_path": "/healthz", "hostnames": ["shop.example.com"]}))
    assert response.status == RequestStatus.READY, response.error
    [balancer] = response.resources.load_balancer
    [security] = puts(fake, "securityRules")
    assert security["properties"]["sourceAddressPrefix"] == "AzureFrontDoor.Backend"
    [lb] = puts(fake, "loadBalancers")
    assert lb["properties"]["probes"][0]["properties"]["requestPath"] == "/healthz"
    [origins] = puts(fake, "originGroups")
    assert origins["properties"]["healthProbeSettings"]["probePath"] == "/healthz"
    [origin] = puts(fake, "origins")
    public = next(item["properties"]["ipAddress"] for item in of_kind(fake, "Microsoft.Network/publicIPAddresses") if item["name"] == "shop-lb-ip")
    assert origin["properties"]["hostName"] == public and origin["properties"]["httpPort"] == 8080
    [domain] = puts(fake, "customDomains")
    assert domain["properties"] == {"hostName": "shop.example.com", "tlsSettings": {"certificateType": "ManagedCertificate", "minimumTlsVersion": "TLS12"}}
    [route] = puts(fake, "routes")
    assert (route["properties"]["httpsRedirect"], route["properties"]["forwardingProtocol"], route["properties"]["supportedProtocols"]) == \
        ("Enabled", "HttpOnly", ["Http", "Https"])
    [policy] = puts(fake, "securityPolicies")
    assert policy["properties"]["parameters"]["wafPolicy"] == {"id": WAF}
    assert re.fullmatch(r"[a-z0-9-]+\.z01\.azurefd\.net", balancer.address) and balancer.certificate == "PROVISIONING"
    [door] = [item for item in balancer.components if item["kind"] == "front_door"]
    assert door["front_door_id"] == fake.of_type("Microsoft.Cdn/profiles")["shop-lb-fd"]["properties"]["frontDoorId"]
    [record] = [item for item in balancer.components if item["kind"] == "custom_domain"]
    assert record["txt_name"] == "_dnsauth.shop.example.com" and record["txt"].startswith("token-for-shop-example-com") and record["cname"] == balancer.address
    info = orchestrator.registry.get("azure").connect_info(balancer, response.resources.all())
    assert "_dnsauth.shop.example.com TXT token-for-" in info.prerequisites[0] and f"shop.example.com CNAME {balancer.address}" in info.prerequisites[0]
    assert any(door["front_door_id"] in note for note in info.notes), "the id the application checks X-Azure-FDID against"
    fake.resources[record["url"].lower()]["properties"]["domainValidationState"] = "Approved"
    assert orchestrator.registry.get("azure").get_load_balancer(balancer).certificate == "ACTIVE"
    assert orchestrator.destroy(response.request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED and fake.resources == {}


def test_a_second_load_balancer_on_a_shared_network_takes_the_next_free_priority(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=balancer_config(tmp_path))
    first = provision(orchestrator, request(name="one", network={}, compute={"cpu": 2, "memory_gb": 8}, load_balancer={}))
    [network] = first.resources.network
    second = provision(orchestrator, request(name="two", compute={"cpu": 2, "memory_gb": 8, "network_id": network.id}, load_balancer={}))
    assert second.status == RequestStatus.READY, second.error
    assert [rule["properties"]["priority"] for rule in puts(fake, "securityRules")] == [300, 301]


def test_a_load_balancer_that_fails_half_way_leaves_nothing_behind(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=balancer_config(tmp_path))
    fake.fail_on.append(("PUT", "/customDomains/"))
    response = provision(orchestrator, request(name="shop", network={}, compute={"cpu": 2, "memory_gb": 8}, load_balancer={"hostnames": ["shop.example.com"]}))
    assert response.status == RequestStatus.FAILED and fake.resources == {}


def test_clusters_services_and_jobs_take_grants_and_a_load_balancer_does_not():
    provider, _ = make_azure_provider(make_azure_config())
    assert all(provider.takes_grants(kind) for kind in ("cluster", "service", "job")) and not provider.takes_grants("load_balancer")


def test_a_load_balancer_over_another_requests_machines_uses_their_network_and_admits_them_there(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=balancer_config(tmp_path))
    first = provision(orchestrator, request(name="one", network={}, compute={"count": 2, "cpu": 2, "memory_gb": 8}))
    machines = [machine.id for machine in first.resources.compute]
    second = provision(orchestrator, request(name="three", network={"cidr": "10.20.0.0/24"}, load_balancer={"backends": machines}))
    assert second.status == RequestStatus.READY, second.error
    [pool] = puts(fake, "backendAddressPools")
    members = pool["properties"]["loadBalancerBackendAddresses"]
    assert {member["properties"]["virtualNetwork"]["id"].rsplit("/", 1)[-1] for member in members} == {"one-net"}, "the machines' network, not this request's"
    [rule] = [path for method, path in fake.order if method == "PUT" and "/securityRules/" in path]
    assert "/networkSecurityGroups/one-net-nsg/" in rule
    third = provision(orchestrator, request(name="four", network={"cidr": "10.30.0.0/24"}, compute={"cpu": 2, "memory_gb": 8}))
    assert third.status == RequestStatus.READY
    mixed = provision(orchestrator, request(name="five", load_balancer={"backends": [machines[0], third.resources.compute[0].id]}))
    assert mixed.status == RequestStatus.FAILED and "on one network" in mixed.error.message


def test_a_network_whose_rules_refuse_egress_takes_no_cluster_and_no_container_apps(tmp_path):
    from cloudinfra.models.resources import FirewallRule, NetworkResource
    provider, fake = make_azure_provider(make_azure_config(azure={"service_auth_client_id": CLIENT}))
    network = NetworkResource(id="net-0123456789ab", name="old-net", request_id="0" * 32, provider="azure", region="eastus2", nat=True, cidr="10.10.0.0/24",
                              provider_id=f"/subscriptions/{'0' * 8}-0000-4000-8000-000000000001/resourceGroups/old-01234567/providers/Microsoft.Network/virtualNetworks/old-net",
                              subnet="x/subnets/old-subnet", firewall_rules=[FirewallRule(name="old-net-deny-egress", direction="egress", deny=True, ports=[], sources=["Internet"])])
    with pytest.raises(InfrastructureError) as caught:
        provider.apps.ensure_environment(network, "job")
    assert caught.value.code == ErrorCode.INVALID_SERVICE_CONFIGURATION and "refuse egress" in caught.value.message
    assert fake.calls == []
