from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_budget(request_id: Annotated[str, field("create_budget", "request_id")], monthly_amount: Annotated[int, field("create_budget", "monthly_amount")],
                      notify: Annotated[list[str], field("create_budget", "notify")], thresholds: Annotated[list[int] | None, field("create_budget", "thresholds")] = None,
                      forecast: Annotated[bool, field("create_budget", "forecast")] = True, name: Annotated[str | None, field("create_budget", "name")] = None,
                      labels: Annotated[dict[str, str] | None, field("create_budget", "labels")] = None, wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_budget(identity(), request_id=request_id, monthly_amount=monthly_amount, notify=notify, thresholds=thresholds, forecast=forecast, name=name,
                                          labels=labels, wait_seconds=wait_seconds)

    def get_budget(resource_id: Annotated[str, field("get_budget", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.budget)

    def delete_budget(resource_id: Annotated[str, field("delete_budget", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.budget, wait_seconds)

    for fn in (create_budget, get_budget, delete_budget):
        register(mcp, wrap, fn)
