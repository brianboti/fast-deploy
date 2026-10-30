from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def get_log_export(resource_id: Annotated[str, field("get_log_export", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.log_export)

    def delete_log_export(resource_id: Annotated[str, field("delete_log_export", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.log_export, wait_seconds)

    for fn in (get_log_export, delete_log_export):
        register(mcp, wrap, fn)
