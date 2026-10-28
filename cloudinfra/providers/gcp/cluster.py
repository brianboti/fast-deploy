"""Kubernetes Engine clusters behind the generic cluster primitives: one zonal cluster with one node pool, VPC-native,
shielded nodes, workload identity on, a release channel for versions. Credentials never come from this service."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import ClusterResource, ResourceStatus
from . import translate
from .api import COMPUTE, CONTAINER

STATUS = {"PROVISIONING": ResourceStatus.CREATING, "RUNNING": ResourceStatus.READY, "RECONCILING": ResourceStatus.UPDATING, "STOPPING": ResourceStatus.DELETING,
          "ERROR": ResourceStatus.FAILED, "DEGRADED": ResourceStatus.FAILED}
NODE_SCOPES = ["https://www.googleapis.com/auth/logging.write", "https://www.googleapis.com/auth/monitoring", "https://www.googleapis.com/auth/devstorage.read_only"]


class GcpCluster:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def _clusters(self, location):
        return f"{CONTAINER}/projects/{self.api.project}/locations/{location}/clusters"

    def create(self, spec, planned, ctx, network) -> ClusterResource:
        location = planned.spec["location"]
        if network is not None:
            net, subnet = network.provider_id, network.subnet
        else:
            net = f"{COMPUTE}/projects/{self.api.project}/global/networks/{self.settings.default_network}"
            subnet = f"{COMPUTE}/projects/{self.api.project}/regions/{ctx.region}/subnetworks/{self.settings.default_subnet}" if self.settings.default_subnet else None
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
        operation = self.api.post(self._clusters(location), json={"cluster": cluster})
        self.api.wait_container(operation)
        resource = ClusterResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region, zone=location,
                                   provider_id=f"{self._clusters(location)}/{planned.name}", location=location, node_count=spec.node_count, machine_type=planned.spec["machine_type"],
                                   node_pool="default-pool", network=network.id if network else None, labels=translate.labels(ctx))
        return self.get(resource)

    def get(self, resource: ClusterResource) -> ClusterResource:
        try:
            cluster = self.api.get(f"{self._clusters(resource.location)}/{resource.name}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED, "endpoint": None})
            raise
        return resource.model_copy(update={"status": STATUS.get(cluster.get("status", ""), ResourceStatus.UNKNOWN), "endpoint": cluster.get("endpoint"),
                                           "version": cluster.get("currentMasterVersion"), "node_count": int(cluster.get("currentNodeCount") or resource.node_count),
                                           "created_at": resource.created_at or cluster.get("createTime")})

    def delete(self, resource: ClusterResource, ctx) -> None:
        try:
            self.api.wait_container(self.api.delete(f"{self._clusters(resource.location)}/{resource.name}"))
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
