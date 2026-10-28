from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_secret(region: Annotated[str, field("create_secret", "region")], secret_name: Annotated[str, field("create_secret", "secret_name")],
                      provider: Annotated[str, field("create_secret", "provider")] = "gcp", generate_value: Annotated[bool, field("create_secret", "generate_value")] = False,
                      name: Annotated[str | None, field("create_secret", "name")] = None, labels: Annotated[dict[str, str] | None, field("create_secret", "labels")] = None,
                      wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_secret(identity(), region=region, secret_name=secret_name, provider=provider, generate_value=generate_value, name=name, labels=labels,
                                          wait_seconds=wait_seconds)

    def get_secret(resource_id: Annotated[str, field("get_secret", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.secret)

    def delete_secret(resource_id: Annotated[str, field("delete_secret", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.secret, wait_seconds)

    for fn in (create_secret, get_secret, delete_secret):
        register(mcp, wrap, fn)
