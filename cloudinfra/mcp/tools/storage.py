from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_storage(region: Annotated[str, field("create_storage", "region")], size_gb: Annotated[int, field("create_storage", "size_gb")],
                       provider: Annotated[str, field("create_storage", "provider")] = "gcp", type: Annotated[str, field("create_storage", "type")] = "persistent",
                       performance: Annotated[str, field("create_storage", "performance")] = "balanced", zone: Annotated[str | None, field("create_storage", "zone")] = None,
                       snapshot_keep_days: Annotated[int | None, field("create_storage", "snapshot_keep_days")] = None,
                       name: Annotated[str | None, field("create_storage", "name")] = None, labels: Annotated[dict[str, str] | None, field("create_storage", "labels")] = None,
                       wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_storage(identity(), region=region, size_gb=size_gb, provider=provider, type=type, performance=performance, zone=zone, name=name, labels=labels,
                                           wait_seconds=wait_seconds, snapshot_keep_days=snapshot_keep_days)

    def attach_storage(storage_id: Annotated[str, field("attach_storage", "storage_id")], compute_id: Annotated[str, field("attach_storage", "compute_id")]) -> Result:
        return orchestrator.attach_storage(storage_id, compute_id, identity())

    def get_storage(resource_id: Annotated[str, field("get_storage", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.storage)

    def delete_storage(resource_id: Annotated[str, field("delete_storage", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.storage, wait_seconds)

    for fn in (create_storage, attach_storage, get_storage, delete_storage):
        register(mcp, wrap, fn)
