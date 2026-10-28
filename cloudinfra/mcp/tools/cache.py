from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_cache(region: Annotated[str, field("create_cache", "region")], network_id: Annotated[str, field("create_cache", "network_id")],
                     provider: Annotated[str, field("create_cache", "provider")] = "gcp", memory_gb: Annotated[int, field("create_cache", "memory_gb")] = 1,
                     high_availability: Annotated[bool, field("create_cache", "high_availability")] = False, version: Annotated[str | None, field("create_cache", "version")] = None,
                     name: Annotated[str | None, field("create_cache", "name")] = None, labels: Annotated[dict[str, str] | None, field("create_cache", "labels")] = None,
                     wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_cache(identity(), region=region, provider=provider, memory_gb=memory_gb, high_availability=high_availability, version=version,
                                         network_id=network_id, name=name, labels=labels, wait_seconds=wait_seconds)

    def get_cache(resource_id: Annotated[str, field("get_cache", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.cache)

    def delete_cache(resource_id: Annotated[str, field("delete_cache", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.cache, wait_seconds)

    for fn in (create_cache, get_cache, delete_cache):
        register(mcp, wrap, fn)
