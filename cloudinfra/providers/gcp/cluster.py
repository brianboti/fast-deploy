"""Kubernetes Engine clusters behind the generic cluster primitives: one zonal cluster with one node pool, VPC-native,
shielded nodes, workload identity on, a release channel for versions. Credentials never come from this service.

Each cluster gets one namespace, named after the request, and access to what a request created is granted there: a
member at `use` is bound to the built-in ClusterRole view, at `admin` to edit, through RoleBindings in that namespace
only (docs/DELIVERY-SPEC.md, phase 2). Fetching credentials still needs the project-level container.clusterViewer. An
admission policy keeps that namespace from opening a public address of its own (phase 3).

The nodes run as an account made for the cluster (nodeaccount.py), allowed to pull from the request's registries."""
import hashlib

from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import ClusterResource, ResourceStatus
from . import translate
from .api import CONTAINER
from .kubernetes import CLUSTER_ROLES, TENANT, KubeApi, no_public_entry
from .nodeaccount import NodeAccounts, account_id

STATUS = {"PROVISIONING": ResourceStatus.CREATING, "RUNNING": ResourceStatus.READY, "RECONCILING": ResourceStatus.UPDATING, "STOPPING": ResourceStatus.DELETING,
          "ERROR": ResourceStatus.FAILED, "DEGRADED": ResourceStatus.FAILED}
NODE_SCOPES = ["https://www.googleapis.com/auth/logging.write", "https://www.googleapis.com/auth/monitoring", "https://www.googleapis.com/auth/devstorage.read_only"]


def binding_name(member: str, level: str) -> str:
    """One RoleBinding per member and level, named without the member's address."""
    return f"cloudinfra-{level}-{hashlib.sha256(member.lower().encode()).hexdigest()[:12]}"


class GcpCluster:
    def __init__(self, api, settings, kube=None):
        self.api, self.settings, self.accounts = api, settings, NodeAccounts(api)
        self.kube = kube or (lambda endpoint, ca_certificate: KubeApi(endpoint, ca_certificate, api.token))

    def _clusters(self, location):
        return f"{CONTAINER}/projects/{self.api.project}/locations/{location}/clusters"

    def create(self, spec, planned, ctx, network) -> ClusterResource:
        location = planned.spec["location"]
        # GKE takes the network and subnetwork by name only: it refuses a self link as a malformed value (found on a real
        # project on 2026-10-29; https://docs.cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.locations.clusters)
        if network is not None:
            net, subnet = network.provider_id.rsplit("/", 1)[-1], network.subnet.rsplit("/", 1)[-1] if network.subnet else None
        else:
            net, subnet = self.settings.default_network, self.settings.default_subnet
        cluster = {
            "name": planned.name, "description": translate.description(ctx), "network": net, "resourceLabels": translate.labels(ctx),
            "nodePools": [{"name": "default-pool", "initialNodeCount": spec.node_count,
                           "config": {"machineType": planned.spec["machine_type"], "diskSizeGb": spec.node_disk_gb, "diskType": "pd-balanced", "oauthScopes": NODE_SCOPES,
                                      "labels": translate.labels(ctx), "shieldedInstanceConfig": {"enableSecureBoot": True, "enableIntegrityMonitoring": True}}}],
            "ipAllocationPolicy": {"useIpAliases": True}, "releaseChannel": {"channel": "REGULAR"}, "shieldedNodes": {"enabled": True},
            "workloadIdentityConfig": {"workloadPool": f"{self.api.project}.svc.id.goog"},
        }
        if subnet:
            cluster["subnetwork"] = subnet
        if spec.kubernetes_version:
            cluster["initialClusterVersion"] = spec.kubernetes_version
        if spec.private_nodes:
            cluster["privateClusterConfig"] = {"enablePrivateNodes": True, "enablePrivateEndpoint": False}
        registries, account = list(spec.registries), None
        if self.settings.cluster_node_accounts:
            account = self.accounts.create(account_id(ctx.request_id, planned.spec.get("cluster", "cluster")), f"Nodes of cluster {planned.name}. {translate.description(ctx)}")
            try:
                self.accounts.bind(account, registries)
            except InfrastructureError:
                self.accounts.forget(account, registries)
                raise
            cluster["nodePools"][0]["config"]["serviceAccount"] = account
        resource = ClusterResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region, zone=location,
                                   provider_id=f"{self._clusters(location)}/{planned.name}", location=location, node_count=spec.node_count, machine_type=planned.spec["machine_type"],
                                   node_disk_gb=planned.spec.get("node_disk_gb"), status=ResourceStatus.CREATING, node_service_account=account, registries=registries,
                                   node_pool="default-pool", network=network.id if network else None, labels=translate.labels(ctx))
        try:
            operation = self.api.post(self._clusters(location), json={"cluster": cluster})
        except InfrastructureError:
            if account:  # Google never took the cluster, so nothing else will delete its account
                self.accounts.forget(account, registries)
            raise
        ctx.accepted(resource)  # known from here: a failed wait, or a failed namespace, still deletes the cluster
        self.api.wait_container(operation)
        resource = self.get(resource)
        kube = self.kube(resource.endpoint, resource.ca_certificate)
        labels = {"managed-by": "cloudinfra", "cloudinfra-request": ctx.request_id}
        for collection, body in no_public_entry(labels):  # before the namespace, so no moment exists without it
            kube.put(collection, body)
        kube.ensure_namespace(ctx.name, {**labels, **TENANT})
        return resource.model_copy(update={"namespace": ctx.name})

    def get(self, resource: ClusterResource) -> ClusterResource:
        try:
            cluster = self.api.get(f"{self._clusters(resource.location)}/{resource.name}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED, "endpoint": None})
            raise
        return resource.model_copy(update={"status": STATUS.get(cluster.get("status", ""), ResourceStatus.UNKNOWN), "endpoint": cluster.get("endpoint"),
                                           "ca_certificate": (cluster.get("masterAuth") or {}).get("clusterCaCertificate") or resource.ca_certificate,
                                           "version": cluster.get("currentMasterVersion"), "node_count": int(cluster.get("currentNodeCount") or resource.node_count),
                                           "created_at": resource.created_at or cluster.get("createTime")})

    def delete(self, resource: ClusterResource, ctx) -> None:
        try:
            self.api.wait_container(self.api.delete(f"{self._clusters(resource.location)}/{resource.name}"))
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
        if resource.node_service_account:  # after the nodes are gone
            self.accounts.remove(resource.node_service_account, resource.registries)

    def _reachable(self, resource: ClusterResource) -> ClusterResource:
        if resource.endpoint and resource.ca_certificate:
            return resource
        return self.get(resource)

    def grant(self, resource: ClusterResource, member: str, level: str) -> list[str]:
        """Bind the member to the level's ClusterRole in the request's namespace, and drop its binding at the other level."""
        resource = self._reachable(resource)
        if resource.status == ResourceStatus.DELETED or not resource.endpoint:
            raise InfrastructureError(ErrorCode.NOT_FOUND, f"Cluster {resource.name} does not exist", {"resource": resource.id})
        namespace = resource.namespace or resource.labels.get("cloudinfra-name") or resource.name
        kube = self.kube(resource.endpoint, resource.ca_certificate)
        labels = {"managed-by": "cloudinfra", "cloudinfra-request": resource.request_id}
        if not resource.namespace:  # a cluster made before namespaces were
            kube.ensure_namespace(namespace, labels)
        kube.put_role_binding(namespace, binding_name(member, level), CLUSTER_ROLES[level], member, labels)
        for other in CLUSTER_ROLES:
            if other != level:
                kube.delete_role_binding(namespace, binding_name(member, other))
        return [f"kubernetes:{CLUSTER_ROLES[level]}"]

    def revoke(self, resource: ClusterResource, member: str) -> None:
        resource = self._reachable(resource)
        if resource.status == ResourceStatus.DELETED or not resource.endpoint:
            return  # the namespace and its bindings went with the cluster
        namespace = resource.namespace or resource.labels.get("cloudinfra-name") or resource.name
        kube = self.kube(resource.endpoint, resource.ca_certificate)
        for level in CLUSTER_ROLES:
            kube.delete_role_binding(namespace, binding_name(member, level))
