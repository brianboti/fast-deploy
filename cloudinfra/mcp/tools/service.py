from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_service(region: Annotated[str, field("create_service", "region")], provider: Annotated[str, field("create_service", "provider")] = "gcp",
                       cpu: Annotated[int, field("create_service", "cpu")] = 1, memory_mb: Annotated[int, field("create_service", "memory_mb")] = 512,
                       min_instances: Annotated[int, field("create_service", "min_instances")] = 0, max_instances: Annotated[int, field("create_service", "max_instances")] = 10,
                       concurrency: Annotated[int, field("create_service", "concurrency")] = 80, timeout_seconds: Annotated[int, field("create_service", "timeout_seconds")] = 300,
                       ingress: Annotated[str, field("create_service", "ingress")] = "internal_and_load_balancer", public: Annotated[bool, field("create_service", "public")] = False,
                       network_id: Annotated[str | None, field("create_service", "network_id")] = None, egress: Annotated[str, field("create_service", "egress")] = "private_ranges",
                       name: Annotated[str | None, field("create_service", "name")] = None, labels: Annotated[dict[str, str] | None, field("create_service", "labels")] = None,
                       wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_service(identity(), region=region, provider=provider, cpu=cpu, memory_mb=memory_mb, min_instances=min_instances, max_instances=max_instances,
                                           concurrency=concurrency, timeout_seconds=timeout_seconds, ingress=ingress, public=public, network_id=network_id, egress=egress,
                                           name=name, labels=labels, wait_seconds=wait_seconds)

    def get_service(resource_id: Annotated[str, field("get_service", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.service)

    def delete_service(resource_id: Annotated[str, field("delete_service", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.service, wait_seconds)

    for fn in (create_service, get_service, delete_service):
        register(mcp, wrap, fn)
