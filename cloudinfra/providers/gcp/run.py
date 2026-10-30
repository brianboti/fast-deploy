"""Cloud Run services and jobs behind the generic service and job primitives (docs/DELIVERY-SPEC.md, phase 4): the shell only. A service
is made with Google's sample image standing in, its limits, scaling, ingress and egress, and an account made for it that
may read the request's secrets and nothing else; the delivery layer puts releases on it as revisions. Public means the
invoker check is off (`invokerIamDisabled`), Google's recommended way, and only when the operator allows it.

Create and delete are long-running; the service itself says when it is done (`reconciling`) and how it went
(`terminalCondition`), so it is polled rather than its operation."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import JobResource, ResourceStatus, ServiceResource
from . import translate
from .api import RUN, SCHEDULER
from .iam import GcpIam, update_policy, with_member, without_member
from .nodeaccount import NOT_YET, NodeAccounts

STAND_IN = "us-docker.pkg.dev/cloudrun/container/hello"
JOB_STAND_IN = "us-docker.pkg.dev/cloudrun/container/job"
INVOKER = "roles/run.invoker"
INGRESS = {"all": "INGRESS_TRAFFIC_ALL", "internal": "INGRESS_TRAFFIC_INTERNAL_ONLY", "internal_and_load_balancer": "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"}
EGRESS = {"private_ranges": "PRIVATE_RANGES_ONLY", "all": "ALL_TRAFFIC"}
SECRET_READER = "roles/secretmanager.secretAccessor"  # noqa: S105 - a role's name, not a credential
SETTLING = (900, 5)  # how long a create or delete may take, and how often the service is asked


def runtime_account(request_id: str, name: str, prefix: str = "run") -> str:
    """<prefix>-<10 characters of the request id>-<the service's or job's name>: 27 characters at most, as IAM allows 30.
    run- is a service's or job's runtime account, sch- the account a job's schedule runs it as."""
    return f"{prefix}-{request_id[:10]}-{name}"


class GcpRun:
    def __init__(self, api, settings):
        self.api, self.settings, self.accounts, self.iam = api, settings, NodeAccounts(api), GcpIam(api)

    def _services(self, region: str) -> str:
        return f"{RUN}/projects/{self.api.project}/locations/{region}/services"

    def _settle(self, url: str, gone: bool = False) -> dict | None:
        """The service once Google is done with it (or, gone, once it no longer exists)."""
        started = self.api.clock()
        while self.api.clock() - started < SETTLING[0]:
            try:
                found = self.api.get(url)
            except InfrastructureError as exc:
                if gone and exc.code == ErrorCode.NOT_FOUND:
                    return None
                raise
            if not found.get("reconciling") and not gone:
                return found
            self.api.sleep(SETTLING[1])
        raise InfrastructureError(ErrorCode.PROVIDER_UNAVAILABLE, f"Cloud Run did not finish with {url.rsplit('/', 1)[-1]} in {SETTLING[0]} seconds", {"url": url})

    def _read_secrets(self, email: str, secrets, give: bool) -> None:
        member = f"serviceAccount:{email}"
        for secret in secrets:
            endpoint = self.iam._secret(secret)
            for attempt in range(NOT_YET[0]):
                try:
                    if give:
                        update_policy(endpoint, lambda policy: with_member(policy, SECRET_READER, member))
                    else:
                        update_policy(endpoint, lambda policy: without_member(policy, member, roles={SECRET_READER}))
                    break
                except InfrastructureError as exc:
                    if not give and exc.code == ErrorCode.NOT_FOUND:
                        break
                    if "does not exist" not in exc.message or attempt == NOT_YET[0] - 1:
                        raise
                    self.api.sleep(NOT_YET[1])

    def _forget(self, email: str, secrets) -> None:
        try:
            self._read_secrets(email, secrets, give=False)
        except InfrastructureError:
            pass
        self.accounts.forget(email, [])

    def create(self, spec, planned, ctx, network, secrets) -> ServiceResource:
        region = ctx.region
        email = self.accounts.create(runtime_account(ctx.request_id, planned.spec["service"]), f"Runtime of Cloud Run service {planned.name}. {translate.description(ctx)}")
        try:
            self._read_secrets(email, secrets, give=True)
        except InfrastructureError:
            self._forget(email, secrets)
            raise
        template = {"serviceAccount": email, "maxInstanceRequestConcurrency": spec.concurrency, "timeout": f"{spec.timeout_seconds}s",
                    "scaling": {"minInstanceCount": spec.min_instances, "maxInstanceCount": spec.max_instances},
                    "containers": [{"image": STAND_IN, "resources": {"limits": {"cpu": str(spec.cpu), "memory": f"{spec.memory_mb}Mi"}}}]}
        if network is not None:  # Direct VPC egress: Cloud Run takes the network and subnetwork by name
            template["vpcAccess"] = {"networkInterfaces": [{"network": network.provider_id.rsplit("/", 1)[-1], "subnetwork": (network.subnet or "").rsplit("/", 1)[-1] or None}],
                                     "egress": EGRESS[spec.egress]}
        body = {"description": translate.description(ctx), "labels": translate.labels(ctx), "ingress": INGRESS[spec.ingress], "invokerIamDisabled": spec.public,
                "template": template}
        url = f"{self._services(region)}/{planned.name}"
        resource = ServiceResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=region, provider_id=url.split("/v2/", 1)[1],
                                   status=ResourceStatus.CREATING, runtime_service_account=email, cpu=spec.cpu, memory_mb=spec.memory_mb, min_instances=spec.min_instances,
                                   max_instances=spec.max_instances, ingress=spec.ingress, public=spec.public, network=network.id if network else None,
                                   secrets=[secret.resource_name for secret in secrets], labels=translate.labels(ctx))
        try:
            self.api.post(self._services(region), json=body, serviceId=planned.name)
        except InfrastructureError:
            self._forget(email, secrets)  # Google never took the service, so nothing else will delete its account
            raise
        ctx.accepted(resource)  # known from here: a failed wait still deletes the service and its account
        return self._view(resource, self._settle(url), strict=True)

    def _view(self, resource: ServiceResource, found: dict, strict: bool = False) -> ServiceResource:
        condition = found.get("terminalCondition") or {}
        serving = next((item.get("revision") for item in found.get("trafficStatuses", []) if item.get("percent") == 100), None)
        if condition.get("state") == "CONDITION_FAILED" and (strict or not serving):
            if strict:
                raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Cloud Run could not make {resource.name}: {condition.get('message', 'no reason given')}",
                                          {"reason": condition.get("reason")})
            status = ResourceStatus.FAILED
        else:  # a release whose revision failed leaves the condition failed while the one before it serves on (found on a real project on 2026-10-30)
            status = ResourceStatus.UPDATING if found.get("reconciling") else ResourceStatus.READY
        return resource.model_copy(update={"status": status, "url": found.get("uri"), "revision": serving or found.get("latestReadyRevision", "").rsplit("/", 1)[-1] or None,
                                           "created_at": resource.created_at or found.get("createTime")})

    def get(self, resource: ServiceResource) -> ServiceResource:
        try:
            found = self.api.get(f"{RUN}/{resource.provider_id}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED})
            raise
        return self._view(resource, found)

    def delete(self, resource: ServiceResource, ctx) -> None:
        url = f"{RUN}/{resource.provider_id}"
        try:
            self.api.delete(url)
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
        self._settle(url, gone=True)
        if resource.runtime_service_account:  # after the service, which runs as it
            self._read_secrets(resource.runtime_service_account, [_Secret(name) for name in resource.secrets], give=False)
            self.accounts.remove(resource.runtime_service_account, [])


class GcpRunJob(GcpRun):
    """A Cloud Run job, made empty with Google's sample job image, its runtime account (the request's secrets, nothing else)
    and, with a schedule, a Cloud Scheduler job calling its run endpoint as a second account that may run this job only
    (https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule)."""

    def _jobs(self, region: str) -> str:
        return f"{RUN}/projects/{self.api.project}/locations/{region}/jobs"

    def _scheduler(self, region: str) -> str:
        return f"{SCHEDULER}/projects/{self.api.project}/locations/{region}/jobs"

    def _job_policy(self, provider_id: str):
        return (lambda: self.api.get(f"{RUN}/{provider_id}:getIamPolicy", **{"options.requestedPolicyVersion": 3}),
                lambda policy: self.api.post(f"{RUN}/{provider_id}:setIamPolicy", json={"policy": policy}))

    def create(self, spec, planned, ctx, network, secrets) -> JobResource:
        region, name = ctx.region, planned.spec["job"]
        email = self.accounts.create(runtime_account(ctx.request_id, name), f"Runtime of Cloud Run job {planned.name}. {translate.description(ctx)}")
        try:
            self._read_secrets(email, secrets, give=True)
        except InfrastructureError:
            self._forget(email, secrets)
            raise
        task = {"serviceAccount": email, "maxRetries": spec.max_retries, "timeout": f"{spec.task_timeout_seconds}s",
                "containers": [{"image": JOB_STAND_IN, "resources": {"limits": {"cpu": str(spec.cpu), "memory": f"{spec.memory_mb}Mi"}}}]}
        if network is not None:
            task["vpcAccess"] = {"networkInterfaces": [{"network": network.provider_id.rsplit("/", 1)[-1], "subnetwork": (network.subnet or "").rsplit("/", 1)[-1] or None}],
                                 "egress": EGRESS[spec.egress]}
        execution = {"taskCount": spec.tasks, "template": task}
        if spec.parallelism:
            execution["parallelism"] = spec.parallelism
        body = {"labels": translate.labels(ctx), "template": execution}
        provider_id = f"projects/{self.api.project}/locations/{region}/jobs/{planned.name}"
        resource = JobResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=region, provider_id=provider_id,
                               status=ResourceStatus.CREATING, runtime_service_account=email, cpu=spec.cpu, memory_mb=spec.memory_mb, tasks=spec.tasks,
                               parallelism=spec.parallelism, max_retries=spec.max_retries, task_timeout_seconds=spec.task_timeout_seconds,
                               network=network.id if network else None, secrets=[secret.resource_name for secret in secrets], labels=translate.labels(ctx),
                               schedule={"cron": spec.schedule.cron, "time_zone": spec.schedule.time_zone} if spec.schedule else None)
        try:
            self.api.post(self._jobs(region), json=body, jobId=planned.name)
        except InfrastructureError:
            self._forget(email, secrets)
            raise
        ctx.accepted(resource)  # known from here: a failed wait or schedule still deletes the job and its accounts
        found = self._settle(f"{RUN}/{provider_id}")
        if spec.schedule is not None:
            resource = self._schedule(resource, spec.schedule, ctx)
        return self._job_view(resource, found)

    def _schedule(self, resource: JobResource, schedule, ctx) -> JobResource:
        """The schedule's account, allowed to run this job only, then the scheduler job calling :run as it."""
        email = self.accounts.create(runtime_account(ctx.request_id, resource.name.removeprefix(f"{ctx.name}-"), "sch"),
                                     f"Runs Cloud Run job {resource.name} on its schedule. {translate.description(ctx)}")
        name = f"{self._scheduler(resource.region).split('/v1/', 1)[1]}/{resource.name}"
        resource = resource.model_copy(update={"scheduler_service_account": email, "scheduler": name})
        ctx.accepted(resource)
        member = f"serviceAccount:{email}"
        for attempt in range(NOT_YET[0]):
            try:
                update_policy(self._job_policy(resource.provider_id), lambda policy: with_member(policy, INVOKER, member))
                break
            except InfrastructureError as exc:
                if "does not exist" not in exc.message or attempt == NOT_YET[0] - 1:
                    raise
                self.api.sleep(NOT_YET[1])
        body = {"name": name, "schedule": schedule.cron, "timeZone": schedule.time_zone, "description": translate.description(ctx),
                "httpTarget": {"uri": f"{RUN}/{resource.provider_id}:run", "httpMethod": "POST",
                               "oauthToken": {"serviceAccountEmail": email, "scope": "https://www.googleapis.com/auth/cloud-platform"}}}
        try:
            self.api.post(self._scheduler(resource.region), json=body)
        except InfrastructureError as exc:
            if exc.code != ErrorCode.PROVIDER_CONFLICT:
                raise
        return resource

    def _job_view(self, resource: JobResource, found: dict) -> JobResource:
        condition = found.get("terminalCondition") or {}
        status = ResourceStatus.FAILED if condition.get("state") == "CONDITION_FAILED" else ResourceStatus.UPDATING if found.get("reconciling") else ResourceStatus.READY
        return resource.model_copy(update={"status": status, "created_at": resource.created_at or found.get("createTime")})

    def get(self, resource: JobResource) -> JobResource:
        try:
            found = self.api.get(f"{RUN}/{resource.provider_id}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED})
            raise
        return self._job_view(resource, found)

    def delete(self, resource: JobResource, ctx) -> None:
        """The schedule first, so nothing starts the job while it goes; then the job, then its accounts."""
        if resource.scheduler:
            try:
                self.api.delete(f"{SCHEDULER}/{resource.scheduler}")
            except InfrastructureError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
        url = f"{RUN}/{resource.provider_id}"
        try:
            self.api.delete(url)
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
        self._settle(url, gone=True)
        if resource.scheduler_service_account:
            self.accounts.remove(resource.scheduler_service_account, [])
        if resource.runtime_service_account:
            self._read_secrets(resource.runtime_service_account, [_Secret(name) for name in resource.secrets], give=False)
            self.accounts.remove(resource.runtime_service_account, [])


class _Secret:
    """A secret by its provider name, as the secret policy endpoint takes it."""
    def __init__(self, resource_name: str):
        self.resource_name = resource_name
