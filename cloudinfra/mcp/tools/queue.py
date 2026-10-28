from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_queue(region: Annotated[str, field("create_queue", "region")], queue_name: Annotated[str, field("create_queue", "queue_name")],
                     provider: Annotated[str, field("create_queue", "provider")] = "gcp", retention_hours: Annotated[int, field("create_queue", "retention_hours")] = 168,
                     subscription: Annotated[bool, field("create_queue", "subscription")] = True, ack_deadline_seconds: Annotated[int, field("create_queue", "ack_deadline_seconds")] = 20,
                     name: Annotated[str | None, field("create_queue", "name")] = None, labels: Annotated[dict[str, str] | None, field("create_queue", "labels")] = None,
                     wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_queue(identity(), region=region, queue_name=queue_name, provider=provider, retention_hours=retention_hours, subscription=subscription,
                                         ack_deadline_seconds=ack_deadline_seconds, name=name, labels=labels, wait_seconds=wait_seconds)

    def get_queue(resource_id: Annotated[str, field("get_queue", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.queue)

    def delete_queue(resource_id: Annotated[str, field("delete_queue", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.queue, wait_seconds)

    for fn in (create_queue, get_queue, delete_queue):
        register(mcp, wrap, fn)
