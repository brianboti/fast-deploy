"""The orchestrator: REQUEST → VALIDATE → PLAN → PROVISION → READY → QUERY → DESTROY, with FAILED and cleanup on the way.

It owns the order (network, storage, compute, database; the reverse to destroy), the state, the audit trail and the
authorization of each caller. Providers implement primitives only. Provisioning and destruction run in bounded workers so
a tool call returns at once; a caller may wait up to 45 seconds for the outcome and otherwise polls the status.
"""
import datetime
import secrets
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor

from ..models.errors import ErrorCode, InfrastructureError
from ..models.request import (
    RESOURCE_KINDS,
    AcceleratorRequest,
    AutostopRequest,
    BudgetRequest,
    CacheRequest,
    ClusterRequest,
    ComputeRequest,
    DatabaseRequest,
    DnsRequest,
    IdentityRequest,
    InfrastructureRequest,
    JobRequest,
    LifetimeRequest,
    LoadBalancerRequest,
    MonitorRequest,
    NetworkRequest,
    NoSqlRequest,
    PublicRequest,
    QueueRequest,
    ScheduleRequest,
    SecretRequest,
    ServiceRequest,
    StorageRequest,
    WarehouseRequest,
)
from ..models.resources import (
    BudgetResource,
    CacheResource,
    ClusterResource,
    ComputeResource,
    DatabaseResource,
    DnsResource,
    Grant,
    IdentityResource,
    JobResource,
    LoadBalancerResource,
    MonitorResource,
    NetworkResource,
    NoSqlResource,
    QueueResource,
    ResourceKind,
    ResourceStatus,
    SecretResource,
    ServiceResource,
    StorageResource,
    WarehouseResource,
)
from ..models.response import (
    AccessGrant,
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
    ProfileInfo,
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
from . import access as access_rules
from . import lifecycle, planner, validator

MAX_WAIT_SECONDS = 45
POLL_SECONDS = 0.5
DESTROY_ORDER = tuple(ResourceKind(kind) for kind in reversed(RESOURCE_KINDS))
KIND_MODELS = {"network": NetworkRequest, "storage": StorageRequest, "compute": ComputeRequest, "database": DatabaseRequest, "cache": CacheRequest, "queue": QueueRequest,
               "secret": SecretRequest, "cluster": ClusterRequest, "service": ServiceRequest, "job": JobRequest, "identity": IdentityRequest, "dns": DnsRequest, "monitor": MonitorRequest,
               "budget": BudgetRequest,
               "warehouse": WarehouseRequest,
               "nosql": NoSqlRequest,
               "load_balancer": LoadBalancerRequest}
SEVERAL = ("compute", "queue", "secret", "cluster", "service", "job", "identity", "warehouse", "nosql")
LIVE = (RequestStatus.PROVISIONING, RequestStatus.READY, RequestStatus.STOPPING, RequestStatus.STOPPED, RequestStatus.STARTING, RequestStatus.FAILED,
        RequestStatus.DESTROYING, RequestStatus.DESTROY_FAILED)
MOVING = (RequestStatus.PROVISIONING, RequestStatus.DESTROYING, RequestStatus.STOPPING, RequestStatus.STARTING)  # a worker is changing the request
CONTROL_ACTIONS = ("stop", "start", "extend", "keep_awake")
ACCESS_ACTIONS = ("grant", "revoke")
MAX_HOURS = 8760


class InlineExecutor:
    """Runs submitted work at once, on the caller's thread: for tests and the command line."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def shutdown(self, wait=True, cancel_futures=False):
        return None


class Orchestrator:
    def __init__(self, config, state: StateManager, registry, executor=None, sleep=time.sleep, clock=time.monotonic, wallclock=None):
        self.config, self.state, self.registry, self.sleep, self.clock = config, state, registry, sleep, clock
        self.wallclock = wallclock or (lambda: datetime.datetime.now(datetime.UTC))  # expiry and idle stop run on this clock
        self._locks: weakref.WeakValueDictionary[str, threading.RLock] = weakref.WeakValueDictionary()  # an entry lasts while its lock is held
        self._locks_guard = threading.Lock()
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

    def lock(self, request_id: str) -> threading.RLock:
        """The lock that serializes changes to one request's status, lifecycle, access list and recorded grants in this
        process, so that a caller, a worker finishing and the sweeper never interleave their read-modify-writes. One process
        per state file, which the server's ProcessLock holds."""
        with self._locks_guard:
            found = self._locks.get(request_id)
            if found is None:
                found = self._locks[request_id] = threading.RLock()
            return found

    def _submit(self, request_id: str, operation_id: str, revert, fn, *args) -> None:
        """Hand work to the workers. When they take no more (the service is shutting down), put the request back as it was
        and say so, rather than leave it in a state no worker will finish."""
        try:
            self.executor.submit(fn, *args)
        except RuntimeError as exc:
            revert()
            error = InfrastructureError(ErrorCode.INTERNAL_ERROR, "The service is shutting down and took no new work; the request is as it was. Ask again once "
                                        "the service is back", {"request_id": request_id})
            self.state.finish_operation(operation_id, OperationStatus.FAILED, error=error.detail().model_dump(mode="json"))
            raise error from exc

    def recover(self) -> list[dict]:
        """After a restart no worker survives, so a request left PROVISIONING, STOPPING, STARTING or DESTROYING, and every
        RUNNING operation, was interrupted. Each request is put where it can be retried or destroyed, and what an interrupted
        provisioning created is cleaned up in the workers as a failure would be. Call before serving."""
        found = []
        for status in MOVING:
            for record in self.state.list_requests(status=status.value, limit=1_000_000):
                request_id = record["request_id"]
                with self.lock(request_id), bound(request_id=request_id, identity="cloudinfra-recovery"):
                    after = self._recover_one(record)
                    event("request_recovered", was=status.value, now=after.value)
                    found.append({"request_id": request_id, "was": status.value, "now": after.value})
        interrupted = InfrastructureError(ErrorCode.INTERNAL_ERROR, "Interrupted by a restart of the service", {"cause": "INTERRUPTED"})
        for operation in self.state.running_operations():
            self.state.finish_operation(operation.operation_id, OperationStatus.FAILED, error=interrupted.detail().model_dump(mode="json"), detail="interrupted")
        return found

    def _recover_one(self, record: dict) -> RequestStatus:
        request_id, status = record["request_id"], record["status"]
        if status == RequestStatus.PROVISIONING:
            for resource in self.state.resources_of(request_id):
                if resource.status in (ResourceStatus.PENDING, ResourceStatus.CREATING):
                    self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.FAILED}))
            # a resource whose create the provider had taken has its id here (ProvisionContext.accepted) and goes with the rest
            created = [item for item in self.state.resources_of(request_id) if item.provider_id and item.status != ResourceStatus.DELETED]
            cleaning = bool(created) and self.config.cleanup_on_failure and record["provider"] in self.registry.names()
            what = ("What it created, or had begun to create, is deleted now, as after any failure" if cleaning
                    else "Destroy the request to delete what it created, or had begun to create")
            error = InfrastructureError(ErrorCode.PROVISIONING_FAILED, f"Provisioning was interrupted by a restart of the service. {what}; only a resource whose "
                                        "create call had not yet been answered at that moment may exist at the provider without an id here, and its labels name "
                                        "this request", {"cause": "INTERRUPTED"})
            self.state.set_request_status(request_id, RequestStatus.FAILED, error=error.detail().model_dump(mode="json"))
            if cleaning:
                self.executor.submit(self._cleanup, request_id, created, self._context(record), self.registry.get(record["provider"]))
            return RequestStatus.FAILED
        if status == RequestStatus.DESTROYING:
            error = InfrastructureError(ErrorCode.DESTROY_FAILED, "Destruction was interrupted by a restart of the service; call destroy_infrastructure again",
                                        {"cause": "INTERRUPTED"})
            self.state.set_request_status(request_id, RequestStatus.DESTROY_FAILED, error=error.detail().model_dump(mode="json"))
            return RequestStatus.DESTROY_FAILED
        # STOPPING or STARTING: STOPPED only when everything that can stop is stopped, as the provider sees it now
        try:
            found = self._stoppable(record, self.registry.get(record["provider"]))
        except InfrastructureError:
            found = []
        stopped = bool(found) and all(item.status == ResourceStatus.STOPPED for item in found)
        life = dict(record["lifecycle"] or {})
        if stopped and not life.get("stop_reason"):
            life.update(stopped_at=lifecycle.iso(self.wallclock()), stop_reason="requested")
        elif not stopped:
            for key in ("stopped_at", "stop_reason", "retain_until"):
                life.pop(key, None)
        self.state.set_lifecycle(request_id, life)
        after = RequestStatus.STOPPED if stopped else RequestStatus.READY
        self.state.set_request_status(request_id, after)
        return after

    @staticmethod
    def _cause(exc: Exception) -> InfrastructureError:
        """Any exception as a structured error: an InfrastructureError as it is, anything else as INTERNAL_ERROR."""
        return exc if isinstance(exc, InfrastructureError) else InfrastructureError(ErrorCode.INTERNAL_ERROR, f"Unexpected {type(exc).__name__}: {str(exc)[:200]}")

    # ---- validate and plan
    def _prepare(self, request: InfrastructureRequest, principal, request_id: str):
        provider = self.registry.get(request.provider)
        problems = validator.validate(request, self.config, provider) + access_rules.problems(request, self.config, principal)
        validator.raise_first(problems)
        ctx = planner.context(request, request_id, principal.name, self.config, provider)
        planned = planner.plan(request, ctx, provider, self.config)
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
        found = validator.validate(request, self.config, provider) + access_rules.problems(request, self.config, principal)
        problems.extend(ErrorDetail(code=item.code, message=item.message, details={"field": item.field} if item.field else {}) for item in found)
        if not found:
            ctx = planner.context(request, new_id(), principal.name, self.config, provider)
            estimate = provider.estimate_cost(planner.plan(request, ctx, provider, self.config))
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
                                warnings=planner.warnings(request, planned, self.config, provider, principal), quota=quota)

    # ---- provision
    def provision(self, request: InfrastructureRequest, principal, wait_seconds: int = 0) -> InfrastructureResponse:
        self.require(principal, "provision")
        if request.lifetime is not None and not getattr(principal, "can_destroy", False):
            # every lifetime ends in a destroy, at once or after the retention: asking for one is asking to destroy later
            raise InfrastructureError(ErrorCode.PERMISSION_DENIED, f"Identity {principal.name} may not destroy infrastructure, and a lifetime ends in a destroy; "
                                      "leave lifetime out, or ask the operator for can_destroy", {"identity": principal.name, "action": "destroy", "field": "lifetime"})
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
            if request.budget is not None and request.budget.request_id:  # a budget may watch only a request its caller may see
                self._record(request.budget.request_id, principal)
            # The database password is generated here, written by the worker to a secret the request owns, and dropped.
            password = secrets.token_urlsafe(24) if request.database else None
            ctx.database_password = password
            self.state.create_request(request_id, ctx.name, provider.name, request.region, ctx.zone, principal.name, RequestStatus.PROVISIONING, request.model_dump(mode="json"),
                                      ctx.labels, estimate.monthly_usd)
            life = lifecycle.initial(request, self.config.limits, self.wallclock())
            if life:
                self.state.set_lifecycle(request_id, life)
            members = access_rules.members(request, principal, self.config)
            if members:
                self.state.set_access(request_id, members)
            links = self._links(planned)
            for item in planned:
                self.state.save_resource(self._pending(item, request, ctx, provider.name, links))
            operation_id = self.state.start_operation(request_id, "provision", identity=principal.name)
            refused = InfrastructureError(ErrorCode.PROVISIONING_FAILED, "The service was shutting down; nothing was created", {"cause": "SHUTTING_DOWN"})
            self._submit(request_id, operation_id, lambda: self.state.set_request_status(request_id, RequestStatus.FAILED, error=refused.detail().model_dump(mode="json")),
                         self._run_provision, request_id, request, planned, ctx, provider, operation_id, existing, external_backends)
            if wait_seconds:
                self._wait(request_id, wait_seconds)
            response = self.status(request_id, principal, refresh=False)
            response.warnings = [*planner.warnings(request, planned, self.config, provider, principal), *response.warnings]
            if request.database or request.cache:
                response.credentials = Credentials(database_password_secret=links.get("database_password"), cache_auth_secret=links.get("cache_auth"),
                                                   database_password=password if self.config.return_database_password else None)
            return response

    @staticmethod
    def _links(planned: list[PlannedResource]) -> dict[str, str]:
        """The secrets a plan makes for the request's own credentials, by purpose: {database_password: sec-…, cache_auth: sec-…}."""
        return {item.spec["purpose"]: item.spec["resource_id"] for item in planned if item.kind == "secret" and item.spec.get("purpose")}

    def _existing_networks(self, request: InfrastructureRequest, principal, provider: CloudProvider) -> dict[str, NetworkResource]:
        """The networks a request names by id (network_id on compute, database, cache, cluster): visible, ready, same provider and region."""
        found = {}
        for block in (*request.compute_groups(), request.database, request.cache, *request.clusters(), *request.services(), *request.jobs()):
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
    def _pending(item: PlannedResource, request: InfrastructureRequest, ctx: ProvisionContext, provider: str, links: dict[str, str]):
        common = {"id": item.spec["resource_id"], "name": item.name, "request_id": ctx.request_id, "provider": provider, "region": ctx.region, "status": ResourceStatus.PENDING}
        spec = item.spec
        if item.kind == "compute":
            accelerator = f"{spec['accelerator']['type']} x{spec['accelerator']['count']}" if spec.get("accelerator") else None
            return ComputeResource(**common, zone=spec["zone"], group=spec["group"], machine_type=spec["machine_type"], cpu=spec["cpu"], memory_gb=spec["memory_gb"],
                                   os_image=spec["image"].rsplit("/", 1)[-1], boot_disk_gb=spec["boot_disk_gb"], accelerator=accelerator, profile=spec.get("profile"),
                                   secure_boot=spec.get("secure_boot"), autostop_idle_minutes=spec.get("autostop_idle_minutes"), max_run_hours=spec.get("max_run_hours"))
        if item.kind == "storage":
            return StorageResource(**common, zone=spec.get("zone"), type=spec["type"], size_gb=spec["size_gb"], performance=request.storage.performance)
        if item.kind == "warehouse":
            return WarehouseResource(**common, dataset="", location=ctx.region)
        if item.kind == "nosql":
            return NoSqlResource(**common, database=item.name, location=ctx.region)
        if item.kind == "monitor":
            return MonitorResource(**common, notify=list(request.monitor.notify))
        if item.kind == "budget":
            return BudgetResource(**common, monthly_amount=request.budget.monthly_amount, notify=list(request.budget.notify), watches=spec["watch"])
        if item.kind == "dns":
            return DnsResource(**common, dns_name=spec["zone"])
        if item.kind == "identity":
            return IdentityResource(**common, email="", level=spec["level"])
        if item.kind == "network":
            return NetworkResource(**common, subnet=spec["subnet"], cidr=spec["cidr"], public_access=spec["public_access"])
        if item.kind == "database":
            return DatabaseResource(**common, engine=spec["engine"], version=spec["version"], tier=spec["tier"], storage_gb=spec["storage_gb"], high_availability=spec["high_availability"],
                                    public_access=not spec["private"], port=spec["port"], database=request.database.database_name, username=request.database.username,
                                    password_secret=links.get("database_password"))
        if item.kind == "cache":
            return CacheResource(**common, version=spec["version"], tier=spec["tier"], memory_gb=spec["memory_gb"], high_availability=spec["tier"] == "STANDARD_HA",
                                 auth_secret=links.get("cache_auth"))
        if item.kind == "queue":
            return QueueResource(**common, topic=f"pending/{item.name}", retention_hours=spec["retention_hours"])
        if item.kind == "secret":
            return SecretResource(**common, resource_name=f"pending/{item.name}", versions=0, purpose=spec.get("purpose"))
        if item.kind == "cluster":
            return ClusterResource(**common, zone=spec["location"], location=spec["location"], node_count=spec["node_count"], machine_type=spec["machine_type"])
        if item.kind == "job":
            return JobResource(**common, cpu=spec["cpu"], memory_mb=spec["memory_mb"], tasks=spec["tasks"], schedule=spec.get("schedule"))
        if item.kind == "service":
            return ServiceResource(**common, cpu=spec["cpu"], memory_mb=spec["memory_mb"], min_instances=spec["min_instances"], max_instances=spec["max_instances"],
                                   ingress=spec["ingress"], public=spec["public"])
        return LoadBalancerResource(**common, type=spec["type"], port=spec["port"], backend_group=spec["backend_group"], health_check_path=spec.get("health_check_path"))

    def _run_provision(self, request_id, request, planned, ctx, provider, operation_id, existing, external_backends=()):
        with bound(request_id=request_id, operation_id=operation_id, identity=ctx.owner):
            event("provisioning_started", provider=provider.name, resources=len(planned))
            created, computes, network, current = [], {}, None, None
            links = self._links(planned)

            def accepted(resource):
                # the provider took a create: from here the resource is in state with its provider id, so that cleanup, destroy
                # and a restart delete it even when the wait for it fails; one already created (a network gaining parts) is updated
                resource = resource.model_copy(update={"created_at": resource.created_at or self.state_now()})
                self.state.save_resource(resource)
                created[:] = [resource if item.id == resource.id else item for item in created]
            ctx.on_accepted = accepted
            try:
                self._check_quota(provider, request, planned)
                for item in planned:
                    current = item
                    pending = self.state.get_resource(item.spec["resource_id"])
                    self.state.save_resource(pending.model_copy(update={"status": ResourceStatus.CREATING}))
                    if item.kind == "warehouse":
                        resource = provider.create_warehouse(next(item_ for item_ in request.warehouses() if item_.warehouse_name == item.spec["warehouse"]), item, ctx)
                    elif item.kind == "nosql":
                        resource = provider.create_nosql(next(item_ for item_ in request.nosqls() if item_.nosql_name == item.spec["nosql"]), item, ctx)
                    elif item.kind == "monitor":
                        resource = provider.create_monitor(request.monitor, item, ctx, created)
                    elif item.kind == "budget":
                        resource = provider.create_budget(request.budget, item, ctx)
                    elif item.kind == "dns":
                        resource = provider.create_dns(request.dns, item, ctx, created)  # made last: what its records point to is made
                    elif item.kind == "identity":
                        identity = request.identity_named(item.spec["identity"])
                        resource = provider.create_identity(identity, item, ctx)
                        self._add_member(request_id, f"serviceAccount:{resource.email}", identity.level)  # granted with every member, at the end
                    elif item.kind == "network":
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
                        resource = provider.create_cache(request.cache, item, ctx, joined).model_copy(update={"auth_secret": links.get("cache_auth")})
                    elif item.kind == "queue":
                        resource = provider.create_queue(next(queue for queue in request.queues() if queue.name == item.spec["queue"]), item, ctx)
                    elif item.kind == "secret":
                        resource = self._create_secret(request, item, ctx, provider, created)
                    elif item.kind == "cluster":
                        cluster = request.cluster_named(item.spec.get("cluster"))
                        joined = network or existing.get(cluster.network_id)
                        resource = provider.create_cluster(cluster, item, ctx, joined)
                    elif item.kind == "service":
                        service = request.service_named(item.spec["service"])
                        joined = network or existing.get(service.network_id)
                        secrets = [made for made in created if made.kind == ResourceKind.secret]  # its runtime account reads the request's secrets
                        resource = provider.create_service(service, item, ctx, joined, secrets)
                    elif item.kind == "job":
                        job = request.job_named(item.spec["job"])
                        joined = network or existing.get(job.network_id)
                        secrets = [made for made in created if made.kind == ResourceKind.secret]
                        resource = provider.create_job(job, item, ctx, joined, secrets)
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
                        resource = resource.model_copy(update={"network": joined.id if joined is not None and not request.database.public_access else None,
                                                               "password_secret": links.get("database_password")})
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
                current = None
                self._apply_grants(request_id, provider, ctx)
                if not self.state.transition(request_id, (RequestStatus.PROVISIONING,), RequestStatus.READY):
                    raise InfrastructureError(ErrorCode.INVALID_STATE, "The request was changed elsewhere while it was being provisioned", {"cause": "SUPERSEDED"})
                self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED, detail=f"{len(created)} resources created")
                event("provisioning_completed", resources=[item.id for item in created])
            except Exception as exc:  # every failure becomes a structured FAILED state; nothing escapes the worker silently
                cause = self._cause(exc)
                where = f" at {current.kind} {current.name}" if current is not None else ""
                error = InfrastructureError(ErrorCode.PROVISIONING_FAILED, f"Provisioning failed{where}: {cause.message}", {"cause": cause.code.value, **cause.details},
                                            resource=current.spec["resource_id"] if current is not None else None)
                if current is not None and current.spec["resource_id"] not in [item.id for item in created]:
                    failed = self.state.get_resource(current.spec["resource_id"]).model_copy(update={"status": ResourceStatus.FAILED, "error": cause.detail()})
                    self.state.save_resource(failed)
                    if failed.provider_id:  # the provider took its create before the failure: it may exist, so it is cleaned up with the rest
                        created.append(failed)
                event("provisioning_failed", error=error.detail().model_dump(mode="json"), created=[item.id for item in created])
                cleanup = self._cleanup(request_id, created, ctx, provider) if self.config.cleanup_on_failure else {"policy": "kept", "deleted": [], "remaining": [item.id for item in created]}
                error.details["cleanup"] = cleanup
                if not self.state.transition(request_id, (RequestStatus.PROVISIONING,), RequestStatus.FAILED, error=error.detail().model_dump(mode="json")):
                    event("provisioning_superseded", status=self.state.get_request(request_id)["status"].value)  # set elsewhere meanwhile: left as it is
                self.state.finish_operation(operation_id, OperationStatus.FAILED, error=error.detail().model_dump(mode="json"),
                                            detail=f"cleanup: {len(cleanup['deleted'])} deleted, {len(cleanup['remaining'])} remaining")
            finally:
                ctx.database_password = None  # written to its secret, or lost with a failed request; never kept
                ctx.on_accepted = None

    def _create_secret(self, request: InfrastructureRequest, item: PlannedResource, ctx: ProvisionContext, provider: CloudProvider, created: list):
        """A secret the request asked for, or one the service makes for the request's own credentials: the generated
        database password, or the cache's AUTH string copied from the provider. Values are written, never returned or logged."""
        purpose = item.spec.get("purpose")
        if purpose is None:
            return provider.create_secret(next(secret for secret in request.secrets() if secret.name == item.spec["secret"]), item, ctx)
        resource = provider.create_secret(SecretRequest(name=item.spec["secret"]), item, ctx)
        try:
            if purpose == "database_password":
                value = ctx.database_password
            else:
                cache = next(found for found in created if found.kind == ResourceKind.cache)
                value = provider.read_cache_auth(cache)
            if value:
                resource = provider.put_secret_value(resource, value, ctx)
        except Exception:
            # the secret exists without its value: cleanup and destroy must know it, or it outlives the request and its name
            # stays taken
            kept = resource.model_copy(update={"id": item.spec["resource_id"], "created_at": resource.created_at or self.state_now()})
            self.state.save_resource(kept)
            created.append(kept)
            raise
        event("credential_stored", purpose=purpose, resource_id=item.spec["resource_id"])
        return resource

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
                    cause = self._cause(exc)
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
        if refresh and record["status"] not in (RequestStatus.DESTROYED, *MOVING):
            resources = [self._refresh(record, resource) for resource in resources]
        return self._response(record, resources)

    def _refresh(self, record, resource):
        if resource.provider_id is None or resource.status in (ResourceStatus.PENDING, ResourceStatus.DELETED, ResourceStatus.FAILED):
            return resource
        try:
            fresh = self.registry.get(record["provider"]).refresh(resource)
        except InfrastructureError as exc:
            fresh = resource.model_copy(update={"status": ResourceStatus.UNKNOWN, "error": exc.detail()})
        return self._save_observed(fresh) if fresh != resource else fresh

    def _save_observed(self, resource):
        """Save what the provider reports about a resource, read before a slow call. What the service records on it is kept
        as stored now, not as it was when the provider was asked: its grants, written under the request's lock, and a delete
        under way or done."""
        with self.lock(resource.request_id):
            stored = self.state.get_resource(resource.id)
            if stored is not None:
                if stored.status == ResourceStatus.DELETED:
                    return stored
                update = {"access": stored.access}
                if stored.status == ResourceStatus.DELETING and resource.status != ResourceStatus.DELETED:
                    update["status"] = ResourceStatus.DELETING
                resource = resource.model_copy(update=update)
            self.state.save_resource(resource)
        return resource

    def _response(self, record: dict, resources) -> InfrastructureResponse:
        now = self.wallclock()
        if record["status"] not in (RequestStatus.DESTROYED, RequestStatus.DESTROYING) and record["provider"] in self.registry.names():
            provider = self.registry.get(record["provider"])
            known = self._with_references(resources)
            resources = [item.model_copy(update={"connect": provider.connect_info(item, known)}) if item.status in (ResourceStatus.READY, ResourceStatus.STOPPED) else item
                         for item in resources]
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
                                      lifecycle=lifecycle.view(record, resources, now), access=[AccessGrant(**entry) for entry in record["access"]],
                                      warnings=lifecycle.warnings(record, now, self.config.limits.expiry_retention_days),
                                      operations=self.state.operations_of(record["request_id"]),
                                      estimated_monthly_cost_usd=record["estimated_cost"],
                                      error=record["error"], created_at=record["created_at"], updated_at=record["updated_at"])

    def _with_references(self, resources: list) -> list:
        """A request's resources, and those of other requests they name (a network joined, the machine a disk is attached to),
        so that a connect block can name them."""
        have = {item.id for item in resources}
        named = {getattr(item, field, None) for item in resources for field in ("network", "attached_to")} - have - {None}
        return [*resources, *(found for found in (self.state.get_resource(item) for item in sorted(named)) if found is not None)]

    def list_requests(self, principal, status: str | None = None, limit: int = 50, expiring_within_hours: int | None = None) -> ListResponse:
        if status is not None and status not in RequestStatus.__members__:
            raise InfrastructureError(ErrorCode.INVALID_REQUEST, f"status must be one of {', '.join(RequestStatus.__members__)}", {"field": "status"})
        expires_before = None
        if expiring_within_hours is not None:
            if not 1 <= expiring_within_hours <= MAX_HOURS:
                raise InfrastructureError(ErrorCode.INVALID_REQUEST, f"expiring_within_hours must be 1 to {MAX_HOURS}", {"field": "expiring_within_hours"})
            expires_before = lifecycle.iso(self.wallclock() + datetime.timedelta(hours=expiring_within_hours))
        owner = None if principal.scope == "all" else principal.name
        records = self.state.list_requests(owner=owner, status=status, limit=max(1, min(limit, 200)), expires_before=expires_before)
        found = [self._response(record, self.state.resources_of(record["request_id"])) for record in records]
        return ListResponse(requests=found, count=len(found))

    # ---- destroy
    def destroy(self, request_id: str, principal, wait_seconds: int = 0) -> InfrastructureResponse:
        self.require(principal, "destroy")
        self._record(request_id, principal)
        with self.lock(request_id):
            self._begin_destroy(self.state.get_request(request_id), principal)
        if wait_seconds:
            self._wait(request_id, wait_seconds)
        return self.status(request_id, principal, refresh=False)

    def _begin_destroy(self, record: dict, principal) -> None:
        request_id = record["request_id"]
        if record["status"] in MOVING:
            raise InfrastructureError(ErrorCode.INVALID_STATE, f"Request {request_id} is {record['status'].value}; wait for it to finish", {"status": record["status"].value})
        if record["status"] == RequestStatus.DESTROYED:
            raise InfrastructureError(ErrorCode.INVALID_STATE, f"Request {request_id} is already destroyed", {"status": "DESTROYED"})
        self._refuse_if_busy(record)
        for network in (item for item in self.state.resources_of(request_id) if item.kind == ResourceKind.network and item.status != ResourceStatus.DELETED):
            tenants = [item for item in self.state.resources_in_network(network.id) if item.request_id != request_id]
            if tenants:
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"Network {network.id} of this request still carries {len(tenants)} machines or databases of other requests; "
                                          "destroy or delete those first", {"network": network.id, "tenants": [item.id for item in tenants]})
        provider = self.registry.get(record["provider"])
        previous = record["status"]
        self.state.set_request_status(request_id, RequestStatus.DESTROYING)
        operation_id = self.state.start_operation(request_id, "destroy", identity=principal.name)
        self._submit(request_id, operation_id, lambda: self.state.set_request_status(request_id, previous, error=record["error"]),
                     self._run_destroy, request_id, self._context(record), provider, operation_id)

    def _run_destroy(self, request_id, ctx, provider, operation_id):
        with bound(request_id=request_id, operation_id=operation_id, identity=ctx.owner):
            resources = [item for item in self.state.resources_of(request_id) if item.provider_id and item.status != ResourceStatus.DELETED]
            event("destruction_started", provider=provider.name, resources=[item.id for item in resources])
            remaining = []
            for resource in sorted(resources, key=lambda item: DESTROY_ORDER.index(item.kind)):
                resource = self._release_grants(provider, resource, ctx)
                self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.DELETING}))
                try:
                    self._delete(provider, resource, ctx)
                    self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.DELETED, "error": None}))
                    event("resource_deleted", kind=resource.kind.value, resource_id=resource.id, name=resource.name)
                except Exception as exc:
                    cause = self._cause(exc)
                    self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.FAILED, "error": cause.detail()}))
                    remaining.append({"resource_id": resource.id, "name": resource.name, "error": cause.detail().model_dump(mode="json")})
            if remaining:
                error = InfrastructureError(ErrorCode.DESTROY_FAILED, f"{len(remaining)} of {len(resources)} resources could not be deleted; call destroy_infrastructure again once the cause is fixed",
                                            {"remaining": remaining})
                self.state.transition(request_id, (RequestStatus.DESTROYING,), RequestStatus.DESTROY_FAILED, error=error.detail().model_dump(mode="json"))
                self.state.finish_operation(operation_id, OperationStatus.FAILED, error=error.detail().model_dump(mode="json"), detail=f"{len(remaining)} remaining")
                event("destruction_failed", remaining=[item["resource_id"] for item in remaining])
            else:
                self._finish_destroyed(request_id, (RequestStatus.DESTROYING,))
                self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED, detail=f"{len(resources)} resources deleted")
                event("destruction_completed", resources=[item.id for item in resources])

    def _finish_destroyed(self, request_id: str, expected) -> bool:
        """A request is DESTROYED once nothing of it is left, and its members' access went with it: the one ending for
        destroy and for the last single delete, from an expected status only, so that it never undoes a later change."""
        with self.lock(request_id):
            if not self.state.transition(request_id, expected, RequestStatus.DESTROYED):
                return False
            entries = self.state.get_request(request_id)["access"]
            if entries:
                stamp = lifecycle.iso(self.wallclock())
                self.state.set_access(request_id, [{**entry, "status": "REVOKED", "resources": [], "error": None, "updated_at": stamp} for entry in entries])
        return True

    def _refuse_if_busy(self, record: dict) -> None:
        """A change waits until nothing else is changing the request: no worker on it, and no operation still running on it
        (a resize, a delete, an idle stop)."""
        request_id = record["request_id"]
        if record["status"] in MOVING:
            raise InfrastructureError(ErrorCode.INVALID_STATE, f"Request {request_id} is {record['status'].value}; ask again once it has finished",
                                      {"status": record["status"].value, "reason": "changing"})
        running = self.state.running_operations_of(request_id)
        if running:
            raise InfrastructureError(ErrorCode.INVALID_STATE, f"Request {request_id} has operations running ({', '.join(sorted({item.kind for item in running}))}); "
                                      "ask again once they have finished", {"reason": "changing", "operations": [item.operation_id for item in running]})

    # ---- access: grants on what the request created, never on the project
    def _grant_targets(self, request_id: str, provider: CloudProvider) -> list:
        """The resources of a request that have an access policy and may still exist, whatever state a failed refresh or a
        change in flight left them in. A removal reaches every one; a grant those that are settled (see _apply_grants)."""
        return [item for item in self.state.resources_of(request_id) if provider.takes_grants(item.kind.value, getattr(item, "type", None)) and item.provider_id
                and item.status not in (ResourceStatus.DELETED, ResourceStatus.PENDING)]

    def _add_member(self, request_id: str, member: str, level: str) -> None:
        """An identity this service made joins the request's access list, so it is granted on the request's resources as any
        member is; it is not checked against the operator's domains, being the request's own."""
        with self.lock(request_id):
            entries = [entry for entry in self.state.get_request(request_id)["access"] if entry["member"] != member]
            entries.append({"member": member, "level": level, "status": "PENDING", "resources": [], "error": None, "updated_at": None})
            self.state.set_access(request_id, entries)

    def _apply_grants(self, request_id: str, provider: CloudProvider, ctx: ProvisionContext, only: str | None = None, held=()) -> None:
        """Grant each pending member (or the one named) on every settled resource of the request that takes grants, and on
        those in `held`, where the member had access before a change of level. A resource being created or deleted is
        named in the member's error, to be granted again (one being deleted is passed over); a failure is recorded on the
        member with its error and never fails the request; manage_infrastructure_access grants again."""
        with self.lock(request_id):
            entries = self.state.get_request(request_id)["access"]
            if not entries:
                return
            reachable = self._grant_targets(request_id, provider)
            unsettled = (ResourceStatus.CREATING, ResourceStatus.DELETING, ResourceStatus.FAILED)
            targets = [item for item in reachable if item.status not in unsettled or (item.id in held and item.status != ResourceStatus.DELETING)]
            waiting = [item.id for item in reachable if item.status == ResourceStatus.CREATING and item.id not in held]
            for entry in entries:
                wanted = entry["member"] == only if only is not None else entry["status"] == "PENDING"
                if not wanted:
                    continue
                granted, errors = [], []
                for target in targets:
                    try:
                        roles = provider.grant(target, entry["member"], entry["level"], ctx)
                    except Exception as exc:
                        cause = self._cause(exc)
                        errors.append(cause.detail().model_dump(mode="json"))
                        event("access_grant_failed", member=entry["member"], resource_id=target.id, error=cause.code.value)
                        continue
                    if not roles:  # a resource of a grantable kind with no policy of its own (a disk)
                        continue
                    fresh = self.state.get_resource(target.id)
                    kept = [grant for grant in fresh.access if grant.member != entry["member"]]
                    self.state.save_resource(fresh.model_copy(update={"access": [*kept, *(Grant(member=entry["member"], role=role) for role in roles)]}))
                    granted.append(target.id)
                    event("access_granted", member=entry["member"], level=entry["level"], resource_id=target.id, roles=roles)
                status, error = access_rules.outcome(granted, errors, waiting)
                entry.update(status=status, resources=granted, error=error, updated_at=lifecycle.iso(self.wallclock()))
            self.state.set_access(request_id, entries)

    def _revoke(self, provider: CloudProvider, resource, member: str, ctx: ProvisionContext) -> dict | None:
        """Remove a member from one resource's policy; the error as a dict when the provider refuses, else None."""
        try:
            provider.revoke(resource, member, ctx)
        except Exception as exc:
            cause = self._cause(exc)
            event("access_revoke_failed", member=member, resource_id=resource.id, error=cause.code.value)
            return cause.detail().model_dump(mode="json")
        with self.lock(resource.request_id):
            fresh = self.state.get_resource(resource.id) or resource
            self.state.save_resource(fresh.model_copy(update={"access": [grant for grant in fresh.access if grant.member != member]}))
        event("access_revoked", member=member, resource_id=resource.id)
        return None

    def _release_grants(self, provider: CloudProvider, resource, ctx: ProvisionContext):
        """Before a machine is deleted, take its members out of the tunnel policy, which outlives the machine. Best effort:
        that binding names the machine's numeric id, which no later machine reuses. Returns the resource as stored now."""
        if resource.kind != ResourceKind.compute or not resource.access:
            return resource
        for member in dict.fromkeys(grant.member for grant in resource.access):
            self._revoke(provider, resource, member, ctx)
        return self.state.get_resource(resource.id) or resource

    def manage_access(self, request_id: str, action: str, member: str, principal, level: str = "use") -> InfrastructureResponse:
        """Grant a member access to what a request created, or revoke it. Grants go on each resource's own policy."""
        self.require(principal, "provision")
        if action not in ACCESS_ACTIONS:
            raise InfrastructureError(ErrorCode.INVALID_REQUEST, f"action must be one of {', '.join(ACCESS_ACTIONS)}", {"field": "action"})
        if level not in ("use", "admin"):
            raise InfrastructureError(ErrorCode.INVALID_REQUEST, "level must be use or admin", {"field": "level"})
        member = validation.check_member(member, "member")
        self._record(request_id, principal)
        with self.lock(request_id):
            self._change_access(self.state.get_request(request_id), action, member, principal, level)
        return self.status(request_id, principal, refresh=False)

    def _change_access(self, record: dict, action: str, member: str, principal, level: str) -> None:
        request_id = record["request_id"]
        settled = record["status"] in (RequestStatus.READY, RequestStatus.STOPPED)
        # a revoke also reaches what a failed request or a failed destroy left behind; a grant only a request in use
        if (action == "grant" and not settled) or record["status"] in (*MOVING, RequestStatus.DESTROYED):
            raise InfrastructureError(ErrorCode.INVALID_STATE, f"Request {request_id} is {record['status'].value}; access is granted on a READY or STOPPED request, "
                                      "and revoked on any that is not changing or destroyed", {"status": record["status"].value})
        provider, ctx, entries = self.registry.get(record["provider"]), self._context(record), record["access"]
        entry = next((item for item in entries if item["member"] == member), None)
        if action == "grant":
            if not access_rules.allowed(member, self.config):
                raise InfrastructureError(ErrorCode.INVALID_ACCESS, access_rules.refusal(member, self.config), {"field": "member"})
            others = [item for item in entries if item["status"] != "REVOKED" and item["member"] != member]
            if len(others) + 1 > self.config.access.max_members:
                raise InfrastructureError(ErrorCode.INVALID_ACCESS, f"At most {self.config.access.max_members} members per request", {"field": "member"})
        elif entry is None or entry["status"] == "REVOKED":
            raise InfrastructureError(ErrorCode.NOT_FOUND, f"{member} has no access on request {request_id}", {"member": member})
        operation_id = self.state.start_operation(request_id, f"{action}_access", identity=principal.name)
        with bound(request_id=request_id, operation_id=operation_id, identity=principal.name):
            # Every resource that may still exist, not only those with a recorded grant: a grant that failed halfway may have
            # reached one of a resource's policies (a machine's, not yet its tunnel's), and a removal must find it.
            reachable = self._grant_targets(request_id, provider)
            held = [item.id for item in reachable if any(grant.member == member for grant in item.access)]
            if action == "grant" and entry is not None and entry["level"] != level:
                # a changed level: take the old roles away first, so admin roles never outlive a change to use
                errors = [error for error in (self._revoke(provider, item, member, ctx) for item in reachable) if error]
                if errors:
                    self.state.finish_operation(operation_id, OperationStatus.FAILED, error=errors[0], detail=f"{member}: the old level could not be removed")
                    raise InfrastructureError(ErrorCode(errors[0]["code"]), f"Changing {member} to {level} failed: {errors[0]['message']}", {"member": member})
            if action == "grant":
                if entry is None:
                    entry = {"member": member, "level": level, "status": "PENDING", "resources": [], "error": None, "updated_at": None}
                    entries.append(entry)
                entry.update(level=level, status="PENDING")
                self.state.set_access(request_id, entries)
                self._apply_grants(request_id, provider, ctx, only=member, held=held)
                result = next(item for item in self.state.get_request(request_id)["access"] if item["member"] == member)
            else:
                errors = [error for error in (self._revoke(provider, item, member, ctx) for item in reachable) if error]
                still = [item.id for item in self._grant_targets(request_id, provider) if any(grant.member == member for grant in item.access)]
                entry.update(status="REVOKED" if not errors else entry["status"], resources=still, error=errors[0] if errors else None,
                             updated_at=lifecycle.iso(self.wallclock()))
                self.state.set_access(request_id, entries)
                result = entry
            done = result["status"] == "REVOKED" if action == "revoke" else result["status"] in ("GRANTED", "RECORDED")
            self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED if done else OperationStatus.FAILED, error=None if done else result["error"],
                                        detail=f"{member}: {result['status']}")

    # ---- stop, start, extend, keep awake
    def control(self, request_id: str, action: str, principal, hours: int | None = None, wait_seconds: int = 0) -> InfrastructureResponse:
        if action not in CONTROL_ACTIONS:
            raise InfrastructureError(ErrorCode.INVALID_REQUEST, f"action must be one of {', '.join(CONTROL_ACTIONS)}", {"field": "action"})
        if action in ("extend", "keep_awake") and hours is None:
            raise InfrastructureError(ErrorCode.INVALID_REQUEST, f"{action} needs hours", {"field": "hours"})
        if action in ("stop", "start") and hours is not None:
            raise InfrastructureError(ErrorCode.INVALID_REQUEST, f"hours is for extend and keep_awake, not {action}", {"field": "hours"})
        if action == "stop":
            return self.stop(request_id, principal, wait_seconds)
        if action == "start":
            return self.start(request_id, principal, wait_seconds)
        if action == "extend":
            return self.extend(request_id, principal, hours)
        return self.keep_awake(request_id, principal, hours)

    def _stoppable(self, record: dict, provider: CloudProvider) -> list:
        """The request's resources that can stop and start, as the provider sees them now."""
        return [self._refresh(record, item) for item in self.state.resources_of(record["request_id"])
                if item.kind.value in provider.stoppable_kinds and item.provider_id and item.status not in (ResourceStatus.DELETED, ResourceStatus.PENDING)]

    @staticmethod
    def _settled(request_id: str, found: list) -> None:
        changing = [item for item in found if item.status in (ResourceStatus.CREATING, ResourceStatus.UPDATING, ResourceStatus.DELETING)]
        if changing:
            names = ", ".join(f"{item.name} ({item.status.value})" for item in changing)
            raise InfrastructureError(ErrorCode.INVALID_STATE, f"{names} of request {request_id} is changing; ask again once it has settled",
                                      {"reason": "changing", "resources": [item.id for item in changing]})

    def stop(self, request_id: str, principal, wait_seconds: int = 0, reason: str = "requested", expired_at: str | None = None) -> InfrastructureResponse:
        """Stop every machine and database of a READY request. Disks, addresses and what cannot stop keep billing. With
        expired_at (the sweeper's), the data is kept for the retention period from the moment the stop completes, unless
        the request's expiry changed meanwhile."""
        self.require(principal, "provision")
        self._record(request_id, principal)
        with self.lock(request_id):
            record = self.state.get_request(request_id)
            if record["status"] != RequestStatus.READY:
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"Request {request_id} is {record['status'].value}; a READY request stops",
                                          {"status": record["status"].value, "reason": "state"})
            self._refuse_if_busy(record)
            provider = self.registry.get(record["provider"])
            found = self._stoppable(record, provider)
            if not found:
                kinds = " and ".join(provider.stoppable_kinds) or "nothing"
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"Nothing in request {request_id} can stop; the {provider.name} adapter stops {kinds}. Destroy it instead",
                                          {"stoppable_kinds": list(provider.stoppable_kinds), "reason": "nothing_to_stop"})
            self._settled(request_id, found)
            targets = [item for item in found if item.status != ResourceStatus.STOPPED]  # READY, and UNKNOWN or FAILED ones that may still run
            self.state.set_request_status(request_id, RequestStatus.STOPPING)
            operation_id = self.state.start_operation(request_id, "stop", identity=principal.name)
            self._submit(request_id, operation_id, lambda: self.state.set_request_status(request_id, RequestStatus.READY),
                         self._run_power, record, targets, provider, operation_id, "stop", reason, expired_at)
        if wait_seconds:
            self._wait(request_id, wait_seconds)
        return self.status(request_id, principal, refresh=False)

    def start(self, request_id: str, principal, wait_seconds: int = 0) -> InfrastructureResponse:
        """Start what is stopped in a READY or STOPPED request that has not expired."""
        self.require(principal, "provision")
        self._record(request_id, principal)
        with self.lock(request_id):
            record = self.state.get_request(request_id)
            if record["status"] not in (RequestStatus.READY, RequestStatus.STOPPED):
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"Request {request_id} is {record['status'].value}; a READY or STOPPED request starts",
                                          {"status": record["status"].value})
            expires_at = (record["lifecycle"] or {}).get("expires_at")
            if expires_at and lifecycle.parse(expires_at) <= self.wallclock():
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"Request {request_id} expired at {expires_at}; extend it first (control_infrastructure extend)",
                                          {"expires_at": expires_at})
            self._refuse_if_busy(record)
            provider = self.registry.get(record["provider"])
            found = self._stoppable(record, provider)
            self._settled(request_id, found)
            targets = [item for item in found if item.status == ResourceStatus.STOPPED]
            if not targets and record["status"] == RequestStatus.READY:
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"Nothing in request {request_id} is stopped", {"status": record["status"].value})
            previous = record["status"]
            self.state.set_request_status(request_id, RequestStatus.STARTING)
            operation_id = self.state.start_operation(request_id, "start", identity=principal.name)
            self._submit(request_id, operation_id, lambda: self.state.set_request_status(request_id, previous),
                         self._run_power, record, targets, provider, operation_id, "start", None, None)
        if wait_seconds:
            self._wait(request_id, wait_seconds)
        return self.status(request_id, principal, refresh=False)

    def _run_power(self, record: dict, targets: list, provider: CloudProvider, operation_id: str, action: str, reason: str | None, expired_at: str | None) -> None:
        request_id, ctx = record["request_id"], self._context(record)
        # machines stop before the database they use, and start after it
        order = RESOURCE_KINDS if action == "stop" else tuple(reversed(RESOURCE_KINDS))
        with bound(request_id=request_id, operation_id=operation_id, identity=ctx.owner):
            event(f"{action}_started", resources=[item.id for item in targets], reason=reason)
            done, failed = [], []
            for resource in sorted(targets, key=lambda item: order.index(item.kind.value)):
                try:
                    fresh = provider.stop(resource, ctx) if action == "stop" else provider.start(resource, ctx)
                except Exception as exc:
                    cause = self._cause(exc)
                    if cause.code == ErrorCode.NOT_FOUND:  # gone at the provider: nothing left to stop, nothing to start
                        self._save_observed(resource.model_copy(update={"status": ResourceStatus.DELETED, "error": None if action == "stop" else cause.detail()}))
                        if action == "stop":
                            done.append(resource.id)
                            continue
                    else:
                        self._save_observed(resource.model_copy(update={"error": cause.detail()}))
                    failed.append({"resource_id": resource.id, "name": resource.name, "error": cause.detail().model_dump(mode="json")})
                    continue
                update = {"error": None, **({"stop_reason": reason} if fresh.kind == ResourceKind.compute else {})}
                self._save_observed(fresh.model_copy(update=update))
                done.append(resource.id)
                event("resource_stopped" if action == "stop" else "resource_started", kind=resource.kind.value, resource_id=resource.id, reason=reason)
            with self.lock(request_id):
                # STOPPED only when nothing that can stop is still running, whatever was asked: a start that fails on an
                # idle-stopped machine leaves a request whose other machines run READY
                running = [item for item in self.state.resources_of(request_id) if item.kind.value in provider.stoppable_kinds and item.provider_id
                           and item.status not in (ResourceStatus.DELETED, ResourceStatus.PENDING, ResourceStatus.STOPPED)]
                status = RequestStatus.READY if running else RequestStatus.STOPPED
                life = dict(self.state.get_request(request_id)["lifecycle"] or {})
                if status == RequestStatus.STOPPED and action == "stop":
                    life.update({"stopped_at": lifecycle.iso(self.wallclock()), "stop_reason": reason})
                    if expired_at is not None and life.get("expires_at") == expired_at:
                        retain = self.wallclock() + datetime.timedelta(days=self.config.limits.expiry_retention_days)
                        life["retain_until"] = lifecycle.iso(retain)
                        for key in ("expiry_attempts", "expiry_attempted_at"):
                            life.pop(key, None)
                elif status == RequestStatus.READY:
                    for key in lifecycle.STOP_KEYS:
                        life.pop(key, None)
                # only from the status this work set: a request deleted or destroyed meanwhile stays as it is now
                if self.state.transition(request_id, (RequestStatus.STOPPING if action == "stop" else RequestStatus.STARTING,), status):
                    self.state.set_lifecycle(request_id, life)
                else:
                    event(f"{action}_superseded", status=self.state.get_request(request_id)["status"].value)
            if failed:
                first = failed[0]["error"]
                error = {"code": first["code"], "message": f"{len(failed)} of {len(targets)} resources did not {action}: {first['message']}", "details": {"failed": failed}}
                self.state.finish_operation(operation_id, OperationStatus.FAILED, error=error, detail=f"{len(done)} done, {len(failed)} failed")
                event(f"{action}_failed", failed=[item["resource_id"] for item in failed])
            else:
                self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED, detail=f"{len(done)} resources")
                event(f"{action}_completed", resources=done, reason=reason)

    def stop_idle(self, record: dict, machines: list, principal, reason: str = "idle") -> list[str]:
        """Stop machines on the service's own initiative (idle ones), each in a worker with an operation of its own, so that
        nobody waits on the provider while the request's lock is held. Returns the operation ids."""
        provider = self.registry.get(record["provider"])
        started = []
        for machine in machines:
            operation_id = self.state.start_operation(record["request_id"], f"stop_{machine.kind.value}", resource_id=machine.id, identity=principal.name)
            self._submit(record["request_id"], operation_id, lambda: None, self._run_stop_one, record, machine, provider, operation_id, reason, principal)
            started.append(operation_id)
        return started

    def _run_stop_one(self, record: dict, resource, provider: CloudProvider, operation_id: str, reason: str, principal) -> None:
        request_id = record["request_id"]
        with bound(request_id=request_id, operation_id=operation_id, identity=principal.name):
            try:
                stopped = provider.stop(resource, self._context(record))
            except Exception as exc:  # recorded; the sweeper tries again later, after a wait that grows with each failure
                cause = self._cause(exc)
                self.state.finish_operation(operation_id, OperationStatus.FAILED, error=cause.detail().model_dump(mode="json"))
                event("resource_stop_failed", kind=resource.kind.value, resource_id=resource.id, reason=reason, error=cause.code.value)
                return
            if stopped.kind == ResourceKind.compute:
                stopped = stopped.model_copy(update={"stop_reason": reason})
            self._save_observed(stopped)
            with self.lock(request_id):
                life = dict(self.state.get_request(request_id)["lifecycle"] or {})
                if resource.id in (life.get("idle_attempts") or {}):
                    life["idle_attempts"] = {key: value for key, value in life["idle_attempts"].items() if key != resource.id}
                    self.state.set_lifecycle(request_id, life)
            self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED, detail=reason)
            event("resource_stopped", kind=resource.kind.value, resource_id=resource.id, reason=reason)

    def extend(self, request_id: str, principal, hours: int) -> InfrastructureResponse:
        """Move a request's expiry later, up to the operator's maximum lifetime; a request stopped at expiry is kept again."""
        self.require(principal, "provision")
        if not 1 <= hours <= MAX_HOURS:
            raise InfrastructureError(ErrorCode.INVALID_REQUEST, f"hours must be 1 to {MAX_HOURS}", {"field": "hours"})
        self._record(request_id, principal)
        with self.lock(request_id):
            record = self.state.get_request(request_id)
            if record["status"] not in (RequestStatus.READY, RequestStatus.STOPPED):
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"Request {request_id} is {record['status'].value}; extend a READY or STOPPED request",
                                          {"status": record["status"].value})
            life = dict(record["lifecycle"] or {})
            expires = lifecycle.parse(life.get("expires_at"))
            if expires is None:
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"Request {request_id} has no expiry to extend", {"request_id": request_id})
            start = max(expires, self.wallclock())
            until = start + datetime.timedelta(hours=hours)
            cap = lifecycle.parse(life.get("max_expires_at"))
            if cap is not None and until > cap:
                left = max(0, int((cap - start).total_seconds() // 3600))
                raise InfrastructureError(ErrorCode.LIMIT_EXCEEDED, f"The operator's maximum lifetime ends at {life['max_expires_at']}; at most {left} more hours",
                                          {"max_expires_at": life["max_expires_at"], "hours_left": left})
            life.update(expires_at=lifecycle.iso(until), warnings_sent=[], retain_until=None)
            for key in ("expiry_attempts", "expiry_attempted_at"):  # a new expiry starts with a clean slate
                life.pop(key, None)
            self.state.set_lifecycle(request_id, life)
            operation_id = self.state.start_operation(request_id, "extend", identity=principal.name)
            self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED, detail=f"expires at {life['expires_at']}")
        with bound(request_id=request_id, operation_id=operation_id, identity=principal.name):
            event("lifetime_extended", hours=hours, expires_at=life["expires_at"])
        return self.status(request_id, principal, refresh=False)

    def keep_awake(self, request_id: str, principal, hours: int) -> InfrastructureResponse:
        """Suspend idle stop for a while, for work that runs without using the CPU."""
        self.require(principal, "provision")
        limit = self.config.limits.max_keep_awake_hours
        if hours < 1:
            raise InfrastructureError(ErrorCode.INVALID_REQUEST, "hours must be at least 1", {"field": "hours"})
        if hours > limit:
            raise InfrastructureError(ErrorCode.LIMIT_EXCEEDED, f"keep_awake takes at most {limit} hours", {"field": "hours", "limit": limit})
        self._record(request_id, principal)
        with self.lock(request_id):
            record = self.state.get_request(request_id)
            if record["status"] not in (RequestStatus.READY, RequestStatus.STOPPED, RequestStatus.STOPPING, RequestStatus.STARTING):
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"Request {request_id} is {record['status'].value}", {"status": record["status"].value})
            life = dict(record["lifecycle"] or {})
            life["awake_until"] = lifecycle.iso(self.wallclock() + datetime.timedelta(hours=hours))
            self.state.set_lifecycle(request_id, life)
            operation_id = self.state.start_operation(request_id, "keep_awake", identity=principal.name)
            self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED, detail=f"awake until {life['awake_until']}")
        with bound(request_id=request_id, operation_id=operation_id, identity=principal.name):
            event("idle_stop_suspended", hours=hours, awake_until=life["awake_until"])
        return self.status(request_id, principal, refresh=False)

    # ---- single-resource primitives
    def create_compute(self, principal, *, region, cpu, memory_gb, provider="gcp", count=1, os=None, profile=None, boot_disk_gb=None, public_ip=False, network_id=None,
                       accelerator=None, accelerator_count=1, autostop_idle_minutes=None, max_run_hours=None, lifetime_hours=None, zone=None, name=None, labels=None,
                       wait_seconds=0) -> InfrastructureResponse:
        gpus = AcceleratorRequest(type=accelerator, count=accelerator_count) if accelerator else None
        optional = {"boot_disk_gb": boot_disk_gb} if boot_disk_gb is not None else {}  # left out, a machine with GPUs gets the GPU default
        compute = ComputeRequest(count=count, cpu=cpu, memory_gb=memory_gb, os=os, profile=profile, public_ip=public_ip, network_id=network_id, accelerator=gpus,
                                 autostop=AutostopRequest(idle_minutes=autostop_idle_minutes) if autostop_idle_minutes is not None else None,
                                 max_run_hours=max_run_hours, **optional)
        lifetime = LifetimeRequest(hours=lifetime_hours) if lifetime_hours is not None else None  # 0 is refused, not taken as "none"
        return self.provision(InfrastructureRequest(provider=provider, region=region, zone=zone, name=name, labels=labels or {}, compute=compute, lifetime=lifetime),
                              principal, wait_seconds)

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
                       network_id=None, registries=None, public=False, hostnames=None, zone=None, name=None, labels=None, wait_seconds=0):
        door = PublicRequest(hostnames=hostnames or []) if public or hostnames else None
        cluster = ClusterRequest(node_count=node_count, node_cpu=node_cpu, node_memory_gb=node_memory_gb, node_disk_gb=node_disk_gb, kubernetes_version=kubernetes_version,
                                 private_nodes=private_nodes, network_id=network_id, registries=registries or [], public=door)
        return self.provision(InfrastructureRequest(provider=provider, region=region, zone=zone, name=name, labels=labels or {}, cluster=cluster), principal, wait_seconds)

    def create_service(self, principal, *, region, provider="gcp", cpu=1, memory_mb=512, min_instances=0, max_instances=10, concurrency=80, timeout_seconds=300,
                       ingress="all", public=False, network_id=None, egress="private_ranges", name=None, labels=None, wait_seconds=0):
        service = ServiceRequest(cpu=cpu, memory_mb=memory_mb, min_instances=min_instances, max_instances=max_instances, concurrency=concurrency,
                                 timeout_seconds=timeout_seconds, ingress=ingress, public=public, network_id=network_id, egress=egress)
        return self.provision(InfrastructureRequest(provider=provider, region=region, name=name, labels=labels or {}, service=service), principal, wait_seconds)

    def create_warehouse(self, principal, *, region, table_expiration_days=None, provider="gcp", name=None, labels=None, wait_seconds=0):
        warehouse = WarehouseRequest(table_expiration_days=table_expiration_days)
        return self.provision(InfrastructureRequest(provider=provider, region=region, name=name, labels=labels or {}, warehouse=warehouse), principal, wait_seconds)

    def create_nosql(self, principal, *, region, provider="gcp", name=None, labels=None, wait_seconds=0):
        return self.provision(InfrastructureRequest(provider=provider, region=region, name=name, labels=labels or {}, nosql=NoSqlRequest()), principal, wait_seconds)

    def create_monitor(self, principal, *, region, notify, uptime, provider="gcp", name=None, labels=None, wait_seconds=0):
        monitor = MonitorRequest(notify=list(notify), uptime=list(uptime))
        return self.provision(InfrastructureRequest(provider=provider, region=region, name=name, labels=labels or {}, monitor=monitor), principal, wait_seconds)

    def create_budget(self, principal, *, request_id, monthly_amount, notify, thresholds=None, forecast=True, name=None, labels=None, wait_seconds=0):
        """A budget on an earlier request's costs, made as a request of its own in that request's region."""
        record = self._record(request_id, principal)
        budget = BudgetRequest(monthly_amount=monthly_amount, notify=list(notify), thresholds=list(thresholds or [50, 90, 100]), forecast=forecast, request_id=request_id)
        return self.provision(InfrastructureRequest(provider=record["provider"], region=record["region"], name=name, labels=labels or {}, budget=budget), principal, wait_seconds)

    def create_dns(self, principal, *, region, zone, records=None, provider="gcp", name=None, labels=None, wait_seconds=0):
        dns = DnsRequest(zone=zone, records=list(records or []))
        return self.provision(InfrastructureRequest(provider=provider, region=region, name=name, labels=labels or {}, dns=dns), principal, wait_seconds)

    def create_identity(self, principal, *, region, level="use", provider="gcp", name=None, labels=None, wait_seconds=0):
        return self.provision(InfrastructureRequest(provider=provider, region=region, name=name, labels=labels or {}, identity=IdentityRequest(level=level)), principal,
                              wait_seconds)

    def create_job(self, principal, *, region, provider="gcp", cpu=1, memory_mb=512, tasks=1, parallelism=0, max_retries=3, task_timeout_seconds=600,
                   network_id=None, egress="private_ranges", schedule=None, time_zone="UTC", name=None, labels=None, wait_seconds=0):
        job = JobRequest(cpu=cpu, memory_mb=memory_mb, tasks=tasks, parallelism=parallelism, max_retries=max_retries, task_timeout_seconds=task_timeout_seconds,
                         network_id=network_id, egress=egress, schedule=ScheduleRequest(cron=schedule, time_zone=time_zone) if schedule else None)
        return self.provision(InfrastructureRequest(provider=provider, region=region, name=name, labels=labels or {}, job=job), principal, wait_seconds)

    def create_load_balancer(self, principal, *, region, backends, provider="gcp", type="http", port=80, health_check_path="/", hostnames=None, name=None, labels=None,
                             wait_seconds=0):
        balancer = LoadBalancerRequest(type=type, port=port, health_check_path=health_check_path, backends=backends, hostnames=hostnames or [])
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
        profiles = {name: [ProfileInfo(**item) for item in provider.profile_infos()] for name, provider in providers.items()}
        return ResourceKindsResponse(kinds=kinds, providers=list(providers), profiles=profiles,
                                     note="A request block of any other name is refused with UNSUPPORTED_RESOURCE_KIND; network_id on compute, database, cache and cluster joins "
                                     "a network created earlier, and load_balancer.backends names machines created earlier. Beside the kinds, a request takes lifetime "
                                     "(hours, on_expiry) and access (members granted on what it creates); compute.profile names one of the profiles listed here.")

    def create_storage(self, principal, *, region, size_gb, provider="gcp", type="persistent", performance="balanced", zone=None, name=None, labels=None, wait_seconds=0,
                       snapshot_keep_days=None):
        storage = StorageRequest(type=type, size_gb=size_gb, performance=performance, snapshots=None if snapshot_keep_days is None else {"keep_days": snapshot_keep_days})
        return self.provision(InfrastructureRequest(provider=provider, region=region, zone=zone, name=name, labels=labels or {}, storage=storage), principal, wait_seconds)

    def create_network(self, principal, *, region, provider="gcp", public_access=False, cidr="10.10.0.0/24", allowed_ports=None, allowed_sources=None, iap_ssh=True, name=None,
                       labels=None, wait_seconds=0, nat=False):
        network = NetworkRequest(public_access=public_access, cidr=cidr, allowed_ports=allowed_ports or [80, 443], allowed_sources=allowed_sources or ["0.0.0.0/0"], iap_ssh=iap_ssh,
                                 nat=nat)
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
        provider = self.registry.get(record["provider"])
        with self.lock(record["request_id"]):
            record, resource = self.state.get_request(record["request_id"]), self.state.get_resource(resource_id)
            if resource.status in (ResourceStatus.DELETED, ResourceStatus.DELETING, ResourceStatus.CREATING, ResourceStatus.PENDING):
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"{resource_id} is {resource.status.value}", {"status": resource.status.value})
            self._refuse_if_busy(record)  # never under a stop, a start or a destroy of its request
            if kind == ResourceKind.network:
                tenants = self.state.resources_in_network(resource_id)
                if tenants:
                    raise InfrastructureError(ErrorCode.INVALID_STATE, f"The network still carries {len(tenants)} machines or databases; delete those first",
                                              {"resource_id": resource_id, "tenants": [item.id for item in tenants]})
            operation_id = self.state.start_operation(record["request_id"], f"delete_{kind.value}", resource_id=resource_id, identity=principal.name)
            self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.DELETING}))
            self._submit(record["request_id"], operation_id, lambda: self.state.save_resource(resource), self._run_delete_one, record, resource, provider, operation_id,
                         self._context(record))
        if wait_seconds:
            self._wait_operation(operation_id, wait_seconds)
        fresh = self.state.get_resource(resource_id)
        return ResourceResponse(status=fresh.status.value, request_id=fresh.request_id, resource=fresh, operation=self.state.get_operation(operation_id))

    def _run_delete_one(self, record, resource, provider, operation_id, ctx):
        request_id = record["request_id"]
        with bound(request_id=request_id, operation_id=operation_id, identity=ctx.owner):
            event("destruction_started", resources=[resource.id], single=True)
            try:
                resource = self._release_grants(provider, resource, ctx)
                self._delete(provider, resource, ctx)
                self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.DELETED, "error": None, "attached_to": None} if resource.kind == ResourceKind.storage
                                                             else {"status": ResourceStatus.DELETED, "error": None}))
                event("resource_deleted", kind=resource.kind.value, resource_id=resource.id)
            except Exception as exc:
                cause = self._cause(exc)
                self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.FAILED, "error": cause.detail()}))
                self.state.finish_operation(operation_id, OperationStatus.FAILED, error=cause.detail().model_dump(mode="json"))
                event("destruction_failed", resource_id=resource.id, error=cause.detail().model_dump(mode="json"))
                return
            gone, detail = [resource.id], "deleted"
            # the secret holding a database's password or a cache's AUTH string belongs to it, and goes with it
            credential = getattr(resource, "password_secret", None) or getattr(resource, "auth_secret", None)
            secret = self.state.get_resource(credential) if credential else None
            if secret is not None and secret.provider_id and secret.status != ResourceStatus.DELETED:
                try:
                    self._delete(provider, secret, ctx)
                    self.state.save_resource(secret.model_copy(update={"status": ResourceStatus.DELETED, "error": None}))
                    gone.append(secret.id)
                    detail = f"deleted with its secret {secret.id}"
                    event("resource_deleted", kind="secret", resource_id=secret.id, reason=f"the credential of {resource.id}")
                except Exception as exc:
                    cause = self._cause(exc)
                    self.state.save_resource(secret.model_copy(update={"error": cause.detail()}))
                    detail = f"deleted; its secret {secret.id} could not be: {cause.message}"
            self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED, detail=detail)
            with self.lock(request_id):
                entries = self.state.get_request(request_id)["access"]
                if entries:  # members no longer have access to what is gone
                    self.state.set_access(request_id, [{**entry, "resources": [item for item in entry["resources"] if item not in gone]} for entry in entries])
            if all(item.status == ResourceStatus.DELETED for item in self.state.resources_of(request_id)):
                if self._finish_destroyed(request_id, (RequestStatus.READY, RequestStatus.STOPPED, RequestStatus.FAILED, RequestStatus.DESTROY_FAILED)):
                    event("destruction_completed", resources=gone, reason="last resource of the request")

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
        problem = provider.resize_problem(resource, cpu, memory_gb)  # refused now, before the machine is touched
        if problem is not None:
            raise InfrastructureError(problem.code, problem.message, {"field": problem.field, "cpu": cpu, "memory_gb": memory_gb})
        with self.lock(record["request_id"]):
            record, resource = self.state.get_request(record["request_id"]), self.state.get_resource(resource_id)
            self._refuse_if_busy(record)  # never under a stop, a start or a destroy of its request, nor another change
            if resource.status not in (ResourceStatus.READY, ResourceStatus.STOPPED):
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"{resource_id} is {resource.status.value}; a machine is resized when READY or STOPPED",
                                          {"status": resource.status.value})
            operation_id = self.state.start_operation(record["request_id"], "resize_compute", resource_id=resource_id, identity=principal.name)
            self.state.save_resource(resource.model_copy(update={"status": ResourceStatus.UPDATING}))

            def run():
                with bound(request_id=record["request_id"], operation_id=operation_id, identity=principal.name):
                    try:
                        resized = provider.resize_compute(resource, cpu, memory_gb, self._context(record))
                        self._save_observed(resized)  # grants made while it ran are kept
                        self.state.finish_operation(operation_id, OperationStatus.SUCCEEDED, detail=f"{resized.machine_type}")
                        event("resource_resized", resource_id=resource_id, cpu=cpu, memory_gb=memory_gb)
                    except Exception as exc:
                        cause = self._cause(exc)
                        self._save_observed(self._refresh(record, resource).model_copy(update={"error": cause.detail()}))
                        self.state.finish_operation(operation_id, OperationStatus.FAILED, error=cause.detail().model_dump(mode="json"))
            self._submit(record["request_id"], operation_id, lambda: self.state.save_resource(resource), run)
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
            attached = self._save_observed(attached)
            with self.lock(compute.request_id):  # the machine as stored now: grants or disks recorded meanwhile are kept
                machine = self.state.get_resource(compute_id)
                self.state.save_resource(machine.model_copy(update={"attached_disks": [*machine.attached_disks, storage_id]}))
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
        # What exists now, as it is now: a resize, a deleted resource or a changed profile is priced as it is, and a request
        # stored by an earlier release is never validated again against a newer contract.
        found = [resource] if resource is not None else self.state.resources_of(record["request_id"])
        existing = [item for item in found if item.status != ResourceStatus.DELETED and not (item.status == ResourceStatus.FAILED and item.provider_id is None)]
        estimate = provider.estimate_resources(existing)
        stopped = {item.name for item in existing if item.status == ResourceStatus.STOPPED}
        current = round(sum(line.stopped_usd if line.resource in stopped and line.stopped_usd is not None else line.monthly_usd for line in estimate.lines), 2)
        return {"request_id": record["request_id"], "resource_id": resource_id, "status": record["status"].value, "estimate": estimate.model_dump(mode="json"),
                "current_monthly_usd": current, "actual": None,
                "note": "Actual spend needs a billing export; this service estimates what exists now from list prices only. current_monthly_usd counts each stopped "
                        "machine and database at its stopped rate."}

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
                "cleanup_on_failure": self.config.cleanup_on_failure, "workers": self.config.workers, "labels": self.config.labels, "access": self.config.access.model_dump(),
                "sweep_interval_seconds": self.config.sweep_interval_seconds, "return_database_password": self.config.return_database_password,
                "profiles": {name: self.registry.get(name).profile_infos() for name in self.registry.names()}}

    # ---- waiting
    def _wait(self, request_id: str, seconds: int) -> None:
        deadline = self.clock() + max(0, min(seconds, MAX_WAIT_SECONDS))
        while True:
            record = self.state.get_request(request_id)
            if record is None or record["status"] not in MOVING or self.clock() >= deadline:
                return
            self.sleep(POLL_SECONDS)

    def _wait_operation(self, operation_id: str, seconds: int) -> None:
        deadline = self.clock() + max(0, min(seconds, MAX_WAIT_SECONDS))
        while True:
            found = self.state.get_operation(operation_id)
            if found is None or found.status != OperationStatus.RUNNING or self.clock() >= deadline:
                return
            self.sleep(POLL_SECONDS)
