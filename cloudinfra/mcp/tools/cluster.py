from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_cluster(region: Annotated[str, field("create_cluster", "region")], provider: Annotated[str, field("create_cluster", "provider")] = "gcp",
                       node_count: Annotated[int, field("create_cluster", "node_count")] = 1, node_cpu: Annotated[int, field("create_cluster", "node_cpu")] = 2,
                       node_memory_gb: Annotated[int, field("create_cluster", "node_memory_gb")] = 8, node_disk_gb: Annotated[int, field("create_cluster", "node_disk_gb")] = 50,
                       kubernetes_version: Annotated[str | None, field("create_cluster", "kubernetes_version")] = None,
                       private_nodes: Annotated[bool, field("create_cluster", "private_nodes")] = False, network_id: Annotated[str | None, field("create_cluster", "network_id")] = None,
                       zone: Annotated[str | None, field("create_cluster", "zone")] = None, name: Annotated[str | None, field("create_cluster", "name")] = None,
                       labels: Annotated[dict[str, str] | None, field("create_cluster", "labels")] = None, wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_cluster(identity(), region=region, provider=provider, node_count=node_count, node_cpu=node_cpu, node_memory_gb=node_memory_gb, node_disk_gb=node_disk_gb,
                                           kubernetes_version=kubernetes_version, private_nodes=private_nodes, network_id=network_id, zone=zone, name=name, labels=labels,
                                           wait_seconds=wait_seconds)

    def get_cluster(resource_id: Annotated[str, field("get_cluster", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.cluster)

    def delete_cluster(resource_id: Annotated[str, field("delete_cluster", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.cluster, wait_seconds)

    for fn in (create_cluster, get_cluster, delete_cluster):
        register(mcp, wrap, fn)
