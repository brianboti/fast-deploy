from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def get_encryption_key(resource_id: Annotated[str, field("get_encryption_key", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.encryption_key)

    def delete_encryption_key(resource_id: Annotated[str, field("delete_encryption_key", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.encryption_key, wait_seconds)

    for fn in (get_encryption_key, delete_encryption_key):
        register(mcp, wrap, fn)
