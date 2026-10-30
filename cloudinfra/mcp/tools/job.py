from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_job(region: Annotated[str, field("create_job", "region")], provider: Annotated[str, field("create_job", "provider")] = "gcp",
                   cpu: Annotated[int, field("create_job", "cpu")] = 1, memory_mb: Annotated[int, field("create_job", "memory_mb")] = 512,
                   tasks: Annotated[int, field("create_job", "tasks")] = 1, parallelism: Annotated[int, field("create_job", "parallelism")] = 0,
                   max_retries: Annotated[int, field("create_job", "max_retries")] = 3, task_timeout_seconds: Annotated[int, field("create_job", "task_timeout_seconds")] = 600,
                   network_id: Annotated[str | None, field("create_job", "network_id")] = None, egress: Annotated[str, field("create_job", "egress")] = "private_ranges",
                   schedule: Annotated[str | None, field("create_job", "schedule")] = None, time_zone: Annotated[str, field("create_job", "time_zone")] = "UTC",
                   name: Annotated[str | None, field("create_job", "name")] = None, labels: Annotated[dict[str, str] | None, field("create_job", "labels")] = None,
                   wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_job(identity(), region=region, provider=provider, cpu=cpu, memory_mb=memory_mb, tasks=tasks, parallelism=parallelism,
                                       max_retries=max_retries, task_timeout_seconds=task_timeout_seconds, network_id=network_id, egress=egress, schedule=schedule,
                                       time_zone=time_zone, name=name, labels=labels, wait_seconds=wait_seconds)

    def get_job(resource_id: Annotated[str, field("get_job", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.job)

    def delete_job(resource_id: Annotated[str, field("delete_job", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.job, wait_seconds)

    for fn in (create_job, get_job, delete_job):
        register(mcp, wrap, fn)
