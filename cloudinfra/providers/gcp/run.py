"""Cloud Run services behind the generic service primitives (docs/DELIVERY-SPEC.md, phase 4): the shell only. A service
is made with Google's sample image standing in, its limits, scaling, ingress and egress, and an account made for it that
may read the request's secrets and nothing else; the delivery layer puts releases on it as revisions. Public means the
invoker check is off (`invokerIamDisabled`), Google's recommended way, and only when the operator allows it.

Create and delete are long-running; the service itself says when it is done (`reconciling`) and how it went
(`terminalCondition`), so it is polled rather than its operation."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import ResourceStatus, ServiceResource
from . import translate
from .api import RUN
from .iam import GcpIam, update_policy, with_member, without_member
from .nodeaccount import NOT_YET, NodeAccounts

STAND_IN = "us-docker.pkg.dev/cloudrun/container/hello"
INGRESS = {"all": "INGRESS_TRAFFIC_ALL", "internal": "INGRESS_TRAFFIC_INTERNAL_ONLY", "internal_and_load_balancer": "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"}
EGRESS = {"private_ranges": "PRIVATE_RANGES_ONLY", "all": "ALL_TRAFFIC"}
SECRET_READER = "roles/secretmanager.secretAccessor"  # noqa: S105 - a role's name, not a credential
SETTLING = (900, 5)  # how long a create or delete may take, and how often the service is asked


def runtime_account(request_id: str, name: str) -> str:
    """run-<10 characters of the request id>-<the service's name>: 27 characters at most, as IAM allows 30."""
    return f"run-{request_id[:10]}-{name}"


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
        if condition.get("state") == "CONDITION_FAILED":
            if strict:
                raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Cloud Run could not make {resource.name}: {condition.get('message', 'no reason given')}",
                                          {"reason": condition.get("reason")})
            status = ResourceStatus.FAILED
        else:
            status = ResourceStatus.UPDATING if found.get("reconciling") else ResourceStatus.READY
        serving = next((item.get("revision") for item in found.get("trafficStatuses", []) if item.get("percent") == 100), None)
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


class _Secret:
    """A secret by its provider name, as the secret policy endpoint takes it."""
    def __init__(self, resource_name: str):
        self.resource_name = resource_name
