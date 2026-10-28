from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_database(region: Annotated[str, field("create_database", "region")], provider: Annotated[str, field("create_database", "provider")] = "gcp",
                        engine: Annotated[str, field("create_database", "engine")] = "postgresql", version: Annotated[str | None, field("create_database", "version")] = None,
                        size: Annotated[str, field("create_database", "size")] = "small", storage_gb: Annotated[int, field("create_database", "storage_gb")] = 10,
                        high_availability: Annotated[bool, field("create_database", "high_availability")] = False,
                        public_access: Annotated[bool, field("create_database", "public_access")] = False,
                        database_name: Annotated[str, field("create_database", "database_name")] = "app", username: Annotated[str, field("create_database", "username")] = "app",
                        network_id: Annotated[str | None, field("create_database", "network_id")] = None, name: Annotated[str | None, field("create_database", "name")] = None,
                        labels: Annotated[dict[str, str] | None, field("create_database", "labels")] = None, wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_database(identity(), region=region, provider=provider, engine=engine, version=version, size=size, storage_gb=storage_gb,
                                            high_availability=high_availability, public_access=public_access, database_name=database_name, username=username,
                                            network_id=network_id, name=name, labels=labels, wait_seconds=wait_seconds)

    def get_database(resource_id: Annotated[str, field("get_database", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.database)

    def delete_database(resource_id: Annotated[str, field("delete_database", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.database, wait_seconds)

    for fn in (create_database, get_database, delete_database):
        register(mcp, wrap, fn)
