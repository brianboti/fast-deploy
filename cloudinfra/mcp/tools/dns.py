from typing import Annotated

from ...models.request import DnsRecordRequest
from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_dns(region: Annotated[str, field("create_dns", "region")], zone: Annotated[str, field("create_dns", "zone")],
                   provider: Annotated[str, field("create_dns", "provider")] = "gcp", records: Annotated[list[DnsRecordRequest] | None, field("create_dns", "records")] = None,
                   name: Annotated[str | None, field("create_dns", "name")] = None, labels: Annotated[dict[str, str] | None, field("create_dns", "labels")] = None,
                   wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_dns(identity(), region=region, zone=zone, provider=provider, records=records or [], name=name, labels=labels, wait_seconds=wait_seconds)

    def get_dns(resource_id: Annotated[str, field("get_dns", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.dns)

    def delete_dns(resource_id: Annotated[str, field("delete_dns", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.dns, wait_seconds)

    for fn in (create_dns, get_dns, delete_dns):
        register(mcp, wrap, fn)
