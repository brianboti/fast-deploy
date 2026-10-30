"""The plan: every check a release must pass before anything touches a machine, against the request as the
infrastructure layer reports it now. Pure: it reads, and records nothing. All problems are reported, each with a code."""
from .errors import DeliveryError, ErrorCode, ErrorDetail
from .infra import IAP_RANGE, InfraRequest
from .models import PlannedMachine, PlanResponse, ReleaseRequest

ARTIFACT_REGISTRY_SUFFIX = "-docker.pkg.dev"  # reachable through Private Google Access, without a public address
CONTAINER_OS_PREFIX = "cos-"  # Container-Optimized OS image and family names


def member(service_account: str) -> str:
    return f"serviceAccount:{service_account}"


def authorize(release: ReleaseRequest, principal, config) -> None:
    """The caller may deploy, and in this environment. Raised at once: nothing else is worth checking for a caller who may not."""
    if release.environment not in config.environments:
        raise DeliveryError(ErrorCode.UNKNOWN_ENVIRONMENT, f"No environment {release.environment!r}; environments: {', '.join(sorted(config.environments))}")
    if not principal.can_deploy:
        raise DeliveryError(ErrorCode.PERMISSION_DENIED, f"{principal.name} may not deploy", {"identity": principal.name})
    if release.environment not in principal.environments:
        raise DeliveryError(ErrorCode.PERMISSION_DENIED, f"{principal.name} may not deploy to {release.environment}",
                            {"identity": principal.name, "environments": principal.environments})


def _problem(code: ErrorCode, message: str, **details) -> ErrorDetail:
    return ErrorDetail(code=code, message=message, details=details)


def check(release: ReleaseRequest, principal, config, infra: InfraRequest) -> tuple[list[ErrorDetail], list[str]]:
    """(problems, warnings) of a release against the request it names."""
    environment = config.environments[release.environment]
    problems, warnings = [], []
    if infra.owner != principal.infra_owner:
        # the same answer cloudinfra gives another caller: whether someone else's request exists is not this caller's to learn
        return [_problem(ErrorCode.NOT_FOUND, f"Request {release.request_id} is not one of {principal.infra_owner}'s", request_id=release.request_id)], []
    if infra.status != "READY":
        problems.append(_problem(ErrorCode.INVALID_STATE, f"Request {release.request_id} is {infra.status}; a release needs it READY", status=infra.status))
    grant = infra.grant_of(member(environment.service_account))
    if grant is None:
        problems.append(_problem(ErrorCode.NOT_GRANTED, f"The request does not name {environment.service_account} in its access list: add it with level admin "
                                 "(manage_infrastructure_access) to let this service run containers on its machines or cluster", member=member(environment.service_account)))
    elif grant.level != "admin" or grant.status != "GRANTED":
        problems.append(_problem(ErrorCode.NOT_GRANTED, f"{environment.service_account} is on the request as {grant.level}, {grant.status}; running containers needs "
                                 "admin, GRANTED", level=grant.level, grant_status=grant.status))

    if release.target.cluster:
        cluster = infra.cluster(release.target.cluster)
        names = sorted(infra.cluster_name(item) for item in infra.clusters())
        if cluster is None and not names:
            problems.append(_problem(ErrorCode.NOT_FOUND, "The request has no cluster; ask cloudinfra for one with a cluster block", field="target.cluster"))
        elif cluster is None and release.target.cluster is True:
            problems.append(_problem(ErrorCode.INVALID_RELEASE, f"The request has {len(names)} clusters ({', '.join(names)}); name one in target.cluster",
                                     field="target.cluster", clusters=names))
        elif cluster is None:
            problems.append(_problem(ErrorCode.NOT_FOUND, f"The request has no cluster {release.target.cluster}; it has {', '.join(names)}", field="target.cluster",
                                     clusters=names))
        else:
            if cluster.status != "READY":
                problems.append(_problem(ErrorCode.INVALID_STATE, f"Cluster {cluster.name} is {cluster.status}", resource=cluster.id))
            if not cluster.namespace:
                problems.append(_problem(ErrorCode.UNSUPPORTED_TARGET, f"Cluster {cluster.name} has no namespace for the request: it was made before the infrastructure "
                                         "layer gave clusters one; grant the access again to make it", resource=cluster.id))
            if not cluster.endpoint or not cluster.ca_certificate:
                problems.append(_problem(ErrorCode.UNREACHABLE_TARGET, f"The infrastructure layer reports no address or CA certificate for cluster {cluster.name}",
                                         resource=cluster.id))
            if grant is not None and grant.resources and cluster.id not in grant.resources:
                problems.append(_problem(ErrorCode.NOT_GRANTED, f"{environment.service_account} was not granted on cluster {cluster.name}", resource=cluster.id))
            if release.target.expose is not None:
                _check_exposure(release, cluster, problems, warnings)
    elif release.target.service:
        service = infra.service(release.target.service)
        names = sorted(infra.cluster_name(item) for item in infra.services())
        if service is None and not names:
            problems.append(_problem(ErrorCode.NOT_FOUND, "The request has no service; ask cloudinfra for one with a service block", field="target.service"))
        elif service is None and release.target.service is True:
            problems.append(_problem(ErrorCode.INVALID_RELEASE, f"The request has {len(names)} services ({', '.join(names)}); name one in target.service",
                                     field="target.service", services=names))
        elif service is None:
            problems.append(_problem(ErrorCode.NOT_FOUND, f"The request has no service {release.target.service}; it has {', '.join(names)}", field="target.service",
                                     services=names))
        else:
            if service.status != "READY":
                problems.append(_problem(ErrorCode.INVALID_STATE, f"Service {service.name} is {service.status}", resource=service.id))
            if not service.provider_id or not service.runtime_service_account:
                problems.append(_problem(ErrorCode.UNSUPPORTED_TARGET, f"The infrastructure layer reports no provider name or runtime account for service {service.name}",
                                         resource=service.id))
            if grant is not None and grant.resources and service.id not in grant.resources:
                problems.append(_problem(ErrorCode.NOT_GRANTED, f"{environment.service_account} was not granted on service {service.name}", resource=service.id))
        if "PORT" in release.env:
            problems.append(_problem(ErrorCode.INVALID_BINDING, "env.PORT: Cloud Run sets PORT to the release's port itself", field="env.PORT"))
    else:
        machines = infra.group(release.target.group)
        if not machines:
            problems.append(_problem(ErrorCode.NOT_FOUND, f"The request has no machines in group {release.target.group!r}; groups: {', '.join(infra.groups()) or 'none'}",
                                     field="target.group"))
        for machine in machines:
            if machine.status != "READY":
                problems.append(_problem(ErrorCode.INVALID_STATE, f"{machine.name} is {machine.status}; start the request first", resource=machine.id))
            if not machine.os_image.startswith(CONTAINER_OS_PREFIX):
                problems.append(_problem(ErrorCode.UNSUPPORTED_TARGET, f"{machine.name} runs {machine.os_image or 'an unknown image'}, which has no Docker; ask cloudinfra for "
                                         f"machines with profile {config.gcp.container_profile} (Container-Optimized OS)", resource=machine.id))
            if grant is not None and grant.resources and machine.id not in grant.resources:
                problems.append(_problem(ErrorCode.NOT_GRANTED, f"{environment.service_account} was not granted on {machine.name}", resource=machine.id))
            network = infra.network(machine.network)
            if machine.network is None:
                if not config.gcp.default_network_iap:
                    problems.append(_problem(ErrorCode.UNREACHABLE_TARGET, f"{machine.name} is in the operator's default network, which this service is not told admits "
                                             f"{IAP_RANGE} on port 22; give the request a network block with iap_ssh", resource=machine.id))
            elif network is None:
                warnings.append(f"{machine.name} is on network {machine.network} of another request: whether it admits SSH through IAP could not be checked")
            elif not network.admits(22, IAP_RANGE):
                problems.append(_problem(ErrorCode.UNREACHABLE_TARGET, f"{network.name} does not admit {IAP_RANGE} on port 22; recreate it with iap_ssh or add the rule "
                                         "with configure_firewall", resource=machine.id))
            elif not network.opens_beyond_subnet(release.port):
                warnings.append(f"Port {release.port} is open only inside {network.name}: add a firewall rule or a load balancer to reach it from elsewhere")
            if not release.registry().endswith(ARTIFACT_REGISTRY_SUFFIX) and machine.public_ip is None:
                problems.append(_problem(ErrorCode.UNREACHABLE_TARGET, f"{machine.name} has no public address and the infrastructure layer offers no NAT, so it can pull only "
                                         f"from Artifact Registry (*{ARTIFACT_REGISTRY_SUFFIX})", resource=machine.id, registry=release.registry()))
        if any(machine.autostop_idle_minutes for machine in machines):
            warnings.append(f"Group {release.target.group} stops itself when idle (autostop): a quiet service is idle to it; ask for keep_awake or a group without autostop")
        if any(machine.max_run_hours for machine in machines):
            warnings.append(f"Group {release.target.group} stops after max_run_hours from each start")

    if not any(release.repository() == prefix or release.repository().startswith(prefix.rstrip("/") + "/") for prefix in environment.allowed_registries):
        problems.append(_problem(ErrorCode.REGISTRY_NOT_ALLOWED, f"{release.repository()} is not under the registries allowed in {release.environment}: "
                                 f"{', '.join(environment.allowed_registries)}", field="image"))

    for name, binding in release.env.items():
        if binding.endpoint is not None and infra.endpoints.get(binding.endpoint) in (None, "", []):
            problems.append(_problem(ErrorCode.INVALID_BINDING, f"env.{name}: the request has no {binding.endpoint}", field=f"env.{name}"))
        if binding.secret is not None:
            secret = infra.secret(binding.secret)
            if secret is None:
                problems.append(_problem(ErrorCode.INVALID_BINDING, f"env.{name}: the request has no secret {binding.secret!r}; its secrets: "
                                         f"{', '.join(infra.secret_names()) or 'none'}", field=f"env.{name}"))
            elif secret.status != "READY":
                problems.append(_problem(ErrorCode.INVALID_BINDING, f"env.{name}: secret {binding.secret} is {secret.status}", field=f"env.{name}"))
    return problems, list(dict.fromkeys(warnings))  # machines on one network warn once


def _check_exposure(release: ReleaseRequest, cluster, problems: list, warnings: list) -> None:
    door, hostname = cluster.public, release.target.expose.hostname
    if door is None:
        problems.append(_problem(ErrorCode.UNSUPPORTED_TARGET, f"Cluster {cluster.name} has no front door; ask the infrastructure layer for one (cluster.public)",
                                 resource=cluster.id, field="target.expose"))
    elif hostname is not None and hostname not in door.hostnames:
        served = f"serves {', '.join(door.hostnames)}" if door.hostnames else "serves plain HTTP on its address, with no hostnames"
        problems.append(_problem(ErrorCode.INVALID_RELEASE, f"Cluster {cluster.name}'s front door {served}, not {hostname}", field="target.expose.hostname"))
    elif door.hostnames and door.certificate != "ACTIVE":
        warnings.append(f"The front door's certificate is {door.certificate or 'not reported'}: HTTPS works once DNS for {', '.join(door.hostnames)} points at "
                        f"{door.address} and Google has issued it")


def url(release: ReleaseRequest, cluster) -> str | None:
    """Where an exposed workload is reached from outside."""
    expose, door = release.target.expose, cluster.public
    if expose is None or door is None:
        return None
    if door.hostnames:
        return f"https://{expose.hostname or door.hostnames[0]}{expose.path}"
    return f"http://{door.address}{expose.path}"


def resolve(release: ReleaseRequest, infra: InfraRequest) -> ReleaseRequest:
    """cluster: true named as the request's only cluster is named, so a release is kept under one key however it was asked for."""
    if release.target.service is True and (service := infra.service(True)) is not None:
        return release.model_copy(update={"target": release.target.model_copy(update={"service": infra.cluster_name(service)})})
    cluster = infra.cluster(True) if release.target.cluster is True else None
    if cluster is None:
        return release
    return release.model_copy(update={"target": release.target.model_copy(update={"cluster": infra.cluster_name(cluster)})})


def plan(release: ReleaseRequest, principal, config, infra: InfraRequest) -> PlanResponse:
    """The plan of a release, or a DeliveryError with every problem found (the first one's code, all in details.problems)."""
    authorize(release, principal, config)
    problems, warnings = check(release, principal, config, infra)
    if problems:
        first = problems[0]
        raise DeliveryError(first.code, first.message if len(problems) == 1 else f"{first.message} (and {len(problems) - 1} more problems)",
                            {"problems": [item.model_dump(mode="json") for item in problems]})
    environment = config.environments[release.environment]
    extra = {}
    if release.target.cluster:
        cluster = infra.cluster(release.target.cluster)
        machines = [PlannedMachine(resource=cluster.id, name=cluster.name, zone=cluster.location)]
        extra = {"target": "cluster", "namespace": cluster.namespace, "workload": release.target.workload, "replicas": release.target.replicas,
                 "url": url(release, cluster)}
    elif release.target.service:
        service = infra.service(release.target.service)
        machines = [PlannedMachine(resource=service.id, name=service.name, zone=service.region)]
        extra = {"target": "service", "url": service.url}
    else:
        machines = [PlannedMachine(resource=machine.id, name=machine.name, zone=machine.zone or "", instance_id=machine.instance_id, internal_ip=machine.internal_ip)
                    for machine in infra.group(release.target.group)]
    return PlanResponse(environment=release.environment, request_id=release.request_id, image=release.image, service_account=environment.service_account,
                        machines=machines, env={name: binding.source() for name, binding in release.env.items()},
                        attestation_required=environment.require_attestation, approval_required=environment.require_approval, warnings=warnings, **extra)
