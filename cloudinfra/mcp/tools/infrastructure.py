from typing import Annotated

from ...models.request import InfrastructureRequest
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def validate_infrastructure_request(request: Annotated[InfrastructureRequest, field("validate_infrastructure_request", "request")]) -> Result:
        return orchestrator.validate(request, identity())

    def plan_infrastructure(request: Annotated[InfrastructureRequest, field("plan_infrastructure", "request")],
                            check_quota: Annotated[bool, field("plan_infrastructure", "check_quota")] = False) -> Result:
        return orchestrator.plan(request, identity(), check_quota)

    def provision_infrastructure(request: Annotated[InfrastructureRequest, field("provision_infrastructure", "request")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.provision(request, identity(), wait_seconds)

    def get_infrastructure_status(request_id: Annotated[str, field("get_infrastructure_status", "request_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.status(request_id, identity(), refresh=True, wait_seconds=wait_seconds)

    def list_infrastructure(status: Annotated[str | None, field("list_infrastructure", "status")] = None,
                            limit: Annotated[int, field("list_infrastructure", "limit")] = 50) -> Result:
        return orchestrator.list_requests(identity(), status, limit)

    def destroy_infrastructure(request_id: Annotated[str, field("destroy_infrastructure", "request_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.destroy(request_id, identity(), wait_seconds)

    for fn in (validate_infrastructure_request, plan_infrastructure, provision_infrastructure, get_infrastructure_status, list_infrastructure, destroy_infrastructure):
        register(mcp, wrap, fn)
