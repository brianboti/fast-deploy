from typing import Annotated

from ... import __version__
from ._common import Result, field, register


def tools(mcp, orchestrator, wrap, identity):
    def get_resource_health(resource_id: Annotated[str, field("get_resource_health", "resource_id")]) -> Result:
        return orchestrator.health(resource_id, identity())

    def get_resource_usage(resource_id: Annotated[str, field("get_resource_usage", "resource_id")], minutes: Annotated[int, field("get_resource_usage", "minutes")] = 60) -> Result:
        return orchestrator.usage(resource_id, minutes, identity())

    def get_resource_cost(request_id: Annotated[str | None, field("get_resource_cost", "request_id")] = None,
                          resource_id: Annotated[str | None, field("get_resource_cost", "resource_id")] = None) -> Result:
        return orchestrator.cost(identity(), request_id, resource_id)

    def get_operation(operation_id: Annotated[str, field("get_operation", "operation_id")]) -> Result:
        return orchestrator.operation(operation_id, identity())

    def get_service_status() -> Result:
        return orchestrator.service_status(__version__)

    for fn in (get_resource_health, get_resource_usage, get_resource_cost, get_operation, get_service_status):
        register(mcp, wrap, fn)
