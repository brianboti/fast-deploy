from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_registry(region: Annotated[str, field("create_registry", "region")], registry_name: Annotated[str, field("create_registry", "registry_name")],
                        provider: Annotated[str, field("create_registry", "provider")] = "gcp",
                        immutable_tags: Annotated[bool, field("create_registry", "immutable_tags")] = False, size_gb: Annotated[int, field("create_registry", "size_gb")] = 10,
                        name: Annotated[str | None, field("create_registry", "name")] = None, labels: Annotated[dict[str, str] | None, field("create_registry", "labels")] = None,
                        wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_registry(identity(), region=region, registry_name=registry_name, provider=provider, immutable_tags=immutable_tags, size_gb=size_gb,
                                            name=name, labels=labels, wait_seconds=wait_seconds)

    def get_registry(resource_id: Annotated[str, field("get_registry", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.registry)

    def delete_registry(resource_id: Annotated[str, field("delete_registry", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.registry, wait_seconds)

    for fn in (create_registry, get_registry, delete_registry):
        register(mcp, wrap, fn)
