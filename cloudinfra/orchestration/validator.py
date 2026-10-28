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
    problems.extend(provider.validate(request))
    limits = config.limits
    if request.compute:
        compute = request.compute
        if compute.count > limits.max_instances_per_request:
            problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_instances_per_request} machines per request; {compute.count} asked", "compute.count"))
        if compute.cpu > limits.max_cpu_per_instance:
            problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_cpu_per_instance} CPUs per machine; {compute.cpu} asked", "compute.cpu"))
        if compute.memory_gb > limits.max_memory_gb_per_instance:
            problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_memory_gb_per_instance} GB per machine; {compute.memory_gb} asked", "compute.memory_gb"))
        if compute.public_ip and request.network is not None and not request.network.public_access:
            problems.append(Problem(ErrorCode.INVALID_NETWORK_CONFIGURATION, "compute.public_ip needs network.public_access: true on the request's network", "compute.public_ip"))
    if request.storage:
        disks = (request.compute.count if request.compute and request.storage.type == "persistent" else 1)
        total = request.storage.size_gb * disks + (request.compute.boot_disk_gb * request.compute.count if request.compute else 0)
        if total > limits.max_storage_gb_per_request:
            problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_storage_gb_per_request} GB of disk per request; {total} GB asked", "storage.size_gb"))
    elif request.compute and request.compute.boot_disk_gb * request.compute.count > limits.max_storage_gb_per_request:
        problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_storage_gb_per_request} GB of disk per request", "compute.boot_disk_gb"))
    if request.database:
        if request.database.storage_gb > limits.max_database_storage_gb:
            problems.append(Problem(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_database_storage_gb} GB of database storage; {request.database.storage_gb} asked", "database.storage_gb"))
        if not request.database.public_access and request.network is None and request.database.network_id is None:
            problems.append(Problem(ErrorCode.INVALID_DATABASE_CONFIGURATION, "A private database (public_access: false) needs a network block in the same request, or "
                                    "network_id naming a network this service created", "database"))
        if request.database.network_id and request.network is not None:
            problems.append(Problem(ErrorCode.INVALID_DATABASE_CONFIGURATION, "database.network_id and a network block in the same request: choose one", "database.network_id"))
    if request.compute and request.compute.network_id and request.network is not None:
        problems.append(Problem(ErrorCode.INVALID_NETWORK_CONFIGURATION, "compute.network_id and a network block in the same request: choose one", "compute.network_id"))
    return problems


def raise_first(problems: list[Problem]) -> None:
    if problems:
        first = problems[0]
        raise InfrastructureError(first.code, first.message, {"field": first.field, "problems": [{"code": item.code.value, "message": item.message, "field": item.field} for item in problems]})
