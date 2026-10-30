from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_load_balancer(region: Annotated[str, field("create_load_balancer", "region")], backends: Annotated[list[str], field("create_load_balancer", "backends")],
                             provider: Annotated[str, field("create_load_balancer", "provider")] = "gcp", type: Annotated[str, field("create_load_balancer", "type")] = "http",
                             port: Annotated[int, field("create_load_balancer", "port")] = 80,
                             health_check_path: Annotated[str, field("create_load_balancer", "health_check_path")] = "/",
                             hostnames: Annotated[list[str] | None, field("create_load_balancer", "hostnames")] = None,
                             name: Annotated[str | None, field("create_load_balancer", "name")] = None,
                             labels: Annotated[dict[str, str] | None, field("create_load_balancer", "labels")] = None, wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_load_balancer(identity(), region=region, backends=backends, provider=provider, type=type, port=port, health_check_path=health_check_path,
                                                 hostnames=hostnames, name=name, labels=labels, wait_seconds=wait_seconds)

    def get_load_balancer(resource_id: Annotated[str, field("get_load_balancer", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.load_balancer)

    def delete_load_balancer(resource_id: Annotated[str, field("delete_load_balancer", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.load_balancer, wait_seconds)

    for fn in (create_load_balancer, get_load_balancer, delete_load_balancer):
        register(mcp, wrap, fn)
