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
    groups = request.compute_groups()
    if request.storage and request.storage.type == "persistent" and not groups:
        found.append("storage without compute: the disk is created unattached; attach_storage connects it to a machine later")
    if request.network and request.network.public_access and "0.0.0.0/0" in request.network.allowed_sources:
        found.append(f"network.public_access opens ports {', '.join(map(str, request.network.allowed_ports))} to the whole internet; narrow allowed_sources if you can")
    if any(group.public_ip for group in groups):
        found.append("machines with a public address cost by the hour and are reachable from allowed_sources on allowed_ports")
    for group in groups:
        if group.accelerator:
            chosen = next((item.spec for item in planned if item.kind == "compute" and item.spec.get("group") == group.group_name), {})
            shape = f" as {chosen['machine_type']} ({chosen['cpu']} CPUs, {chosen['memory_gb']} GB)" if chosen else ""
            found.append(f"{group.group_name}: {group.accelerator.count} {group.accelerator.type} per machine{shape}; the GPU driver and toolkit are the Application Layer's to install, "
                         "and the machine restarts rather than migrates on host maintenance")
    if request.database and request.database.size == "small":
        found.append("a small database is a shared-core instance: fine for development, not covered by the provider's SLA")
    if request.database and request.database.public_access:
        found.append("the database has a public address; connections are encrypted-only, and the Application Layer must manage its authorized networks")
    if request.cache:
        found.append("the cache's AUTH string is read from the provider by the Application Layer with its own identity; this service never sees it")
    if request.cluster:
        found.append("a cluster bills a management fee by the hour on top of its nodes (one zonal cluster per billing account is free); credentials come from the provider's IAM")
        if request.cluster.private_nodes:
            found.append("private nodes have no public address: the Application Layer needs the provider's NAT for them to reach the internet")
    if request.load_balancer:
        found.append(f"the load balancer is public on port {request.load_balancer.port}; it serves plain {request.load_balancer.type}, and TLS termination is a later feature")
    return found
