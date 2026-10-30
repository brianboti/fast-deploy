from typing import Annotated

from ...models.request import UptimeRequest
from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_monitor(region: Annotated[str, field("create_monitor", "region")], notify: Annotated[list[str], field("create_monitor", "notify")],
                       uptime: Annotated[list[UptimeRequest], field("create_monitor", "uptime")], provider: Annotated[str, field("create_monitor", "provider")] = "gcp",
                       name: Annotated[str | None, field("create_monitor", "name")] = None, labels: Annotated[dict[str, str] | None, field("create_monitor", "labels")] = None,
                       wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_monitor(identity(), region=region, notify=notify, uptime=uptime, provider=provider, name=name, labels=labels, wait_seconds=wait_seconds)

    def get_monitor(resource_id: Annotated[str, field("get_monitor", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.monitor)

    def delete_monitor(resource_id: Annotated[str, field("delete_monitor", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.monitor, wait_seconds)

    for fn in (create_monitor, get_monitor, delete_monitor):
        register(mcp, wrap, fn)
