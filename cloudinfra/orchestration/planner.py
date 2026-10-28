"""From a validated request to a plan: names, identifiers, provider shapes, cost, warnings, and the cost ceiling check."""
import secrets

from ..models.errors import ErrorCode
from ..models.request import InfrastructureRequest
from ..models.resources import ID_PREFIX, ResourceKind
from ..models.response import CostEstimate, PlannedResource
from ..providers.base import CloudProvider, Problem, ProvisionContext


def context(request: InfrastructureRequest, request_id: str, owner: str | None, config, provider: CloudProvider) -> ProvisionContext:
    name = request.name or f"infra-{request_id[:8]}"
    zone = request.zone or (provider.zones(request.region) or [None])[0]
    return ProvisionContext(request_id=request_id, name=name, region=request.region, zone=zone, owner=owner, labels={**config.labels, **request.labels})


def new_resource_id(kind: str) -> str:
    return f"{ID_PREFIX[ResourceKind(kind)]}-{secrets.token_hex(6)}"


def plan(request: InfrastructureRequest, ctx: ProvisionContext, provider: CloudProvider) -> list[PlannedResource]:
    planned = provider.plan(request, ctx)
    for item in planned:
        item.spec.setdefault("resource_id", new_resource_id(item.kind))
    return planned


def ceiling(config, principal) -> float:
    limit = config.limits.max_monthly_cost_usd
    own = getattr(principal, "max_monthly_cost_usd", None)
    return min(limit, own) if own is not None else limit


def cost_problem(estimate: CostEstimate, config, principal) -> Problem | None:
    limit = ceiling(config, principal)
    if estimate.monthly_usd > limit:
        return Problem(ErrorCode.COST_LIMIT_EXCEEDED, f"Estimated {estimate.monthly_usd:.2f} USD a month is above the ceiling of {limit:.2f} USD for this caller; "
                       "ask for less, or have the operator raise the limit")
    return None


def warnings(request: InfrastructureRequest, planned: list[PlannedResource]) -> list[str]:
    found = []
    if request.storage and request.storage.type == "persistent" and not request.compute:
        found.append("storage without compute: the disk is created unattached; attach_storage connects it to a machine later")
    if request.network and request.network.public_access and "0.0.0.0/0" in request.network.allowed_sources:
        found.append(f"network.public_access opens ports {', '.join(map(str, request.network.allowed_ports))} to the whole internet; narrow allowed_sources if you can")
    if request.compute and request.compute.public_ip:
        found.append("each machine gets a public address; it costs by the hour and is reachable from allowed_sources on allowed_ports")
    if request.database and request.database.size == "small":
        found.append("a small database is a shared-core instance: fine for development, not covered by the provider's SLA")
    if request.database and request.database.public_access:
        found.append("the database has a public address; connections are encrypted-only, and the Application Layer must manage its authorized networks")
    return found
