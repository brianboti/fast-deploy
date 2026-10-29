from typing import Annotated, Literal

from ...models.request import InfrastructureRequest
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def list_resource_kinds() -> Result:
        return orchestrator.resource_kinds()

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
                            limit: Annotated[int, field("list_infrastructure", "limit")] = 50,
                            expiring_within_hours: Annotated[int | None, field("list_infrastructure", "expiring_within_hours")] = None) -> Result:
        return orchestrator.list_requests(identity(), status, limit, expiring_within_hours)

    def destroy_infrastructure(request_id: Annotated[str, field("destroy_infrastructure", "request_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.destroy(request_id, identity(), wait_seconds)

    def control_infrastructure(request_id: Annotated[str, field("control_infrastructure", "request_id")],
                               action: Annotated[Literal["stop", "start", "extend", "keep_awake"], field("control_infrastructure", "action")],
                               hours: Annotated[int | None, field("control_infrastructure", "hours")] = None, wait_seconds: Wait = 0) -> Result:
        return orchestrator.control(request_id, action, identity(), hours, wait_seconds)

    def manage_infrastructure_access(request_id: Annotated[str, field("manage_infrastructure_access", "request_id")],
                                     action: Annotated[Literal["grant", "revoke"], field("manage_infrastructure_access", "action")],
                                     member: Annotated[str, field("manage_infrastructure_access", "member")],
                                     level: Annotated[Literal["use", "admin"], field("manage_infrastructure_access", "level")] = "use") -> Result:
        return orchestrator.manage_access(request_id, action, member, identity(), level)

    for fn in (list_resource_kinds, validate_infrastructure_request, plan_infrastructure, provision_infrastructure, get_infrastructure_status, list_infrastructure, destroy_infrastructure,
               control_infrastructure, manage_infrastructure_access):
        register(mcp, wrap, fn)
