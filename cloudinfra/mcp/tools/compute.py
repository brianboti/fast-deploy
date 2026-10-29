from typing import Annotated

from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_compute(region: Annotated[str, field("create_compute", "region")], cpu: Annotated[int, field("create_compute", "cpu")],
                       memory_gb: Annotated[int, field("create_compute", "memory_gb")], provider: Annotated[str, field("create_compute", "provider")] = "gcp",
                       count: Annotated[int, field("create_compute", "count")] = 1, os: Annotated[str | None, field("create_compute", "os")] = None,
                       profile: Annotated[str | None, field("create_compute", "profile")] = None,
                       boot_disk_gb: Annotated[int | None, field("create_compute", "boot_disk_gb")] = None,
                       public_ip: Annotated[bool, field("create_compute", "public_ip")] = False,
                       network_id: Annotated[str | None, field("create_compute", "network_id")] = None,
                       accelerator: Annotated[str | None, field("create_compute", "accelerator")] = None,
                       accelerator_count: Annotated[int, field("create_compute", "accelerator_count")] = 1,
                       autostop_idle_minutes: Annotated[int | None, field("create_compute", "autostop_idle_minutes")] = None,
                       max_run_hours: Annotated[int | None, field("create_compute", "max_run_hours")] = None,
                       lifetime_hours: Annotated[int | None, field("create_compute", "lifetime_hours")] = None,
                       zone: Annotated[str | None, field("create_compute", "zone")] = None,
                       name: Annotated[str | None, field("create_compute", "name")] = None, labels: Annotated[dict[str, str] | None, field("create_compute", "labels")] = None,
                       wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_compute(identity(), region=region, cpu=cpu, memory_gb=memory_gb, provider=provider, count=count, os=os, profile=profile,
                                           boot_disk_gb=boot_disk_gb, public_ip=public_ip, network_id=network_id, accelerator=accelerator, accelerator_count=accelerator_count,
                                           autostop_idle_minutes=autostop_idle_minutes, max_run_hours=max_run_hours, lifetime_hours=lifetime_hours, zone=zone, name=name,
                                           labels=labels, wait_seconds=wait_seconds)

    def get_compute(resource_id: Annotated[str, field("get_compute", "resource_id")]) -> Result:
        from ...models.resources import ResourceKind
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.compute)

    def list_compute(request_id: Annotated[str | None, field("list_compute", "request_id")] = None) -> Result:
        return orchestrator.list_compute(identity(), request_id)

    def resize_compute(resource_id: Annotated[str, field("resize_compute", "resource_id")], cpu: Annotated[int, field("resize_compute", "cpu")],
                       memory_gb: Annotated[int, field("resize_compute", "memory_gb")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.resize_compute(resource_id, cpu, memory_gb, identity(), wait_seconds)

    def delete_compute(resource_id: Annotated[str, field("delete_compute", "resource_id")], wait_seconds: Wait = 0) -> Result:
        from ...models.resources import ResourceKind
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.compute, wait_seconds)

    for fn in (create_compute, get_compute, list_compute, resize_compute, delete_compute):
        register(mcp, wrap, fn)
