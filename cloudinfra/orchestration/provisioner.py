"""The orchestrator: REQUEST → VALIDATE → PLAN → PROVISION → READY → QUERY → DESTROY, with FAILED and cleanup on the way.

It owns the order (network, storage, compute, database; the reverse to destroy), the state, the audit trail and the
authorization of each caller. Providers implement primitives only. Provisioning and destruction run in bounded workers so
a tool call returns at once; a caller may wait up to 45 seconds for the outcome and otherwise polls the status.
"""
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

from ..models.errors import ErrorCode, InfrastructureError
from ..models.request import (
    RESOURCE_KINDS,
    AcceleratorRequest,
    CacheRequest,
    ClusterRequest,
    ComputeRequest,
    DatabaseRequest,
    InfrastructureRequest,
    LoadBalancerRequest,
    NetworkRequest,
    QueueRequest,
    SecretRequest,
    StorageRequest,
)
from ..models.resources import (
    CacheResource,
    ClusterResource,
    ComputeResource,
    DatabaseResource,
    LoadBalancerResource,
    NetworkResource,
    QueueResource,
    ResourceKind,
    ResourceStatus,
    SecretResource,
    StorageResource,
)
from ..models.response import (
    Credentials,
    Endpoints,
    ErrorDetail,
    Health,
    InfrastructureResponse,
    KindField,
    ListResponse,
    OperationStatus,
    PlannedResource,
    PlanResponse,
    RequestStatus,
    ResourceKindInfo,
    ResourceKindsResponse,
    ResourceResponse,
    Resources,
    Usage,
    ValidationResponse,
)
from ..observability.logging import bound, event
from ..providers.base import CloudProvider, ProvisionContext
from ..security import validation
from ..state.state_manager import StateManager, new_id
from . import planner, validator

MAX_WAIT_SECONDS = 45
POLL_SECONDS = 0.5
DESTROY_ORDER = tuple(ResourceKind(kind) for kind in reversed(RESOURCE_KINDS))
KIND_MODELS = {"network": NetworkRequest, "storage": StorageRequest, "compute": ComputeRequest, "database": DatabaseRequest, "cache": CacheRequest, "queue": QueueRequest,
               "secret": SecretRequest, "cluster": ClusterRequest, "load_balancer": LoadBalancerRequest}
SEVERAL = ("compute", "queue", "secret")
LIVE = (RequestStatus.PROVISIONING, RequestStatus.READY, RequestStatus.FAILED, RequestStatus.DESTROYING, RequestStatus.DESTROY_FAILED)


class InlineExecutor:
    """Runs submitted work at once, on the caller's thread: for tests and the command line."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def shutdown(self, wait=True):
        return None


class Orchestrator:
    def __init__(self, config, state: StateManager, registry, executor=None, sleep=time.sleep, clock=time.monotonic):
        self.config, self.state, self.registry, self.sleep, self.clock = config, state, registry, sleep, clock
        self.executor = executor or ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix="cloudinfra")

    # ---- authorization and visibility
    @staticmethod
    def require(principal, action: str) -> None:
        allowed = {"provision": getattr(principal, "can_provision", False), "destroy": getattr(principal, "can_destroy", False), "read": True}[action]
        if not allowed:
            raise InfrastructureError(ErrorCode.PERMISSION_DENIED, f"Identity {principal.name} may not {action} infrastructure", {"identity": principal.name, "action": action})

    def _record(self, request_id: str, principal) -> dict:
        validation.check_request_id(request_id)
        record = self.state.get_request(request_id)
        if record is None or (principal.scope != "all" and record["owner"] != principal.name):
            raise InfrastructureError(ErrorCode.NOT_FOUND, f"No infrastructure request {request_id} is visible to {principal.name}", {"request_id": request_id})
        return record

    def _resource(self, resource_id: str, principal):
        validation.check_identifier(resource_id)
        resource = self.state.get_resource(resource_id)
        if resource is None:
            raise InfrastructureError(ErrorCode.NOT_FOUND, f"No resource {resource_id} is known to this service", {"resource_id": resource_id})
        record = self._record(resource.request_id, principal)
        return resource, record

    def _context(self, record: dict, principal=None) -> ProvisionContext:
        return ProvisionContext(request_id=record["request_id"], name=record["name"], region=record["region"], zone=record["zone"], owner=record["owner"], labels=record["labels"])

    # ---- validate and plan
    def _prepare(self, request: InfrastructureRequest, principal, request_id: str):
        provider = self.registry.get(request.provider)
        problems = validator.validate(request, self.config, provider)
        validator.raise_first(problems)
        ctx = planner.context(request, request_id, principal.name, self.config, provider)
        planned = planner.plan(request, ctx, provider)
        estimate = provider.estimate_cost(planned)
        cost = planner.cost_problem(estimate, self.config, principal)
        if cost:
            raise InfrastructureError(cost.code, cost.message, {"estimated_monthly_usd": estimate.monthly_usd, "ceiling_usd": planner.ceiling(self.config, principal)})
        return provider, ctx, planned, estimate

    def validate(self, request: InfrastructureRequest, principal) -> ValidationResponse:
        problems: list[ErrorDetail] = []
        try:
            provider = self.registry.get(request.provider)
        except InfrastructureError as exc:
            return ValidationResponse(valid=False, provider=request.provider, region=request.region, request=request, problems=[exc.detail()])
        found = validator.validate(request, self.config, provider)
        problems.extend(ErrorDetail(code=item.code, message=item.message, details={"field": item.field} if item.field else {}) for item in found)
        if not found:
            ctx = planner.context(request, new_id(), principal.name, self.config, provider)
            estimate = provider.estimate_cost(planner.plan(request, ctx, provider))
            cost = planner.cost_problem(estimate, self.config, principal)
            if cost:
                problems.append(ErrorDetail(code=cost.code, message=cost.message, details={"estimated_monthly_usd": estimate.monthly_usd}))
        event("request_validated", provider=request.provider, region=request.region, kinds=request.kinds(), valid=not problems, problems=len(problems))
        return ValidationResponse(valid=not problems, provider=provider.name, region=request.region, request=request, problems=problems)

    def plan(self, request: InfrastructureRequest, principal, check_quota: bool = False) -> PlanResponse:
        request_id = new_id()
        with bound(request_id=request_id, identity=principal.name):
            event("request_received", purpose="plan", provider=request.provider, region=request.region, kinds=request.kinds())
            provider, ctx, planned, estimate = self._prepare(request, principal, request_id)
            event("provider_selected", provider=provider.name)
            event("resources_requested", planned=[{"kind": item.kind, "name": item.name} for item in planned], estimated_monthly_usd=estimate.monthly_usd)
            quota = provider.quota(request.region) if check_quota else None
            return PlanResponse(request_id=request_id, name=ctx.name, provider=provider.name, region=request.region, zone=ctx.zone, planned=planned, estimate=estimate,
                                warnings=planner.warnings(request, planned), quota=quota)

    # ---- provision
    def provision(self, request: InfrastructureRequest, principal, wait_seconds: int = 0) -> InfrastructureResponse:
        self.require(principal, "provision")
        request_id = new_id()
        with bound(request_id=request_id, identity=principal.name):
            event("request_received", purpose="provision", provider=request.provider, region=request.region, kinds=request.kinds())
            provider, ctx, planned, estimate = self._prepare(request, principal, request_id)
            event("request_validated", valid=True)
            event("provider_selected", provider=provider.name)
            event("resources_requested", planned=[{"kind": item.kind, "name": item.name, "mapping": item.provider_mapping} for item in planned], estimated_monthly_usd=estimate.monthly_usd)
            active = self.state.active_requests(principal.name)
            if active >= self.config.limits.max_active_requests_per_principal:
                raise InfrastructureError(ErrorCode.LIMIT_EXCEEDED, f"{principal.name} already has {active} live requests; destroy one before provisioning more",
                                          {"active": active, "limit": self.config.limits.max_active_requests_per_principal})
            existing = self._existing_networks(request, principal, provider)
            external_backends = self._existing_backends(request, principal, provider)
            if request.database:
                ctx.database_password = secrets.token_urlsafe(24)
            self.state.create_request(request_id, ctx.name, provider.name, request.region, ctx.zone, principal.name, RequestStatus.PROVISIONING, request.model_dump(mode="json"),
                                      ctx.labels, estimate.monthly_usd)
            for item in planned:
                self.state.save_resource(self._pending(item, request, ctx, provider.name))
            operation_id = self.state.start_operation(request_id, "provision", identity=principal.name)
            self.executor.submit(self._run_provision, request_id, request, planned, ctx, provider, operation_id, existing, external_backends)
            if wait_seconds:
                self._wait(request_id, wait_seconds)
            response = self.status(request_id, principal, refresh=False)
            if request.database:
                response.credentials = Credentials(database_password=ctx.database_password)
            return response

    def _existing_networks(self, request: InfrastructureRequest, principal, provider: CloudProvider) -> dict[str, NetworkResource]:
        """The networks a request names by id (network_id on compute, database, cache, cluster): visible, ready, same provider and region."""
        found = {}
        for block in (*request.compute_groups(), request.database, request.cache, request.cluster):
            network_id = getattr(block, "network_id", None)
            if not network_id:
                continue
            resource, _ = self._resource(network_id, principal)
            if resource.kind != ResourceKind.network:
                raise InfrastructureError(ErrorCode.NOT_FOUND, f"{network_id} is a {resource.kind.value}, not a network", {"resource_id": network_id})
            if resource.status != ResourceStatus.READY:
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"Network {network_id} is {resource.status.value}, not READY", {"resource_id": network_id})
            if resource.provider != provider.name or resource.region != request.region:
                raise InfrastructureError(ErrorCode.INVALID_NETWORK_CONFIGURATION, f"Network {network_id} is in {resource.provider} {resource.region}, not {provider.name} {request.region}",
                                          {"resource_id": network_id})
            found[network_id] = resource
        return found

    def _existing_backends(self, request: InfrastructureRequest, principal, provider: CloudProvider) -> list[ComputeResource]:
        """The machines a load balancer names by id: visible, ready, same provider, region and zone."""
        found = []
        for compute_id in (request.load_balancer.backends if request.load_balancer else None) or []:
            resource, _ = self._resource(compute_id, principal)
            if resource.kind != ResourceKind.compute:
                raise InfrastructureError(ErrorCode.NOT_FOUND, f"{compute_id} is a {resource.kind.value}, not a machine", {"resource_id": compute_id})
            if resource.status != ResourceStatus.READY:
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"Machine {compute_id} is {resource.status.value}, not READY", {"resource_id": compute_id})
            if resource.provider != provider.name or resource.region != request.region or (found and resource.zone != found[0].zone):
                raise InfrastructureError(ErrorCode.INVALID_LOAD_BALANCER_CONFIGURATION, f"Machine {compute_id} is in {resource.provider} {resource.zone}; every backend must be in "
                                          f"{provider.name} {request.region} and one zone", {"resource_id": compute_id})
            found.append(resource)
        return found

    @staticmethod
    def _pending(item: PlannedResource, request: InfrastructureRequest, ctx: ProvisionContext, provider: str):
        common = {"id": item.spec["resource_id"], "name": item.name, "request_id": ctx.request_id, "provider": provider, "region": ctx.region, "status": ResourceStatus.PENDING}
        spec = item.spec
        if item.kind == "compute":
            accelerator = f"{spec['accelerator']['type']} x{spec['accelerator']['count']}" if spec.get("accelerator") else None
            return ComputeResource(**common, zone=spec["zone"], group=spec["group"], machine_type=spec["machine_type"], cpu=spec["cpu"], memory_gb=spec["memory_gb"],
                                   os_image=spec["image"].rsplit("/", 1)[-1], boot_disk_gb=spec["boot_disk_gb"], accelerator=accelerator)
        if item.kind == "storage":
            return StorageResource(**common, zone=spec.get("zone"), type=spec["type"], size_gb=spec["size_gb"], performance=request.storage.performance)
        if item.kind == "network":
            return NetworkResource(**common, subnet=spec["subnet"], cidr=spec["cidr"], public_access=spec["public_access"])
        if item.kind == "database":
            return DatabaseResource(**common, engine=spec["engine"], version=spec["version"], tier=spec["tier"], storage_gb=spec["storage_gb"], high_availability=spec["high_availability"],
                                    public_access=not spec["private"], port=spec["port"], database=request.database.database_name, username=request.database.username)
        if item.kind == "cache":
            return CacheResource(**common, version=spec["version"], tier=spec["tier"], memory_gb=spec["memory_gb"], high_availability=spec["tier"] == "STANDARD_HA")
        if item.kind == "queue":
            return QueueResource(**common, topic=f"pending/{item.name}", retention_hours=spec["retention_hours"])
        if item.kind == "secret":
            return SecretResource(**common, resource_name=f"pending/{item.name}", versions=0)
        if item.kind == "cluster":
            return ClusterResource(**common, zone=spec["location"], location=spec["location"], node_count=spec["node_count"], machine_type=spec["machine_type"])
        return LoadBalancerResource(**common, type=spec["type"], port=spec["port"], backend_group=spec["backend_group"], health_check_path=spec.get("health_check_path"))

    def _run_provision(self, request_id, request, planned, ctx, provider, operation_id, existing, external_backends=()):
        with bound(request_id=request_id, operation_id=operation_id, identity=ctx.owner):
            event("provisioning_started", provider=provider.name, resources=len(planned))
            created, computes, network, current = [], {}, None, None
            try:
                self._check_quota(provider, request, planned)
                for item in planned:
                    current = item
                    pending = self.state.get_resource(item.spec["resource_id"])
                    self.state.save_resource(pending.model_copy(update={"status": ResourceStatus.CREATING}))
                    if item.kind == "network":
                        resource = network = provider.create_network(request.network, item, ctx)
                    elif item.kind == "storage":
                        resource = provider.create_storage(request.storage, item, ctx)
                    elif item.kind == "compute":
                        group = request.group(item.spec["group"])
                        joined = network or existing.get(group.network_id)
                        resource = provider.create_compute(group, item, ctx, joined)
                        resource = resource.model_copy(update={"network": joined.id if joined is not None else None})
                        computes[item.name] = resource
                    elif item.kind == "cache":
                        joined = network or existing.get(request.cache.network_id)
                        resource = provider.create_cache(request.cache, item, ctx, joined)
                    elif item.kind == "queue":
                        resource = provider.create_queue(next(queue for queue in request.queues() if queue.name == item.spec["queue"]), item, ctx)
                    elif item.kind == "secret":
                        resource = provider.create_secret(next(secret for secret in request.secrets() if secret.name == item.spec["secret"]), item, ctx)
                    elif item.kind == "cluster":
                        joined = network or existing.get(request.cluster.network_id)
                        resource = provider.create_cluster(request.cluster, item, ctx, joined)
                    elif item.kind == "load_balancer":
                        backends = [computes[name] for name in item.spec["backends"] if name in computes] or list(external_backends)
                        group = request.group(request.load_balancer.backend_group)
                        joined = network or existing.get(group.network_id if group else None)
                        resource = provider.create_load_balancer(request.load_balancer, item, ctx, backends, joined)
                    else:
                        joined = network or existing.get(request.database.network_id)
                        if joined is not None and not request.database.public_access:
                            joined = provider.prepare_network_for_database(joined, ctx)
                            self.state.save_resource(joined)
                            if network is not None:  # the request's own network: what cleanup and destroy must see from now on
                                network = joined
                                created = [joined if item.id == joined.id else item for item in created]
                        resource, _ = provider.create_database(request.database, item, ctx, joined)
                        resource = resource.model_copy(update={"network": joined.id if joined is not None and not request.database.public_access else None})
                    resource = resource.model_copy(update={"id": item.spec["resource_id"], "created_at": resource.created_at or self.state_now()})
                    created.append(resource)
                    self.state.save_resource(resource)
                    event("resource_created", kind=item.kind, resource_id=resource.id, name=resource.name, provider_id=resource.provider_id, status=resource.status.value)
                current = None
                for storage in [item for item in created if item.kind == ResourceKind.storage and item.type == "persistent"]:
                    target = next((item.spec.get("attach_to") for item in planned if item.name == storage.name), None)
                    if target and target in computes:
                        current = next(item for item in planned if item.name == storage.name)
                        attached = provider.attach_storage(storage, computes[target], ctx)
                        self.state.save_resource(attached)
                        machine = computes[target].model_copy(update={"attached_disks": [*computes[target].attached_disks, storage.id]})
                        computes[target] = machine
                        self.state.save_resource(machine)
                        event("storage_attached", storage=storage.id, compute=machine.id)
                self.state.set_request_status(request_id, RequestStatus.READY)
                self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED, detail=f"{len(created)} resources created")
                event("provisioning_completed", resources=[item.id for item in created])
            except Exception as exc:  # every failure becomes a structured FAILED state; nothing escapes the worker silently
                cause = exc if isinstance(exc, InfrastructureError) else InfrastructureError(ErrorCode.INTERNAL_ERROR, f"Unexpected {type(exc).__name__}: {str(exc)[:200]}")
                where = f" at {current.kind} {current.name}" if current is not None else ""
                error = InfrastructureError(ErrorCode.PROVISIONING_FAILED, f"Provisioning failed{where}: {cause.message}", {"cause": cause.code.value, **cause.details},
                                            resource=current.spec["resource_id"] if current is not None else None)
                if current is not None and current.spec["resource_id"] not in [item.id for item in created]:
                    failed = self.state.get_resource(current.spec["resource_id"])
                    self.state.save_resource(failed.model_copy(update={"status": ResourceStatus.FAILED, "error": cause.detail()}))
                event("provisioning_failed", error=error.detail().model_dump(mode="json"), created=[item.id for item in created])
                cleanup = self._cleanup(request_id, created, ctx, provider) if self.config.cleanup_on_failure else {"policy": "kept", "deleted": [], "remaining": [item.id for item in created]}
                error.details["cleanup"] = cleanup
                self.state.set_request_status(request_id, RequestStatus.FAILED, error=error.detail().model_dump(mode="json"))
                self.state.finish_operation(operation_id, OperationStatus.FAILED, error=error.detail().model_dump(mode="json"),
                                            detail=f"cleanup: {len(cleanup['deleted'])} deleted, {len(cleanup['remaining'])} remaining")

    @staticmethod
    def state_now():
        from ..state.state_manager import now
        return now()

    def _check_quota(self, provider: CloudProvider, request: InfrastructureRequest, planned: list[PlannedResource]) -> None:
        quota = provider.quota(request.region)
        if not quota:
            event("quota_unavailable", provider=provider.name, region=request.region)
            return
        wanted = {"CPUS": sum(item.spec.get("cpu", 0) for item in planned if item.kind == "compute"),
                  "INSTANCES": sum(1 for item in planned if item.kind == "compute"),
                  "DISKS_TOTAL_GB": sum(item.spec.get("size_gb", 0) for item in planned if item.kind == "storage" and item.spec.get("type") == "persistent")
                  + sum(item.spec.get("boot_disk_gb", 0) for item in planned if item.kind == "compute"),
                  "IN_USE_ADDRESSES": sum(1 for item in planned if item.kind == "compute" and item.spec.get("public_ip"))}
        for metric, needed in wanted.items():
            found = quota.get(metric)
            if needed and found and found.get("limit") is not None and float(found.get("usage") or 0) + needed > float(found["limit"]):
                raise InfrastructureError(ErrorCode.QUOTA_EXCEEDED, f"{metric} quota in {request.region}: {found.get('usage')} of {found['limit']} used, {needed} more needed",
                                          {"metric": metric, "usage": found.get("usage"), "limit": found["limit"], "needed": needed})
        event("quota_checked", region=request.region, wanted=wanted)

    def _delete(self, provider: CloudProvider, resource, ctx: ProvisionContext) -> None:
        provider.delete(resource, ctx)

    def _cleanup(self, request_id, created, ctx, provider) -> dict:
        operation_id = self.state.start_operation(request_id, "cleanup", identity=ctx.owner)
        deleted, remaining = [], []
        with bound(operation_id=operation_id):
            event("cleanup_started", resources=[item.id for item in created])
            for resource in sorted(reversed(created), key=lambda item: DESTROY_ORDER.index(item.kind)):
                try:
                    self._delete(provider, resource, ctx)
                    self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.DELETED}))
                    deleted.append(resource.id)
                    event("resource_deleted", kind=resource.kind.value, resource_id=resource.id, during="cleanup")
                except Exception as exc:
                    cause = exc if isinstance(exc, InfrastructureError) else InfrastructureError(ErrorCode.INTERNAL_ERROR, f"Unexpected {type(exc).__name__}: {str(exc)[:200]}")
                    self.state.save_resource(resource.model_copy(update={"error": cause.detail()}))
                    remaining.append({"resource_id": resource.id, "name": resource.name, "error": cause.detail().model_dump(mode="json")})
            outcome = {"policy": "delete", "deleted": deleted, "remaining": remaining}
            self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED if not remaining else OperationStatus.FAILED, detail=f"{len(deleted)} deleted, {len(remaining)} remaining")
            event("cleanup_completed", deleted=deleted, remaining=[item["resource_id"] for item in remaining])
        return outcome

    # ---- status
    def status(self, request_id: str, principal, refresh: bool = True, wait_seconds: int = 0) -> InfrastructureResponse:
        record = self._record(request_id, principal)
        if wait_seconds:
            self._wait(request_id, wait_seconds)
            record = self.state.get_request(request_id)
        resources = self.state.resources_of(request_id)
        if refresh and record["status"] not in (RequestStatus.DESTROYED, RequestStatus.PROVISIONING, RequestStatus.DESTROYING):
            resources = [self._refresh(record, resource) for resource in resources]
        return self._response(record, resources)

    def _refresh(self, record, resource):
        if resource.provider_id is None or resource.status in (ResourceStatus.PENDING, ResourceStatus.DELETED, ResourceStatus.FAILED):
            return resource
        try:
            fresh = self.registry.get(record["provider"]).refresh(resource)
        except InfrastructureError as exc:
            fresh = resource.model_copy(update={"status": ResourceStatus.UNKNOWN, "error": exc.detail()})
        if fresh != resource:
            self.state.save_resource(fresh)
        return fresh

    def _response(self, record: dict, resources) -> InfrastructureResponse:
        grouped = Resources()
        for resource in resources:
            getattr(grouped, resource.kind.value).append(resource)
        database, cache, cluster, balancer = (next(iter(items), None) for items in (grouped.database, grouped.cache, grouped.cluster, grouped.load_balancer))
        endpoints = Endpoints(public_ips=[item.public_ip for item in grouped.compute if item.public_ip], internal_ips=[item.internal_ip for item in grouped.compute if item.internal_ip],
                              database_host=database.host if database else None, database_port=database.port if database and database.host else None,
                              bucket_urls=[item.url for item in grouped.storage if item.url], load_balancer_address=balancer.address if balancer else None,
                              cache_host=cache.host if cache else None, cache_port=cache.port if cache and cache.host else None, cluster_endpoint=cluster.endpoint if cluster else None,
                              queue_topics=[item.topic for item in grouped.queue if item.status == ResourceStatus.READY],
                              secret_names=[item.resource_name for item in grouped.secret if item.status == ResourceStatus.READY])
        return InfrastructureResponse(request_id=record["request_id"], name=record["name"], provider=record["provider"], region=record["region"], zone=record["zone"],
                                      status=record["status"], owner=record["owner"], labels=record["labels"], resources=grouped, endpoints=endpoints,
                                      operations=self.state.operations_of(record["request_id"]), estimated_monthly_cost_usd=record["estimated_cost"],
                                      error=record["error"], created_at=record["created_at"], updated_at=record["updated_at"])

    def list_requests(self, principal, status: str | None = None, limit: int = 50) -> ListResponse:
        if status is not None and status not in RequestStatus.__members__:
            raise InfrastructureError(ErrorCode.INVALID_REQUEST, f"status must be one of {', '.join(RequestStatus.__members__)}", {"field": "status"})
        owner = None if principal.scope == "all" else principal.name
        records = self.state.list_requests(owner=owner, status=status, limit=max(1, min(limit, 200)))
        found = [self._response(record, self.state.resources_of(record["request_id"])) for record in records]
        return ListResponse(requests=found, count=len(found))

    # ---- destroy
    def destroy(self, request_id: str, principal, wait_seconds: int = 0) -> InfrastructureResponse:
        self.require(principal, "destroy")
        record = self._record(request_id, principal)
        if record["status"] in (RequestStatus.PROVISIONING, RequestStatus.DESTROYING):
            raise InfrastructureError(ErrorCode.INVALID_STATE, f"Request {request_id} is {record['status'].value}; wait for it to finish", {"status": record["status"].value})
        if record["status"] == RequestStatus.DESTROYED:
            raise InfrastructureError(ErrorCode.INVALID_STATE, f"Request {request_id} is already destroyed", {"status": "DESTROYED"})
        for network in (item for item in self.state.resources_of(request_id) if item.kind == ResourceKind.network and item.status != ResourceStatus.DELETED):
            tenants = [item for item in self.state.resources_in_network(network.id) if item.request_id != request_id]
            if tenants:
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"Network {network.id} of this request still carries {len(tenants)} machines or databases of other requests; "
                                          "destroy or delete those first", {"network": network.id, "tenants": [item.id for item in tenants]})
        provider = self.registry.get(record["provider"])
        self.state.set_request_status(request_id, RequestStatus.DESTROYING)
        operation_id = self.state.start_operation(request_id, "destroy", identity=principal.name)
        self.executor.submit(self._run_destroy, request_id, self._context(record), provider, operation_id)
        if wait_seconds:
            self._wait(request_id, wait_seconds)
        return self.status(request_id, principal, refresh=False)

    def _run_destroy(self, request_id, ctx, provider, operation_id):
        with bound(request_id=request_id, operation_id=operation_id, identity=ctx.owner):
            resources = [item for item in self.state.resources_of(request_id) if item.provider_id and item.status != ResourceStatus.DELETED]
            event("destruction_started", provider=provider.name, resources=[item.id for item in resources])
            remaining = []
            for resource in sorted(resources, key=lambda item: DESTROY_ORDER.index(item.kind)):
                self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.DELETING}))
                try:
                    self._delete(provider, resource, ctx)
                    self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.DELETED, "error": None}))
                    event("resource_deleted", kind=resource.kind.value, resource_id=resource.id, name=resource.name)
                except Exception as exc:
                    cause = exc if isinstance(exc, InfrastructureError) else InfrastructureError(ErrorCode.INTERNAL_ERROR, f"Unexpected {type(exc).__name__}: {str(exc)[:200]}")
                    self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.FAILED, "error": cause.detail()}))
                    remaining.append({"resource_id": resource.id, "name": resource.name, "error": cause.detail().model_dump(mode="json")})
            if remaining:
                error = InfrastructureError(ErrorCode.DESTROY_FAILED, f"{len(remaining)} of {len(resources)} resources could not be deleted; call destroy_infrastructure again once the cause is fixed",
                                            {"remaining": remaining})
                self.state.set_request_status(request_id, RequestStatus.DESTROY_FAILED, error=error.detail().model_dump(mode="json"))
                self.state.finish_operation(operation_id, OperationStatus.FAILED, error=error.detail().model_dump(mode="json"), detail=f"{len(remaining)} remaining")
                event("destruction_failed", remaining=[item["resource_id"] for item in remaining])
            else:
                self.state.set_request_status(request_id, RequestStatus.DESTROYED)
                self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED, detail=f"{len(resources)} resources deleted")
                event("destruction_completed", resources=[item.id for item in resources])

    # ---- single-resource primitives
    def create_compute(self, principal, *, region, cpu, memory_gb, provider="gcp", count=1, os="ubuntu", boot_disk_gb=20, public_ip=False, network_id=None, accelerator=None,
                       accelerator_count=1, zone=None, name=None, labels=None, wait_seconds=0) -> InfrastructureResponse:
        gpus = AcceleratorRequest(type=accelerator, count=accelerator_count) if accelerator else None
        compute = ComputeRequest(count=count, cpu=cpu, memory_gb=memory_gb, os=os, boot_disk_gb=boot_disk_gb, public_ip=public_ip, network_id=network_id, accelerator=gpus)
        return self.provision(InfrastructureRequest(provider=provider, region=region, zone=zone, name=name, labels=labels or {}, compute=compute), principal, wait_seconds)

    def create_cache(self, principal, *, region, provider="gcp", memory_gb=1, high_availability=False, version=None, network_id=None, name=None, labels=None, wait_seconds=0):
        cache = CacheRequest(memory_gb=memory_gb, high_availability=high_availability, version=version, network_id=network_id)
        return self.provision(InfrastructureRequest(provider=provider, region=region, name=name, labels=labels or {}, cache=cache), principal, wait_seconds)

    def create_queue(self, principal, *, region, queue_name, provider="gcp", retention_hours=168, subscription=True, ack_deadline_seconds=20, name=None, labels=None, wait_seconds=0):
        queue = QueueRequest(name=queue_name, retention_hours=retention_hours, subscription=subscription, ack_deadline_seconds=ack_deadline_seconds)
        return self.provision(InfrastructureRequest(provider=provider, region=region, name=name, labels=labels or {}, queue=queue), principal, wait_seconds)

    def create_secret(self, principal, *, region, secret_name, provider="gcp", generate_value=False, name=None, labels=None, wait_seconds=0):
        secret = SecretRequest(name=secret_name, generate_value=generate_value)
        return self.provision(InfrastructureRequest(provider=provider, region=region, name=name, labels=labels or {}, secret=secret), principal, wait_seconds)

    def create_cluster(self, principal, *, region, provider="gcp", node_count=1, node_cpu=2, node_memory_gb=8, node_disk_gb=50, kubernetes_version=None, private_nodes=False,
                       network_id=None, zone=None, name=None, labels=None, wait_seconds=0):
        cluster = ClusterRequest(node_count=node_count, node_cpu=node_cpu, node_memory_gb=node_memory_gb, node_disk_gb=node_disk_gb, kubernetes_version=kubernetes_version,
                                 private_nodes=private_nodes, network_id=network_id)
        return self.provision(InfrastructureRequest(provider=provider, region=region, zone=zone, name=name, labels=labels or {}, cluster=cluster), principal, wait_seconds)

    def create_load_balancer(self, principal, *, region, backends, provider="gcp", type="http", port=80, health_check_path="/", name=None, labels=None, wait_seconds=0):
        balancer = LoadBalancerRequest(type=type, port=port, health_check_path=health_check_path, backends=backends)
        return self.provision(InfrastructureRequest(provider=provider, region=region, name=name, labels=labels or {}, load_balancer=balancer), principal, wait_seconds)

    def resource_kinds(self) -> ResourceKindsResponse:
        """What can be asked for: every kind of the contract with its fields, and which configured adapters provide it."""
        providers = {name: self.registry.get(name) for name in self.registry.names()}
        kinds = []
        for kind in RESOURCE_KINDS:
            schema = KIND_MODELS[kind].model_json_schema()
            fields = []
            for field_name, definition in schema.get("properties", {}).items():
                kind_of = definition.get("type") or " | ".join(item.get("type", "object") for item in definition.get("anyOf", [])) or "object"
                fields.append(KindField(name=field_name, type=kind_of, required=field_name in schema.get("required", []), default=definition.get("default"),
                                        description=definition.get("description")))
            kinds.append(ResourceKindInfo(kind=kind, description=(schema.get("description") or "").strip(), several_per_request=kind in SEVERAL, fields=fields,
                                          providers=[name for name, provider in providers.items() if kind in provider.supported_kinds]))
        return ResourceKindsResponse(kinds=kinds, providers=list(providers), note="A request block of any other name is refused with UNSUPPORTED_RESOURCE_KIND; "
                                     "network_id on compute, database, cache and cluster joins a network created earlier, and load_balancer.backends names machines created earlier.")

    def create_storage(self, principal, *, region, size_gb, provider="gcp", type="persistent", performance="balanced", zone=None, name=None, labels=None, wait_seconds=0):
        storage = StorageRequest(type=type, size_gb=size_gb, performance=performance)
        return self.provision(InfrastructureRequest(provider=provider, region=region, zone=zone, name=name, labels=labels or {}, storage=storage), principal, wait_seconds)

    def create_network(self, principal, *, region, provider="gcp", public_access=False, cidr="10.10.0.0/24", allowed_ports=None, allowed_sources=None, iap_ssh=True, name=None,
                       labels=None, wait_seconds=0):
        network = NetworkRequest(public_access=public_access, cidr=cidr, allowed_ports=allowed_ports or [80, 443], allowed_sources=allowed_sources or ["0.0.0.0/0"], iap_ssh=iap_ssh)
        return self.provision(InfrastructureRequest(provider=provider, region=region, name=name, labels=labels or {}, network=network), principal, wait_seconds)

    def create_database(self, principal, *, region, provider="gcp", engine="postgresql", version=None, size="small", storage_gb=10, high_availability=False, public_access=False,
                        database_name="app", username="app", network_id=None, name=None, labels=None, wait_seconds=0):
        database = DatabaseRequest(engine=engine, version=version, size=size, storage_gb=storage_gb, high_availability=high_availability, public_access=public_access,
                                   database_name=database_name, username=username, network_id=network_id)
        return self.provision(InfrastructureRequest(provider=provider, region=region, name=name, labels=labels or {}, database=database), principal, wait_seconds)

    def get_resource(self, resource_id: str, principal, kind: ResourceKind | None = None) -> ResourceResponse:
        resource, record = self._resource(resource_id, principal)
        if kind is not None and resource.kind != kind:
            raise InfrastructureError(ErrorCode.NOT_FOUND, f"{resource_id} is a {resource.kind.value}, not a {kind.value}", {"resource_id": resource_id})
        fresh = self._refresh(record, resource)
        return ResourceResponse(status=fresh.status.value, request_id=fresh.request_id, resource=fresh)

    def delete_resource(self, resource_id: str, principal, kind: ResourceKind, wait_seconds: int = 0) -> ResourceResponse:
        self.require(principal, "destroy")
        resource, record = self._resource(resource_id, principal)
        if resource.kind != kind:
            raise InfrastructureError(ErrorCode.NOT_FOUND, f"{resource_id} is a {resource.kind.value}, not a {kind.value}", {"resource_id": resource_id})
        if resource.status in (ResourceStatus.DELETED, ResourceStatus.DELETING, ResourceStatus.CREATING, ResourceStatus.PENDING):
            raise InfrastructureError(ErrorCode.INVALID_STATE, f"{resource_id} is {resource.status.value}", {"status": resource.status.value})
        if kind == ResourceKind.network:
            tenants = self.state.resources_in_network(resource_id)
            if tenants:
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"The network still carries {len(tenants)} machines or databases; delete those first",
                                          {"resource_id": resource_id, "tenants": [item.id for item in tenants]})
        provider = self.registry.get(record["provider"])
        operation_id = self.state.start_operation(record["request_id"], f"delete_{kind.value}", resource_id=resource_id, identity=principal.name)
        self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.DELETING}))
        self.executor.submit(self._run_delete_one, record, resource, provider, operation_id, self._context(record))
        if wait_seconds:
            self._wait_operation(operation_id, wait_seconds)
        fresh = self.state.get_resource(resource_id)
        return ResourceResponse(status=fresh.status.value, request_id=fresh.request_id, resource=fresh, operation=self.state.get_operation(operation_id))

    def _run_delete_one(self, record, resource, provider, operation_id, ctx):
        with bound(request_id=record["request_id"], operation_id=operation_id, identity=ctx.owner):
            event("destruction_started", resources=[resource.id], single=True)
            try:
                self._delete(provider, resource, ctx)
                self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.DELETED, "error": None, "attached_to": None} if resource.kind == ResourceKind.storage
                                                             else {"status": ResourceStatus.DELETED, "error": None}))
                self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED, detail="deleted")
                event("resource_deleted", kind=resource.kind.value, resource_id=resource.id)
                if all(item.status == ResourceStatus.DELETED for item in self.state.resources_of(record["request_id"])):
                    self.state.set_request_status(record["request_id"], RequestStatus.DESTROYED)
                    event("destruction_completed", resources=[resource.id], reason="last resource of the request")
            except Exception as exc:
                cause = exc if isinstance(exc, InfrastructureError) else InfrastructureError(ErrorCode.INTERNAL_ERROR, f"Unexpected {type(exc).__name__}: {str(exc)[:200]}")
                self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.FAILED, "error": cause.detail()}))
                self.state.finish_operation(operation_id, OperationStatus.FAILED, error=cause.detail().model_dump(mode="json"))
                event("destruction_failed", resource_id=resource.id, error=cause.detail().model_dump(mode="json"))

    def resize_compute(self, resource_id: str, cpu: int, memory_gb: int, principal, wait_seconds: int = 0) -> ResourceResponse:
        self.require(principal, "provision")
        resource, record = self._resource(resource_id, principal)
        if resource.kind != ResourceKind.compute:
            raise InfrastructureError(ErrorCode.NOT_FOUND, f"{resource_id} is a {resource.kind.value}, not a machine", {"resource_id": resource_id})
        if resource.status not in (ResourceStatus.READY, ResourceStatus.STOPPED):
            raise InfrastructureError(ErrorCode.INVALID_STATE, f"{resource_id} is {resource.status.value}; a machine is resized when READY or STOPPED", {"status": resource.status.value})
        limits = self.config.limits
        if cpu > limits.max_cpu_per_instance or memory_gb > limits.max_memory_gb_per_instance:
            raise InfrastructureError(ErrorCode.LIMIT_EXCEEDED, f"At most {limits.max_cpu_per_instance} CPUs and {limits.max_memory_gb_per_instance} GB per machine")
        provider = self.registry.get(record["provider"])
        operation_id = self.state.start_operation(record["request_id"], "resize_compute", resource_id=resource_id, identity=principal.name)
        self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.UPDATING}))

        def run():
            with bound(request_id=record["request_id"], operation_id=operation_id, identity=principal.name):
                try:
                    resized = provider.resize_compute(resource, cpu, memory_gb, self._context(record))
                    self.state.save_resource(resized)
                    self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED, detail=f"{resized.machine_type}")
                    event("resource_resized", resource_id=resource_id, cpu=cpu, memory_gb=memory_gb)
                except Exception as exc:
                    cause = exc if isinstance(exc, InfrastructureError) else InfrastructureError(ErrorCode.INTERNAL_ERROR, f"Unexpected {type(exc).__name__}: {str(exc)[:200]}")
                    self.state.save_resource(self._refresh(record, resource.model_copy(update={"error": cause.detail()})))
                    self.state.finish_operation(operation_id, OperationStatus.FAILED, error=cause.detail().model_dump(mode="json"))
        self.executor.submit(run)
        if wait_seconds:
            self._wait_operation(operation_id, wait_seconds)
        fresh = self.state.get_resource(resource_id)
        return ResourceResponse(status=fresh.status.value, request_id=fresh.request_id, resource=fresh, operation=self.state.get_operation(operation_id))

    def attach_storage(self, storage_id: str, compute_id: str, principal) -> ResourceResponse:
        self.require(principal, "provision")
        storage, record = self._resource(storage_id, principal)
        compute, other = self._resource(compute_id, principal)
        if storage.kind != ResourceKind.storage or compute.kind != ResourceKind.compute:
            raise InfrastructureError(ErrorCode.NOT_FOUND, "attach_storage takes a storage id (sto-…) and a compute id (cmp-…)", {"storage_id": storage_id, "compute_id": compute_id})
        if storage.attached_to:
            raise InfrastructureError(ErrorCode.INVALID_STATE, f"{storage_id} is already attached to {storage.attached_to}", {"attached_to": storage.attached_to})
        if storage.status != ResourceStatus.READY or compute.status != ResourceStatus.READY:
            raise InfrastructureError(ErrorCode.INVALID_STATE, "Both the disk and the machine must be READY", {"storage": storage.status.value, "compute": compute.status.value})
        provider = self.registry.get(record["provider"])
        operation_id = self.state.start_operation(record["request_id"], "attach_storage", resource_id=storage_id, identity=principal.name)
        with bound(request_id=record["request_id"], operation_id=operation_id, identity=principal.name):
            try:
                attached = provider.attach_storage(storage, compute, self._context(record))
            except InfrastructureError as exc:
                self.state.finish_operation(operation_id, OperationStatus.FAILED, error=exc.detail().model_dump(mode="json"))
                raise
            self.state.save_resource(attached)
            self.state.save_resource(compute.model_copy(update={"attached_disks": [*compute.attached_disks, storage_id]}))
            self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED, detail=f"attached to {compute_id}")
            event("storage_attached", storage=storage_id, compute=compute_id)
        return ResourceResponse(status=attached.status.value, request_id=attached.request_id, resource=attached, operation=self.state.get_operation(operation_id))

    def configure_firewall(self, network_id: str, ports: list[int], sources: list[str], principal) -> ResourceResponse:
        self.require(principal, "provision")
        ports, sources = validation.check_ports(ports, "ports"), validation.check_sources(sources)
        network, record = self._resource(network_id, principal)
        if network.kind != ResourceKind.network:
            raise InfrastructureError(ErrorCode.NOT_FOUND, f"{network_id} is a {network.kind.value}, not a network", {"resource_id": network_id})
        if network.status != ResourceStatus.READY:
            raise InfrastructureError(ErrorCode.INVALID_STATE, f"{network_id} is {network.status.value}", {"status": network.status.value})
        provider = self.registry.get(record["provider"])
        operation_id = self.state.start_operation(record["request_id"], "configure_firewall", resource_id=network_id, identity=principal.name)
        with bound(request_id=record["request_id"], operation_id=operation_id, identity=principal.name):
            try:
                rule = provider.configure_firewall(network, ports, sources, self._context(record))
            except InfrastructureError as exc:
                self.state.finish_operation(operation_id, OperationStatus.FAILED, error=exc.detail().model_dump(mode="json"))
                raise
            updated = network.model_copy(update={"firewall_rules": [*network.firewall_rules, rule]})
            self.state.save_resource(updated)
            self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED, detail=rule.name)
            event("firewall_configured", network=network_id, rule=rule.name, ports=ports, sources=sources)
        return ResourceResponse(status=updated.status.value, request_id=updated.request_id, resource=updated, operation=self.state.get_operation(operation_id))

    def list_compute(self, principal, request_id: str | None = None) -> dict:
        labels = {"managed-by": "cloudinfra"}
        if request_id:
            labels["cloudinfra-request"] = validation.check_request_id(request_id)
        if principal.scope != "all":
            labels["cloudinfra-owner"] = principal.name
        found = [instance for name in self.registry.names() for instance in self.registry.get(name).list_compute(labels)]
        return {"instances": [item.model_dump(mode="json") for item in found], "count": len(found), "labels": labels}

    # ---- operations
    def health(self, resource_id: str, principal) -> Health:
        resource, record = self._resource(resource_id, principal)
        return self.registry.get(record["provider"]).resource_health(resource)

    def usage(self, resource_id: str, minutes: int, principal) -> Usage:
        resource, record = self._resource(resource_id, principal)
        return self.registry.get(record["provider"]).resource_usage(resource, max(5, min(minutes, 1440)))

    def cost(self, principal, request_id: str | None = None, resource_id: str | None = None) -> dict:
        if not request_id and not resource_id:
            raise InfrastructureError(ErrorCode.INVALID_REQUEST, "Give request_id or resource_id")
        if resource_id:
            resource, record = self._resource(resource_id, principal)
        else:
            record, resource = self._record(request_id, principal), None
        provider = self.registry.get(record["provider"])
        request = InfrastructureRequest.model_validate(record["request"])
        estimate = provider.estimate_cost(planner.plan(request, self._context(record), provider))
        if resource is not None:
            lines = [line for line in estimate.lines if line.resource == resource.name]
            estimate = estimate.model_copy(update={"lines": lines, "monthly_usd": round(sum(line.monthly_usd for line in lines), 2)})
        return {"request_id": record["request_id"], "resource_id": resource_id, "status": record["status"].value, "estimate": estimate.model_dump(mode="json"),
                "actual": None, "note": "Actual spend needs a billing export; this service reports estimates from list prices only."}

    def operation(self, operation_id: str, principal):
        found = self.state.get_operation(operation_id)
        if found is None:
            raise InfrastructureError(ErrorCode.NOT_FOUND, f"No operation {operation_id}", {"operation_id": operation_id})
        self._record(found.request_id, principal)
        return found

    def service_status(self, version: str) -> dict:
        providers = {}
        for name in self.registry.names():
            settings = getattr(self.config.providers, name)
            providers[name] = {"project": getattr(settings, "project_id", None), "allowed_regions": list(settings.allowed_regions), "planned": False}
        from ..providers import PLANNED_PROVIDERS
        return {"version": version, "providers": providers, "planned_providers": list(PLANNED_PROVIDERS), "limits": self.config.limits.model_dump(),
                "cleanup_on_failure": self.config.cleanup_on_failure, "workers": self.config.workers, "labels": self.config.labels}

    # ---- waiting
    def _wait(self, request_id: str, seconds: int) -> None:
        deadline = self.clock() + max(0, min(seconds, MAX_WAIT_SECONDS))
        while True:
            record = self.state.get_request(request_id)
            if record is None or record["status"] not in (RequestStatus.PROVISIONING, RequestStatus.DESTROYING) or self.clock() >= deadline:
                return
            self.sleep(POLL_SECONDS)

    def _wait_operation(self, operation_id: str, seconds: int) -> None:
        deadline = self.clock() + max(0, min(seconds, MAX_WAIT_SECONDS))
        while True:
            found = self.state.get_operation(operation_id)
            if found is None or found.status != OperationStatus.RUNNING or self.clock() >= deadline:
                return
            self.sleep(POLL_SECONDS)
