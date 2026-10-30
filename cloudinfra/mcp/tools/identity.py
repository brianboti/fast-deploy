from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_identity(region: Annotated[str, field("create_identity", "region")], provider: Annotated[str, field("create_identity", "provider")] = "gcp",
                        level: Annotated[str, field("create_identity", "level")] = "use", name: Annotated[str | None, field("create_identity", "name")] = None,
                        labels: Annotated[dict[str, str] | None, field("create_identity", "labels")] = None, wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_identity(identity(), region=region, level=level, provider=provider, name=name, labels=labels, wait_seconds=wait_seconds)

    def get_identity(resource_id: Annotated[str, field("get_identity", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.identity)

    def delete_identity(resource_id: Annotated[str, field("delete_identity", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.identity, wait_seconds)

    for fn in (create_identity, get_identity, delete_identity):
        register(mcp, wrap, fn)
