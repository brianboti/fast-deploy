"""AKS clusters behind the generic cluster primitives (docs/research/RESEARCH-2026-11-03-azure.md, section 5.5).

One cluster with one system pool of identical nodes (at least two of four vCPUs, AKS's floor), in the request network's
subnet, leaving through its NAT gateway (outboundType userAssignedNATGateway): a network without one, or whose rules
refuse egress, is refused. Azure CNI in overlay mode with Cilium, which enforces Kubernetes NetworkPolicy as GKE's
Dataplane V2 does; overlay traffic between pods on different nodes is not encapsulated, so the subnet's security group
sees it, and the cluster adds a rule there admitting its pod range to itself (<cluster>-pods), removed with it
(learn.microsoft.com/azure/aks/concepts-network-azure-cni-overlay, "Network security groups"); Azure Linux 3 nodes,
the patch channel for a pinned version and the stable channel otherwise, node images kept current; the Standard tier,
whose control plane has an SLA. Entra ID is the only sign-in (local accounts off) and Azure RBAC decides who does what
in the cluster, so a member's grant is two role assignments and no cluster call: the Cluster User role on the cluster,
for its user kubeconfig, and RBAC Reader (use) or Writer (admin) on the request's namespace only
(learn.microsoft.com/azure/aks/manage-azure-rbac). The API server is public behind Entra sign-in and the operator's
authorized ranges, as a GKE cluster's is behind IAM and its authorized networks.

Identities. The control plane runs as a user-assigned identity made for the cluster, granted Network Contributor on its
nodes' subnet before the cluster is made (learn.microsoft.com/azure/aks/concepts-network-cni-overview) and removed
after it. The nodes pull as the kubelet identity AKS makes, which this service grants Container Registry Repository
Reader under the registry prefixes the request names, and nothing else. The OIDC issuer and workload identity are on:
each of the request's identities gets a federated credential for the service account of its name in the request's
namespace, so a pod acts as it with no secret.

What the cluster API holds, the same as on GKE (cloudinfra/providers/kubernetes.py): the request's namespace, labelled
for Pod Security baseline, and the admission policy that refuses LoadBalancer and NodePort Services, Ingresses and
Gateways in it. The service reaches the API with an Entra token for AKS's server application, and its own role holds the
Kubernetes data actions for those objects (REQUIREMENTS.md).

A front door (cluster.public) is refused: Application Gateway for Containers, Azure's Gateway API, is offered for AKS
only as a preview add-on (2026-11-05), and this adapter calls GA APIs only.
"""
import base64
from urllib.parse import urlparse

import yaml

from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import ClusterResource, ResourceStatus
from ...observability.logging import event
from ..kubernetes import KubeApi, no_public_entry, tenant_labels, tenant_namespace
from . import translate
from .access import NETWORK_CONTRIBUTOR, PREFIX
from .api import API_VERSIONS
from .clouds import AKS_SERVER_APPLICATION
from .groups import group_of
from .network import reaches_internet, security_group

VERSION = API_VERSIONS["Microsoft.ContainerService"]
IDENTITY_VERSION = API_VERSIONS["Microsoft.ManagedIdentity"]
POOL = "system"
FEDERATED_AUDIENCE = "api://AzureADTokenExchange"
STATES = {"succeeded": ResourceStatus.READY, "creating": ResourceStatus.CREATING, "updating": ResourceStatus.UPDATING, "upgrading": ResourceStatus.UPDATING,
          "scaling": ResourceStatus.UPDATING, "starting": ResourceStatus.UPDATING, "stopping": ResourceStatus.UPDATING, "deleting": ResourceStatus.DELETING,
          "failed": ResourceStatus.FAILED, "canceled": ResourceStatus.FAILED}
# A role just granted to the control plane's identity may not be seen by AKS's own check of the subnet for a moment: the
# create is tried again, this many times, this many seconds apart.
NOT_YET = (6, 20)
LINKED = ("LinkedAuthorizationFailed", "AuthorizationFailed")


def control_identity(cluster_id: str) -> str:
    """The control plane's identity of a cluster: <cluster>-control, in the cluster's group."""
    group, name = cluster_id.split("/providers/", 1)[0], cluster_id.rsplit("/", 1)[-1]
    return f"{group}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{name}-control"


def node_group(cluster_or_identity_id: str, cluster: str) -> str:
    """The group AKS makes for the nodes and deletes with the cluster: named after the request's own, so it is plainly the
    request's (AKS's default is MC_<group>_<cluster>_<region>)."""
    return f"{group_of(cluster_or_identity_id)}-{cluster}-nodes"[:80]


def pods_rule(subnet: str, cluster: str) -> str:
    """The rule a cluster adds to its subnet's security group for its pods."""
    return f"{security_group(subnet.split('/subnets/', 1)[0])}/securityRules/{cluster}-pods"


def kubelet_of(found: dict | None) -> str | None:
    """The kubelet identity's object id in a cluster as Azure answers it."""
    profile = ((found or {}).get("properties") or {}).get("identityProfile") or {}
    return (profile.get("kubeletidentity") or {}).get("objectId")


def kubeconfig_endpoint(value: str) -> tuple[str, str]:
    """(host:port, base64 CA) of the first cluster in a kubeconfig AKS gave, base64-encoded."""
    config = yaml.safe_load(base64.b64decode(value)) or {}
    cluster = ((config.get("clusters") or [{}])[0] or {}).get("cluster") or {}
    server, ca = urlparse(str(cluster.get("server") or "")), cluster.get("certificate-authority-data")
    if server.scheme != "https" or not server.netloc or not ca:
        raise InfrastructureError(ErrorCode.PROVIDER_ERROR, "AKS gave a kubeconfig without an https server and its CA")
    return server.netloc, ca


class AzureCluster:
    def __init__(self, api, settings, groups, access, registries, network, tenant, ceiling, kube=None):
        self.api, self.settings, self.groups, self.access, self.registries, self.network = api, settings, groups, access, registries, network
        self.tenant, self.ceiling = tenant, ceiling
        self.kube = kube or (lambda endpoint, ca_certificate: KubeApi(endpoint, ca_certificate, lambda: api.token(AKS_SERVER_APPLICATION), provider="azure"))

    # ---- create
    def create(self, spec, planned, ctx, network) -> ClusterResource:
        if network is None:
            raise InfrastructureError(ErrorCode.INVALID_CLUSTER_CONFIGURATION, "An AKS cluster needs a network of the request, or network_id naming one this "
                                      "service created", resource=planned.name)
        if not network.nat or not reaches_internet(network, False):
            raise InfrastructureError(ErrorCode.INVALID_CLUSTER_CONFIGURATION, f"Network {network.name} has no NAT gateway, or its rules refuse egress, and an "
                                      "AKS cluster's nodes leave through its NAT gateway; give the cluster a network with nat on, where the operator does not deny "
                                      "egress", resource=planned.name)
        group = self.groups.ensure(ctx)
        cluster_id = f"{self.groups.id(group)}/providers/Microsoft.ContainerService/managedClusters/{planned.name}"
        control_id = control_identity(cluster_id)
        tags = translate.tags(ctx)
        resource = ClusterResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="azure", region=ctx.region, zone=ctx.zone,
                                   provider_id=cluster_id, location=ctx.region, node_count=planned.spec["node_count"], machine_type=planned.spec["machine_type"],
                                   node_disk_gb=spec.node_disk_gb, node_pool=POOL, network=network.id, subnet=network.subnet, registries=list(spec.registries),
                                   labels=tags, status=ResourceStatus.CREATING)
        ctx.accepted(resource)  # the cluster, its control identity and the role on the subnet are named here: a cleanup finds them all
        control = self.api.put(self.api.arm(control_id), {"location": ctx.region, "tags": tags}, IDENTITY_VERSION, timeout=self.ceiling("identity"))
        principal = (control.get("properties") or {}).get("principalId")
        self.access.assign(network.subnet, {"id": principal, "type": "ServicePrincipal"}, NETWORK_CONTRIBUTOR,
                           f"cloudinfra request {ctx.request_id} ({ctx.name}): the control plane of cluster {planned.name}, on its nodes' subnet")
        pods = translate.cluster_ranges(network.cidr)[0]  # before the nodes start: CoreDNS answers pods on other nodes across it
        self.network.admit(security_group(network.provider_id), f"{planned.name}-pods", pods, [], pods, protocol="*")
        found = self._create(cluster_id, self._body(spec, planned, ctx, network, control_id, tags))
        properties = found.get("properties") or {}
        kubelet = kubelet_of(found)
        resource = resource.model_copy(update={"node_service_account": kubelet})
        ctx.accepted(resource)  # the kubelet's grants are found from here
        if resource.registries:
            self._pull(kubelet, resource.registries, ctx, give=True)
        namespace = tenant_namespace(ctx)
        self._federate(planned.spec.get("identities") or [], cluster_id, (properties.get("oidcIssuerProfile") or {}).get("issuerURL"), namespace, planned.name)
        endpoint, ca = self._credentials(cluster_id)
        kube = self.kube(endpoint, ca)
        labels = {"managed-by": "cloudinfra", "cloudinfra-request": ctx.request_id}
        for collection, body in no_public_entry(labels):  # before the namespace, so no moment exists without it
            kube.put(collection, body)
        kube.ensure_namespace(namespace, tenant_labels(labels))
        return self._view(resource.model_copy(update={"namespace": namespace, "endpoint": endpoint, "ca_certificate": ca}), found)

    def _body(self, spec, planned, ctx, network, control_id: str, tags: dict) -> dict:
        pods, services, dns = translate.cluster_ranges(network.cidr)
        pool = {"name": POOL, "mode": "System", "type": "VirtualMachineScaleSets", "count": planned.spec["node_count"], "vmSize": planned.spec["machine_type"],
                "osType": "Linux", "osSKU": "AzureLinux3", "osDiskSizeGB": spec.node_disk_gb, "osDiskType": "Managed", "vnetSubnetID": network.subnet,
                "enableNodePublicIP": not spec.private_nodes, "tags": tags}
        if ctx.zone:
            pool["availabilityZones"] = [ctx.zone]
        properties = {
            "dnsPrefix": planned.name[:54], "agentPoolProfiles": [pool],
            "nodeResourceGroup": node_group(control_id, planned.spec["cluster"]),
            "networkProfile": {"networkPlugin": "azure", "networkPluginMode": "overlay", "networkPolicy": "cilium", "networkDataplane": "cilium",
                               "outboundType": "userAssignedNATGateway", "loadBalancerSku": "standard", "podCidr": pods, "serviceCidr": services, "dnsServiceIP": dns},
            "aadProfile": {"managed": True, "enableAzureRBAC": True, "tenantID": self.tenant()}, "disableLocalAccounts": True,
            "oidcIssuerProfile": {"enabled": True}, "securityProfile": {"workloadIdentity": {"enabled": True}},
            "autoUpgradeProfile": {"upgradeChannel": "patch" if spec.kubernetes_version else "stable", "nodeOSUpgradeChannel": "NodeImage"},
        }
        if spec.kubernetes_version:
            properties["kubernetesVersion"] = spec.kubernetes_version
        if self.settings.cluster_authorized_networks:
            properties["apiServerAccessProfile"] = {"authorizedIPRanges": list(self.settings.cluster_authorized_networks)}
        return {"location": ctx.region, "tags": tags, "sku": {"name": "Base", "tier": "Standard"},
                "identity": {"type": "UserAssigned", "userAssignedIdentities": {control_id: {}}}, "properties": properties}

    def _create(self, cluster_id: str, body: dict) -> dict:
        for attempt in range(NOT_YET[0]):
            try:
                return self.api.put(self.api.arm(cluster_id), body, VERSION, timeout=self.ceiling("cluster"))
            except InfrastructureError as exc:
                if exc.details.get("reason") not in LINKED or attempt == NOT_YET[0] - 1:
                    raise
                event("cluster_role_not_yet", cluster=cluster_id.rsplit("/", 1)[-1], attempt=attempt + 1)
                self.api.sleep(NOT_YET[1])

    def _credentials(self, cluster_id: str) -> tuple[str, str]:
        """The API server's address and CA from the cluster's user kubeconfig (it holds no secret: sign-in is Entra's)."""
        answer = self.api.post(self.api.arm(f"{cluster_id}/listClusterUserCredential"), VERSION) or {}
        configs = answer.get("kubeconfigs") or []
        if not configs or not configs[0].get("value"):
            raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"AKS gave no kubeconfig for {cluster_id.rsplit('/', 1)[-1]}")
        return kubeconfig_endpoint(configs[0]["value"])

    # ---- the identities around the cluster
    def _pull(self, kubelet: str | None, registries: list[str], ctx, give: bool) -> None:
        """Grant (or take back) Repository Reader under each named prefix of the operator's registry to the kubelet identity."""
        if not kubelet or not self.settings.registry:
            return
        targets = []
        for entry in registries:
            login, _, prefix = entry.partition("/")
            if not PREFIX.fullmatch(prefix):
                raise InfrastructureError(ErrorCode.INVALID_CLUSTER_CONFIGURATION, f"cluster.registries: {entry!r} names no repository prefix")
            targets.append((self.settings.registry, "registry", prefix, login))
        if give:
            registry = self.registries._registry(self.settings.registry)
            if registry is None:
                raise InfrastructureError(ErrorCode.NOT_FOUND, f"The operator's registry {self.settings.registry.rsplit('/', 1)[-1]} does not exist")
            served = self.registries.login_server(registry)
            wrong = [f"{login}/{prefix}" for _, _, prefix, login in targets if login != served]
            if wrong:
                raise InfrastructureError(ErrorCode.INVALID_CLUSTER_CONFIGURATION, f"cluster.registries: {', '.join(wrong)} is not in the operator's registry, "
                                          f"{served}", {"field": "cluster.registries"})
            self.access.grant([target[:3] for target in targets], {"id": kubelet, "type": "ServicePrincipal"}, "use",
                              f"cloudinfra request {ctx.request_id} ({ctx.name}): the nodes of a cluster")
        else:
            self.access.revoke([target[:3] for target in targets], {"id": kubelet, "type": "ServicePrincipal"})

    def _federate(self, identities: list[dict], cluster_id: str, issuer: str | None, namespace: str, name: str) -> None:
        """A federated credential on each of the request's identities for the service account of its name, one at a time
        (Azure refuses concurrent writes of an identity's credentials)."""
        if not identities:
            return
        if not issuer:
            raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"AKS gave cluster {name} no OIDC issuer")
        group = cluster_id.split("/providers/", 1)[0]
        for identity in identities:
            self.api.put(self.api.arm(f"{group}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{identity['name']}/federatedIdentityCredentials/{name}"),
                         {"properties": {"issuer": issuer, "subject": f"system:serviceaccount:{namespace}:{identity['account']}",
                                         "audiences": [FEDERATED_AUDIENCE]}}, IDENTITY_VERSION, timeout=self.ceiling("identity"))

    def _unfederate(self, resource: ClusterResource) -> None:
        """Every federated credential named after the cluster on the identities of its group: they name its issuer, which
        goes with it, and an identity holds 20 at most."""
        group = resource.provider_id.split("/providers/", 1)[0]
        try:
            identities = self.api.list(self.api.arm(f"{group}/providers/Microsoft.ManagedIdentity/userAssignedIdentities"), IDENTITY_VERSION)
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return
            raise
        for identity in identities:
            if str(identity.get("name", "")).endswith("-control") or "/federatedidentitycredentials/" in str(identity.get("id", "")).lower():
                continue
            self.api.delete(self.api.arm(f"{identity['id']}/federatedIdentityCredentials/{resource.name}"), IDENTITY_VERSION, timeout=self.ceiling("identity"))

    # ---- read, delete
    def _view(self, resource: ClusterResource, found: dict) -> ClusterResource:
        properties = found.get("properties") or {}
        state = str(properties.get("provisioningState") or "").lower()
        power = str((properties.get("powerState") or {}).get("code") or "").lower()
        status = ResourceStatus.STOPPED if power == "stopped" and state == "succeeded" else STATES.get(state, ResourceStatus.UNKNOWN)
        pool = (properties.get("agentPoolProfiles") or [{}])[0]
        return resource.model_copy(update={"status": status, "endpoint": resource.endpoint or properties.get("fqdn"),
                                           "version": properties.get("currentKubernetesVersion") or properties.get("kubernetesVersion") or resource.version,
                                           "node_count": int(pool.get("count") or resource.node_count)})

    def get(self, resource: ClusterResource) -> ClusterResource:
        found = self.api.find(self.api.arm(resource.provider_id), VERSION)
        if found is None:
            return resource.model_copy(update={"status": ResourceStatus.DELETED, "endpoint": None})
        return self._view(resource, found)

    def delete(self, resource: ClusterResource, ctx) -> None:
        """The kubelet's registry grants and the federated credentials first (both outlive the cluster otherwise), then the
        cluster, then the control plane's role and identity."""
        found = self.api.find(self.api.arm(resource.provider_id), VERSION)
        kubelet = resource.node_service_account or kubelet_of(found)
        if resource.registries:
            self._pull(kubelet, resource.registries, ctx, give=False)
        self._unfederate(resource)
        if found is not None:
            self.api.delete(self.api.arm(resource.provider_id), VERSION, timeout=self.ceiling("cluster"))
        if resource.subnet:  # after the nodes: nothing uses it any more
            self.network.remove_rule(pods_rule(resource.subnet, resource.name))
        control_id = control_identity(resource.provider_id)
        control = self.api.find(self.api.arm(control_id), IDENTITY_VERSION)
        if control is None:
            return
        principal = (control.get("properties") or {}).get("principalId")
        if principal and resource.subnet:
            self.access.unassign(resource.subnet, {"id": principal, "type": "ServicePrincipal"}, NETWORK_CONTRIBUTOR)
        self.api.delete(self.api.arm(control_id), IDENTITY_VERSION, timeout=self.ceiling("identity"))

    # ---- what a member's grant targets
    @staticmethod
    def targets(resource: ClusterResource) -> list[tuple]:
        """The cluster's scope (its user kubeconfig) and its request's namespace (Azure RBAC for Kubernetes)."""
        found = [(resource.provider_id, "cluster")]
        if resource.namespace:
            found.append((f"{resource.provider_id}/namespaces/{resource.namespace}", "namespace"))
        return found
