"""Request validation beyond the contract's own shape checks: the operator's regions and limits, the provider's shapes,
and the rules that tie the blocks of one request together. Returns problems; the caller decides whether to raise."""
from ..models.errors import ErrorCode, InfrastructureError
from ..models.request import InfrastructureRequest
from ..providers.base import CloudProvider, Problem


def validate(request: InfrastructureRequest, config, provider: CloudProvider) -> list[Problem]:
    problems: list[Problem] = []
    settings = getattr(config.providers, provider.name)
    allowed = list(settings.allowed_regions)
    if request.region not in allowed:
        problems.append(Problem(ErrorCode.INVALID_REGION, f"Region {request.region} is not allowed by this service; allowed: {', '.join(allowed)}", "region"))
    for kind in request.kinds():
        if kind not in provider.supported_kinds:
            problems.append(Problem(ErrorCode.UNSUPPORTED_RESOURCE_KIND, f"The {provider.name} adapter does not provide {kind}; it provides {', '.join(provider.supported_kinds)}", kind))
    problems.extend(provider.validate(request))
    limits = config.limits
    groups = request.compute_groups()
    machines = sum(group.count for group in groups)
    if machines > limits.max_instances_per_request:
        problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_instances_per_request} machines per request; {machines} asked", "compute.count"))
    if request.lifetime and limits.max_lifetime_hours and request.lifetime.hours > limits.max_lifetime_hours:
        problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_lifetime_hours} hours of lifetime; {request.lifetime.hours} asked", "lifetime.hours"))
    if request.lifetime and config.sweep_interval_seconds == 0:
        problems.append(Problem(ErrorCode.INVALID_REQUEST, "This service applies no lifetimes: the operator turned its sweeper off (sweep_interval_seconds: 0)",
                                "lifetime"))
    for group in groups:
        where = f"compute[{group.group_name}]" if len(groups) > 1 else "compute"
        if (group.autostop or group.max_run_hours) and "compute" not in provider.stoppable_kinds:
            problems.append(Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, f"The {provider.name} adapter cannot stop machines: autostop and max_run_hours are not available",
                                    f"{where}.autostop" if group.autostop else f"{where}.max_run_hours"))
        elif group.autostop and config.sweep_interval_seconds == 0:
            problems.append(Problem(ErrorCode.INVALID_REQUEST, "This service stops no idle machines: the operator turned its sweeper off (sweep_interval_seconds: 0)",
                                    f"{where}.autostop"))
        if group.cpu > limits.max_cpu_per_instance:
            problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_cpu_per_instance} CPUs per machine; {group.cpu} asked", f"{where}.cpu"))
        if group.memory_gb > limits.max_memory_gb_per_instance:
            problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_memory_gb_per_instance} GB per machine; {group.memory_gb} asked", f"{where}.memory_gb"))
        if group.accelerator and group.accelerator.count > limits.max_accelerators_per_instance:
            text = "Accelerators are not allowed by this service" if limits.max_accelerators_per_instance == 0 else f"At most {limits.max_accelerators_per_instance} accelerators per machine"
            problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, text, f"{where}.accelerator.count"))
        if group.public_ip and request.network is not None and not request.network.public_access:
            problems.append(Problem(ErrorCode.INVALID_NETWORK_CONFIGURATION, "compute.public_ip needs network.public_access: true on the request's network", f"{where}.public_ip"))
        if group.network_id and request.network is not None:
            problems.append(Problem(ErrorCode.INVALID_NETWORK_CONFIGURATION, "compute.network_id and a network block in the same request: choose one", f"{where}.network_id"))
    boot = sum(provider.boot_disk_gb(group) * group.count for group in groups)  # as planned: a profile may raise it
    if request.storage:
        targets = [group for group in groups if request.storage.group in (None, group.group_name)]
        disks = sum(group.count for group in targets) if request.storage.type == "persistent" and groups else 1
        total = request.storage.size_gb * disks + boot
        if total > limits.max_storage_gb_per_request:
            problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_storage_gb_per_request} GB of disk per request; {total} GB asked", "storage.size_gb"))
    elif boot > limits.max_storage_gb_per_request:
        problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_storage_gb_per_request} GB of disk per request", "compute.boot_disk_gb"))
    if request.cache:
        if request.cache.memory_gb > limits.max_cache_memory_gb:
            problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_cache_memory_gb} GB of cache; {request.cache.memory_gb} asked", "cache.memory_gb"))
        if request.network is None and request.cache.network_id is None:
            problems.append(Problem(ErrorCode.INVALID_CACHE_CONFIGURATION, "A cache is private: it needs a network block in the same request, or network_id naming one this service created", "cache"))
        if request.cache.network_id and request.network is not None:
            problems.append(Problem(ErrorCode.INVALID_CACHE_CONFIGURATION, "cache.network_id and a network block in the same request: choose one", "cache.network_id"))
    clusters = request.clusters()
    nodes = sum(cluster.node_count for cluster in clusters)
    if nodes > limits.max_cluster_nodes:  # across the request's clusters, as a cost limit
        problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_cluster_nodes} cluster nodes in a request; {nodes} asked", "cluster.node_count"))
    for cluster in clusters:
        if cluster.node_cpu > limits.max_cpu_per_instance or cluster.node_memory_gb > limits.max_memory_gb_per_instance:
            problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_cpu_per_instance} CPUs and {limits.max_memory_gb_per_instance} GB per node", "cluster"))
        if cluster.network_id and request.network is not None:
            problems.append(Problem(ErrorCode.INVALID_CLUSTER_CONFIGURATION, "cluster.network_id and a network block in the same request: choose one", "cluster.network_id"))
    if request.database:
        if request.database.storage_gb > limits.max_database_storage_gb:
            problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_database_storage_gb} GB of database storage; {request.database.storage_gb} asked", "database.storage_gb"))
        if not request.database.public_access and request.network is None and request.database.network_id is None:
            problems.append(Problem(ErrorCode.INVALID_DATABASE_CONFIGURATION, "A private database (public_access: false) needs a network block in the same request, or "
                                    "network_id naming a network this service created", "database"))
        if request.database.network_id and request.network is not None:
            problems.append(Problem(ErrorCode.INVALID_DATABASE_CONFIGURATION, "database.network_id and a network block in the same request: choose one", "database.network_id"))
    return problems


def raise_first(problems: list[Problem]) -> None:
    if problems:
        first = problems[0]
        raise InfrastructureError(first.code, first.message, {"field": first.field, "problems": [{"code": item.code.value, "message": item.message, "field": item.field} for item in problems]})
