"""The MCP tools: one function per catalog record, registered with the record's title, description and safety hint."""
from typing import Annotated, Any

import pydantic

from .catalog import BY_NAME
from .models import ReleaseRequest

Result = dict[str, Any]
Wait = Annotated[int, pydantic.Field(ge=0, le=45, description=BY_NAME["deploy_release"].describe("wait_seconds"))]


def field(tool: str, name: str):
    return pydantic.Field(description=BY_NAME[tool].describe(name))


def register(mcp, delivery, wrap, identity, version: str) -> None:
    def plan_release(release: Annotated[ReleaseRequest, field("plan_release", "release")]) -> Result:
        return delivery.plan(release, identity())

    def deploy_release(release: Annotated[ReleaseRequest, field("deploy_release", "release")], wait_seconds: Wait = 0) -> Result:
        return delivery.deploy(release, identity(), wait_seconds)

    def approve_release(release_id: Annotated[str, field("approve_release", "release_id")], wait_seconds: Wait = 0) -> Result:
        return delivery.approve(release_id, identity(), wait_seconds)

    def get_release(release_id: Annotated[str, field("get_release", "release_id")]) -> Result:
        return delivery.get(release_id, identity())

    def list_releases(request_id: Annotated[str | None, field("list_releases", "request_id")] = None,
                      environment: Annotated[str | None, field("list_releases", "environment")] = None,
                      limit: Annotated[int, pydantic.Field(ge=1, le=200, description=BY_NAME["list_releases"].describe("limit"))] = 50) -> Result:
        releases = delivery.list_releases(identity(), request_id, environment, limit)
        return {"count": len(releases), "releases": [item.model_dump(mode="json") for item in releases]}

    def get_running(request_id: Annotated[str, field("get_running", "request_id")], live: Annotated[bool, field("get_running", "live")] = False) -> Result:
        return delivery.running(request_id, identity(), live)

    def rollback_release(release_id: Annotated[str, field("rollback_release", "release_id")], wait_seconds: Wait = 0) -> Result:
        return delivery.rollback(release_id, identity(), wait_seconds)

    def remove_release(release_id: Annotated[str, field("remove_release", "release_id")], wait_seconds: Wait = 0) -> Result:
        return delivery.remove(release_id, identity(), wait_seconds)

    def run_release(release_id: Annotated[str, field("run_release", "release_id")], wait_seconds: Wait = 0) -> Result:
        return delivery.start_run(release_id, identity(), wait_seconds)

    def get_release_run(release_id: Annotated[str, field("get_release_run", "release_id")], execution: Annotated[str, field("get_release_run", "execution")]) -> Result:
        return delivery.get_run(release_id, execution, identity())

    def get_service_status() -> Result:
        return delivery.service_status(version)

    for fn in (plan_release, deploy_release, approve_release, get_release, list_releases, get_running, rollback_release, remove_release, run_release, get_release_run,
               get_service_status):
        spec = BY_NAME[fn.__name__]
        mcp.tool(name=spec.name, title=spec.title, description=spec.model_description, annotations=spec.annotations, structured_output=True)(wrap(fn))
