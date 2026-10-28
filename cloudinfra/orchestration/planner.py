"""From a validated request to a plan: names, identifiers, provider shapes, cost, warnings, and the cost ceiling check."""
import secrets

from ..models.errors import ErrorCode
from ..models.request import InfrastructureRequest
from ..models.resources import ID_PREFIX, ResourceKind
from ..models.response import CostEstimate, PlannedResource
from ..providers.base import CloudProvider, Problem, ProvisionContext
from . import lifecycle


def context(request: InfrastructureRequest, request_id: str, owner: str | None, config, provider: CloudProvider) -> ProvisionContext:
    name = request.name or f"infra-{request_id[:8]}"
    zone = request.zone or (provider.zones(request.region) or [None])[0]
    return ProvisionContext(request_id=request_id, name=name, region=request.region, zone=zone, owner=owner, labels={**config.labels, **request.labels})


def new_resource_id(kind: str) -> str:
    return f"{ID_PREFIX[ResourceKind(kind)]}-{secrets.token_hex(6)}"


def autostop_minutes(group, limits) -> int | None:
    """The idle window after which a group's machines stop: the request's own, else the operator's default when the
    group is in its scope (machines with GPUs, or all), else none."""
    if group.autostop is not None:
        return group.autostop.idle_minutes
    if limits.default_autostop_idle_minutes and (limits.default_autostop_scope == "all" or group.accelerator is not None):
        return limits.default_autostop_idle_minutes
    return None


def plan(request: InfrastructureRequest, ctx: ProvisionContext, provider: CloudProvider, config=None) -> list[PlannedResource]:
    """The provider's plan with identifiers, and the operator's policies the provider does not apply itself (idle stop)."""
    planned = provider.plan(request, ctx)
    for item in planned:
        item.spec.setdefault("resource_id", new_resource_id(item.kind))
        if item.kind == "compute" and config is not None and "compute" in provider.stoppable_kinds:
            group = request.group(item.spec.get("group"))
            item.spec["autostop_idle_minutes"] = autostop_minutes(group, config.limits) if group is not None else None
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


def warnings(request: InfrastructureRequest, planned: list[PlannedResource], config=None, provider: CloudProvider | None = None) -> list[str]:
    found = []
    groups = request.compute_groups()
    if request.storage and request.storage.type == "persistent" and not groups:
        found.append("storage without compute: the disk is created unattached; attach_storage connects it to a machine later")
    if request.network and request.network.public_access and "0.0.0.0/0" in request.network.allowed_sources:
        found.append(f"network.public_access opens ports {', '.join(map(str, request.network.allowed_ports))} to the whole internet; narrow allowed_sources if you can")
    if any(group.public_ip for group in groups):
        found.append("machines with a public address cost by the hour and are reachable from allowed_sources on allowed_ports")
    for group in groups:
        chosen = next((item.spec for item in planned if item.kind == "compute" and item.spec.get("group") == group.group_name), {})
        if group.accelerator:
            shape = f" as {chosen['machine_type']} ({chosen['cpu']} CPUs, {chosen['memory_gb']} GB)" if chosen else ""
            if chosen.get("profile"):
                software = f"drivers come with profile {chosen['profile']}"
            else:
                software = ("the image has no GPU driver: the Application Layer installs it, and Secure Boot is off so that driver can load "
                            "(an operator profile with signed drivers keeps it on)")
            found.append(f"{group.group_name}: {group.accelerator.count} {group.accelerator.type} per machine{shape}; {software}; "
                         "the machine restarts rather than migrates on host maintenance")
        if chosen.get("boot_disk_gb") and chosen["boot_disk_gb"] > group.boot_disk_gb:
            found.append(f"{group.group_name}: the boot disk is {chosen['boot_disk_gb']} GB, the minimum of profile {chosen.get('profile')}, not the {group.boot_disk_gb} GB asked")
        if chosen.get("autostop_idle_minutes") and group.autostop is None:
            found.append(f"{group.group_name}: the operator's default stops these machines after {chosen['autostop_idle_minutes']} idle minutes; "
                         "control_infrastructure keep_awake suspends it")
        if group.max_run_hours:
            found.append(f"{group.group_name}: each machine stops after {group.max_run_hours} hours of running; starting it again starts the count again")
    if request.database and request.database.size == "small":
        found.append("a small database is a shared-core instance: fine for development, not covered by the provider's SLA")
    database = next((item.spec for item in planned if item.kind == "database"), None)
    if database and database.get("extended_support"):
        found.append(f"{database['engine']} {database['version']} is kept by the provider only under paid extended support; "
                     f"{database.get('recommended_version')} is its current default, unless the application needs this version")
    if request.database and request.database.public_access:
        found.append("the database has a public address; connections are encrypted-only, and the Application Layer must manage its authorized networks")
    if request.database:
        found.append("the database password is generated and written to the request's db-password secret, which its access members can read; no response carries it")
    if request.cache:
        found.append("the cache's AUTH string is copied into the request's cache-auth secret, which its access members can read")
    if request.cluster:
        found.append("a cluster bills a management fee by the hour on top of its nodes (one zonal cluster per billing account is free); credentials come from the provider's IAM")
        if request.cluster.private_nodes:
            found.append("private nodes have no public address: the Application Layer needs the provider's NAT for them to reach the internet")
    if request.load_balancer:
        found.append(f"the load balancer is public on port {request.load_balancer.port}; it serves plain {request.load_balancer.type}, and TLS termination is a later feature")
    found.extend(time_warnings(request, config))
    if request.access and provider is not None:
        takes = [kind for kind in request.kinds() if kind in provider.grantable_kinds]
        if not takes and not ((request.database or request.cache) and "secret" in provider.grantable_kinds):
            found.append(f"access: nothing in this request takes grants (the {provider.name} adapter grants on {', '.join(provider.grantable_kinds) or 'nothing'}); "
                         "the members are recorded only")
    return found


def time_warnings(request: InfrastructureRequest, config) -> list[str]:
    """What happens with time: an expiry, and what it does."""
    if config is None:
        return [f"expires {request.lifetime.hours} hours after it is created"] if request.lifetime else []
    hours = lifecycle.lifetime_hours(request, config.limits)
    if not hours:
        return []
    on_expiry = request.lifetime.on_expiry if request.lifetime else "stop"
    retention = config.limits.expiry_retention_days
    if on_expiry == "destroy" or retention == 0:
        then = "everything is destroyed"
    else:
        then = f"machines and databases stop, and everything is destroyed {retention} days later"
    if request.lifetime:
        source = ""
    elif config.limits.default_lifetime_hours:
        source = " (the operator's default lifetime)"
    else:
        source = " (the operator's maximum lifetime)"
    return [f"expires {hours} hours after it is created{source}; then {then} unless control_infrastructure extend adds hours"]
