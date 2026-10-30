from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_warehouse(region: Annotated[str, field("create_warehouse", "region")], provider: Annotated[str, field("create_warehouse", "provider")] = "gcp",
                         table_expiration_days: Annotated[int | None, field("create_warehouse", "table_expiration_days")] = None,
                         name: Annotated[str | None, field("create_warehouse", "name")] = None,
                         labels: Annotated[dict[str, str] | None, field("create_warehouse", "labels")] = None, wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_warehouse(identity(), region=region, table_expiration_days=table_expiration_days, provider=provider, name=name, labels=labels,
                                             wait_seconds=wait_seconds)

    def get_warehouse(resource_id: Annotated[str, field("get_warehouse", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.warehouse)

    def delete_warehouse(resource_id: Annotated[str, field("delete_warehouse", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.warehouse, wait_seconds)

    def create_nosql(region: Annotated[str, field("create_nosql", "region")], provider: Annotated[str, field("create_nosql", "provider")] = "gcp",
                     name: Annotated[str | None, field("create_nosql", "name")] = None, labels: Annotated[dict[str, str] | None, field("create_nosql", "labels")] = None,
                     wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_nosql(identity(), region=region, provider=provider, name=name, labels=labels, wait_seconds=wait_seconds)

    def get_nosql(resource_id: Annotated[str, field("get_nosql", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.nosql)

    def delete_nosql(resource_id: Annotated[str, field("delete_nosql", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.nosql, wait_seconds)

    for fn in (create_warehouse, get_warehouse, delete_warehouse, create_nosql, get_nosql, delete_nosql):
        register(mcp, wrap, fn)
