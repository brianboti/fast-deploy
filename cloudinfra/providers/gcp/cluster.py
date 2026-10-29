"""Kubernetes Engine clusters behind the generic cluster primitives: one zonal cluster with one node pool, VPC-native,
shielded nodes, workload identity on, a release channel for versions. Credentials never come from this service.

Each cluster gets one namespace, named after the request, and access to what a request created is granted there: a
member at `use` is bound to the built-in ClusterRole view, at `admin` to edit, through RoleBindings in that namespace
only (docs/DELIVERY-SPEC.md, phase 2). Fetching credentials still needs the project-level container.clusterViewer. An
admission policy keeps that namespace from opening a public address of its own (phase 3); the front door, when asked
for, is this service's: a reserved global address, a Google-managed certificate, and one Gateway in its own namespace.
A Gateway's load balancer is GKE's to make and delete, so a cluster with a front door loses its Gateway, and Google its
forwarding rules, before the cluster goes; otherwise the load balancer would outlive it.

The nodes run as an account made for the cluster (nodeaccount.py), allowed to pull from the request's registries."""
import hashlib

from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import ClusterResource, FrontDoor, ResourceStatus
from . import translate
from .api import COMPUTE, CONTAINER
from .kubernetes import CLUSTER_ROLES, DOOR, DOOR_NAMESPACE, GATEWAYS, ROUTES, TENANT, KubeApi, front_door, no_public_entry, routes_role
from .nodeaccount import NodeAccounts, account_id

# Gateway API's kinds appear a little after a cluster runs; a Gateway is tried again meanwhile.
GATEWAY_KINDS = (30, 10)
# GKE deletes a Gateway's load balancer in a few minutes; the cluster waits for its forwarding rules to go.
DOOR_CLOSING = (600, 15)

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
        door = None
        if spec.public is not None:  # https://docs.cloud.google.com/kubernetes-engine/docs/how-to/deploying-gateways
            cluster["networkConfig"] = {"gatewayApiConfig": {"channel": "CHANNEL_STANDARD"}}
            cluster["addonsConfig"] = {"httpLoadBalancing": {"disabled": False}}
            door = FrontDoor(hostnames=list(spec.public.hostnames), address_name=f"{planned.name}-door",
                             certificate_name=f"{planned.name}-door" if spec.public.hostnames else None)
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
                                   node_disk_gb=planned.spec.get("node_disk_gb"), status=ResourceStatus.CREATING, node_service_account=account, registries=registries, public=door,
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
        for collection, body in [*no_public_entry(labels), routes_role(labels)]:  # before the namespace, so no moment exists without them
            kube.put(collection, body)
        kube.ensure_namespace(ctx.name, {**labels, **TENANT})
        resource = resource.model_copy(update={"namespace": ctx.name})
        if door is not None:
            resource = resource.model_copy(update={"public": self._open(door, kube, ctx, labels)})
        return resource

    # ---- the front door
    def _global(self, collection: str, body: dict) -> dict:
        """Create a global Compute Engine object (one an earlier attempt made is kept) and read it back."""
        try:
            self.api.wait_compute(self.api.post(f"{COMPUTE}/projects/{self.api.project}/global/{collection}", json=body))
        except InfrastructureError as exc:
            if exc.code != ErrorCode.PROVIDER_CONFLICT:
                raise
        return self.api.get(f"{COMPUTE}/projects/{self.api.project}/global/{collection}/{body['name']}")

    def _open(self, door: FrontDoor, kube, ctx, labels) -> FrontDoor:
        address = self._global("addresses", {"name": door.address_name, "addressType": "EXTERNAL", "ipVersion": "IPV4", "description": translate.description(ctx)})
        certificate = None
        if door.certificate_name:
            found = self._global("sslCertificates", {"name": door.certificate_name, "type": "MANAGED", "managed": {"domains": door.hostnames},
                                                     "description": translate.description(ctx)})
            certificate = (found.get("managed") or {}).get("status")
        kube.ensure_namespace(DOOR_NAMESPACE, labels)  # the platform's, not a tenant's: the admission policy does not reach it
        for collection, body in front_door(ctx.name, door.address_name, door.certificate_name, labels):
            for attempt in range(GATEWAY_KINDS[0]):
                try:
                    kube.put(collection, body)
                    break
                except InfrastructureError as exc:
                    if exc.code != ErrorCode.NOT_FOUND or attempt == GATEWAY_KINDS[0] - 1:
                        raise
                    self.api.sleep(GATEWAY_KINDS[1])
        return door.model_copy(update={"address": address.get("address"), "certificate": certificate})

    def _close(self, resource: ClusterResource) -> None:
        """The Gateway first, then GKE's forwarding rules on the address, then the cluster may go."""
        door = resource.public
        resource = self._reachable(resource)  # one recorded as Google accepted it has no address yet
        if resource.status != ResourceStatus.DELETED and resource.endpoint and resource.ca_certificate:
            kube = self.kube(resource.endpoint, resource.ca_certificate)
            kube.delete(f"{ROUTES}/to-https")
            kube.delete(f"{GATEWAYS}/{DOOR}")
        address = door.address
        if not address:  # a front door that failed half way never recorded it, though a Gateway may already use it
            try:
                address = self.api.get(f"{COMPUTE}/projects/{self.api.project}/global/addresses/{door.address_name}").get("address")
            except InfrastructureError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
                return
        started = self.api.clock()
        while self.api.clock() - started < DOOR_CLOSING[0]:
            rules = self.api.get(f"{COMPUTE}/projects/{self.api.project}/global/forwardingRules", filter=f'IPAddress="{address}"').get("items", [])
            if not [rule for rule in rules if rule.get("IPAddress") == address]:
                return
            self.api.sleep(DOOR_CLOSING[1])
        raise InfrastructureError(ErrorCode.PROVIDER_UNAVAILABLE, f"GKE has not deleted the front door's load balancer on {address} after {DOOR_CLOSING[0]} seconds",
                                  {"address": address})

    def _release(self, door: FrontDoor) -> None:
        for collection, name in (("sslCertificates", door.certificate_name), ("addresses", door.address_name)):
            if not name:
                continue
            try:
                self.api.wait_compute(self.api.delete(f"{COMPUTE}/projects/{self.api.project}/global/{collection}/{name}"))
            except InfrastructureError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise

    def get(self, resource: ClusterResource) -> ClusterResource:
        try:
            cluster = self.api.get(f"{self._clusters(resource.location)}/{resource.name}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED, "endpoint": None})
            raise
        door = resource.public
        if door is not None and door.certificate_name:
            try:
                found = self.api.get(f"{COMPUTE}/projects/{self.api.project}/global/sslCertificates/{door.certificate_name}")
                door = door.model_copy(update={"certificate": (found.get("managed") or {}).get("status")})
            except InfrastructureError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
        return resource.model_copy(update={"public": door, "status": STATUS.get(cluster.get("status", ""), ResourceStatus.UNKNOWN), "endpoint": cluster.get("endpoint"),
                                           "ca_certificate": (cluster.get("masterAuth") or {}).get("clusterCaCertificate") or resource.ca_certificate,
                                           "version": cluster.get("currentMasterVersion"), "node_count": int(cluster.get("currentNodeCount") or resource.node_count),
                                           "created_at": resource.created_at or cluster.get("createTime")})

    def delete(self, resource: ClusterResource, ctx) -> None:
        if resource.public is not None:
            self._close(resource)
        try:
            self.api.wait_container(self.api.delete(f"{self._clusters(resource.location)}/{resource.name}"))
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
        if resource.public is not None:  # no load balancer uses them any more
            self._release(resource.public)
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
