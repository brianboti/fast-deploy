"""Container Apps behind the generic service and job primitives (docs/research/RESEARCH-2026-11-03-azure.md, section 5.5):
the shell only, as on Google. A service or a job is made with Microsoft's sample image standing in, its limits and
scaling, and a runtime identity made for it that may read the secrets its block names and nothing else; the delivery
layer puts releases on it as revisions, and adds the Key Vault references its containers read the secrets by.

The environment. Apps and jobs run in a Container Apps environment, which is the network's: one per network, in the
network's own subnet for it (network.py, a /27 delegated to Microsoft.App/environments), internal (its load balancer
has a private address only), on the Consumption workload profile, which costs nothing while nothing runs. The first
service or job on a network makes it; the network's delete removes it. A network made without that subnet, in a
request that had no service or job, cannot hold one, and one whose rules refuse egress cannot run Container Apps'
platform; each says so.

Who may call a service. Ingress is published on the environment's internal load balancer, so a service is reachable
from its own network (the network's security group admits nothing else), never from the internet (ingress all is
refused). A service that is
not public has Entra sign-in in front of it (the built-in authentication, authConfigs/current), which answers 401 to a
call without a token for the operator's app registration (providers.azure.service_auth_client_id) and 403 to a caller
whose object id or group is not on the service's allow-list; the list starts with nobody's id and a grant at use or
admin adds the member, as Cloud Run's invoker role does on Google
(learn.microsoft.com/azure/app-service/configure-authentication-provider-aad, "Use a built-in authorization policy").
The allow-list holds at most 500 characters of object ids (13 members); a group is one entry, and its members pass
when the app registration puts groups in its tokens. admin also gets Container Apps Operator on the app (revisions,
logs, a console), never Contributor, which could turn the sign-in off; a new revision is the delivery layer's.

Jobs. Manual, or on a schedule, which Container Apps evaluates in UTC and runs itself: no scheduler and no second
identity, unlike Cloud Scheduler. All of a run's tasks start at once; Container Apps gives a task no index. use reads
the job and its runs (Container Apps Jobs Reader); admin also starts and changes it (Jobs Contributor), since Azure's
start takes an image and a command of the caller's choosing and runs them as the job's identity.
"""
import threading

from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import JobResource, ResourceStatus, ServiceResource
from . import translate
from .api import API_VERSIONS
from .network import apps_subnet_name, environment_of, reaches_internet

VERSION = API_VERSIONS["Microsoft.App"]
IDENTITY_VERSION = API_VERSIONS["Microsoft.ManagedIdentity"]
NETWORK_VERSION = API_VERSIONS["Microsoft.Network"]
STAND_IN = "mcr.microsoft.com/k8se/quickstart:latest"  # Microsoft's Container Apps sample, serving on port 80
STAND_IN_PORT = 80
JOB_STAND_IN = "mcr.microsoft.com/k8se/quickstart-jobs:latest"
PROFILE = "Consumption"
NOBODY = "00000000-0000-0000-0000-000000000000"  # the allow-list's only entry until a member is granted: no object has this id
ALLOW_LIST_CHARACTERS = 500
STATES = {"succeeded": ResourceStatus.READY, "inprogress": ResourceStatus.UPDATING, "failed": ResourceStatus.FAILED, "canceled": ResourceStatus.FAILED,
          "deleting": ResourceStatus.DELETING}
ENVIRONMENT_FAILED = ("failed", "canceled", "upgradefailed", "scheduledfordelete")


def auth_config(app_id: str) -> str:
    return f"{app_id}/authConfigs/current"


class AzureApps:
    def __init__(self, api, settings, cloud, groups, access, tenant, zones, ceiling):
        self.api, self.settings, self.cloud, self.groups, self.access, self.tenant, self.zones, self.ceiling = (
            api, settings, cloud, groups, access, tenant, zones, ceiling)
        self._environments: set[str] = set()
        self._guard = threading.Lock()  # guards the table of locks below
        self._writing: dict[str, threading.Lock] = {}  # one lock per environment: a network's first service or job waits for its environment only
        self._auth = threading.Lock()  # an allow-list is read, changed and written back, one change at a time

    # ---- the network's environment
    def ensure_environment(self, network, kind: str) -> str:
        """The network's environment, made if it is not there yet; its id."""
        environment = environment_of(network)
        if not reaches_internet(network, False):
            raise InfrastructureError(ErrorCode.INVALID_SERVICE_CONFIGURATION, f"Network {network.name}'s rules refuse egress, which Container Apps' platform needs; "
                                      f"give the {kind} a network the operator does not deny egress on", {"network": network.id})
        with self._guard:
            lock = self._writing.setdefault(environment.lower(), threading.Lock())
        with lock:
            if environment.lower() in self._environments:
                return environment
            found = self.api.find(self.api.arm(environment), VERSION)
            state = str(((found or {}).get("properties") or {}).get("provisioningState") or "").lower()
            if found is None or state in ENVIRONMENT_FAILED:
                vnet = self.api.find(self.api.arm(network.provider_id), NETWORK_VERSION) or {}
                subnets = {item.get("name") for item in (vnet.get("properties") or {}).get("subnets") or []}
                if apps_subnet_name(network.name) not in subnets:
                    raise InfrastructureError(ErrorCode.INVALID_SERVICE_CONFIGURATION, f"Network {network.name} has no subnet for Container Apps: it was made in a "
                                              f"request without a service or job. Give the {kind} a network of its own request", {"network": network.id})
                self.api.put(self.api.arm(environment), {"location": network.region, "tags": dict(network.labels), "properties": {
                    "vnetConfiguration": {"infrastructureSubnetId": f"{network.provider_id}/subnets/{apps_subnet_name(network.name)}", "internal": True},
                    "workloadProfiles": [{"name": PROFILE, "workloadProfileType": PROFILE}], "zoneRedundant": bool(self.zones(network.region)),
                    "appLogsConfiguration": {"destination": "azure-monitor"}}}, VERSION, timeout=self.ceiling("environment"))
            self._environments.add(environment.lower())
            return environment

    # ---- the runtime identity
    def _runtime(self, identity_id: str, ctx) -> str:
        made = self.api.put(self.api.arm(identity_id), {"location": ctx.region, "tags": translate.tags(ctx)}, IDENTITY_VERSION, timeout=self.ceiling("identity"))
        return (made.get("properties") or {}).get("principalId")

    def _principal_of(self, identity_id: str | None) -> str | None:
        found = self.api.find(self.api.arm(identity_id), IDENTITY_VERSION) if identity_id else None
        return ((found or {}).get("properties") or {}).get("principalId")

    def _read_secrets(self, principal: str | None, secrets: list[str], give: bool, ctx=None) -> None:
        """Key Vault Secrets User on each secret, at the secret's own scope, for the runtime identity; or taken back."""
        if not principal or not secrets:
            return
        targets, who = [(secret, "secret") for secret in secrets], {"id": principal, "type": "ServicePrincipal"}
        if give:
            self.access.grant(targets, who, "use", f"cloudinfra request {ctx.request_id} ({ctx.name}): the runtime of a container service or job")
        else:
            self.access.revoke(targets, who)

    def _forget(self, resource) -> None:
        """The runtime identity's grants, then the identity itself, after its app or job is gone."""
        principal = self._principal_of(resource.runtime_service_account)
        self._read_secrets(principal, list(resource.secrets), give=False)
        if resource.runtime_service_account:
            self.api.delete(self.api.arm(resource.runtime_service_account), IDENTITY_VERSION, timeout=self.ceiling("identity"))

    def _start(self, kind: str, planned, ctx, network, secrets):
        if network is None:
            raise InfrastructureError(ErrorCode.INVALID_SERVICE_CONFIGURATION, f"A {kind} on Azure runs in its network's Container Apps environment: give it the "
                                      "request's network, or network_id naming one this service created", resource=planned.name)
        group = self.groups.ensure(ctx)
        collection = "containerApps" if kind == "service" else "jobs"
        return (f"{self.groups.id(group)}/providers/Microsoft.App/{collection}/{planned.name}",
                f"{self.groups.id(group)}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{planned.name}-run", [secret.provider_id for secret in secrets])

    @staticmethod
    def _container(name: str, image: str, spec) -> dict:
        return {"name": name, "image": image, "resources": {"cpu": spec.cpu, "memory": f"{translate.app_memory_mb(spec) // 1024}Gi"}}

    # ---- services
    def create_service(self, spec, planned, ctx, network, secrets) -> ServiceResource:
        app_id, identity_id, secret_ids = self._start("service", planned, ctx, network, secrets)
        tags = translate.tags(ctx)
        resource = ServiceResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="azure", region=ctx.region,
                                   provider_id=app_id, status=ResourceStatus.CREATING, runtime_service_account=identity_id, cpu=spec.cpu,
                                   memory_mb=translate.app_memory_mb(spec), min_instances=spec.min_instances, max_instances=spec.max_instances, ingress=spec.ingress,
                                   public=spec.public, network=network.id, secrets=secret_ids, labels=tags)
        ctx.accepted(resource)  # the app and its identity are named here: a cleanup finds both
        principal = self._runtime(identity_id, ctx)
        self._read_secrets(principal, secret_ids, give=True, ctx=ctx)
        environment = self.ensure_environment(network, "service")
        self.api.put(self.api.arm(app_id), {"location": ctx.region, "tags": tags, "identity": {"type": "UserAssigned", "userAssignedIdentities": {identity_id: {}}},
                                            "properties": {"environmentId": environment, "workloadProfileName": PROFILE, "configuration": {
                                                "activeRevisionsMode": "Multiple",
                                                "ingress": {"external": True, "targetPort": STAND_IN_PORT, "transport": "auto", "allowInsecure": False,
                                                            "traffic": [{"latestRevision": True, "weight": 100}]}},
                                                "template": {"containers": [self._container("app", STAND_IN, spec)],
                                                             "scale": {"minReplicas": spec.min_instances, "maxReplicas": spec.max_instances,
                                                                       "rules": [{"name": "http", "http": {"metadata": {"concurrentRequests": str(spec.concurrency)}}}]}}}},
                     VERSION, timeout=self.ceiling("app"))
        if not spec.public:
            self._write_auth(app_id, [], [])
        return self.get_service(resource)

    def _auth_body(self, identities: list[str], groups: list[str]) -> dict:
        client = self.settings.service_auth_client_id
        return {"properties": {"platform": {"enabled": True}, "globalValidation": {"unauthenticatedClientAction": "Return401"},
                               "identityProviders": {"azureActiveDirectory": {
                                   "enabled": True, "registration": {"clientId": client, "openIdIssuer": f"{self.cloud.login}/{self.tenant()}/v2.0"},
                                   "validation": {"allowedAudiences": [f"api://{client}"],
                                                  "defaultAuthorizationPolicy": {"allowedPrincipals": {"identities": identities or [NOBODY], "groups": groups}}}}}}}

    def _write_auth(self, app_id: str, identities: list[str], groups: list[str]) -> None:
        if not self.settings.service_auth_client_id:
            raise InfrastructureError(ErrorCode.INVALID_SERVICE_CONFIGURATION, "This operator names no app registration for services' Entra sign-in "
                                      "(providers.azure.service_auth_client_id), so a service on Azure can only be public")
        if len(",".join(identities)) > ALLOW_LIST_CHARACTERS:
            raise InfrastructureError(ErrorCode.INVALID_ACCESS, f"A service's Entra sign-in allows at most {ALLOW_LIST_CHARACTERS} characters of object ids "
                                      f"({ALLOW_LIST_CHARACTERS // 37} members); grant a group instead", {"field": "access"})
        self.api.put(self.api.arm(auth_config(app_id)), self._auth_body(identities, groups), VERSION, timeout=self.ceiling("app"))

    def allow(self, resource: ServiceResource, principal: dict, allowed: bool) -> list[str]:
        """Put a member on a service's allow-list (or take it off); the names of what was changed."""
        with self._auth:
            found = self.api.find(self.api.arm(auth_config(resource.provider_id)), VERSION)
            if found is None:
                if allowed and not resource.public and self.api.find(self.api.arm(resource.provider_id), VERSION) is not None:
                    raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Service {resource.name} has lost its Entra sign-in; delete and make it again")
                return []  # a public service: anyone on its network calls it
            policy = ((((found.get("properties") or {}).get("identityProviders") or {}).get("azureActiveDirectory") or {}).get("validation") or {}) \
                .get("defaultAuthorizationPolicy") or {}
            principals = policy.get("allowedPrincipals") or {}
            identities = [item for item in principals.get("identities") or [] if item != NOBODY]
            groups = list(principals.get("groups") or [])
            listed = groups if principal["type"] == "Group" else identities
            if allowed == (principal["id"] in listed):
                return ["Entra sign-in allow-list"] if allowed else []
            if allowed:
                listed.append(principal["id"])
            else:
                listed.remove(principal["id"])
            self._write_auth(resource.provider_id, identities, groups)
        return ["Entra sign-in allow-list"] if allowed else []

    def _view(self, resource, found: dict):
        properties = found.get("properties") or {}
        state = str(properties.get("provisioningState") or "").lower()
        status = STATES.get(state, ResourceStatus.UNKNOWN)
        if resource.kind.value == "service":
            fqdn = ((properties.get("configuration") or {}).get("ingress") or {}).get("fqdn")
            return resource.model_copy(update={"status": status, "url": f"https://{fqdn}" if fqdn else resource.url,
                                               "revision": properties.get("latestReadyRevisionName") or resource.revision})
        return resource.model_copy(update={"status": status})

    def get_service(self, resource: ServiceResource) -> ServiceResource:
        found = self.api.find(self.api.arm(resource.provider_id), VERSION)
        if found is None:
            return resource.model_copy(update={"status": ResourceStatus.DELETED})
        return self._view(resource, found)

    def delete_service(self, resource: ServiceResource, ctx) -> None:
        self.api.delete(self.api.arm(resource.provider_id), VERSION, timeout=self.ceiling("app"))  # its sign-in goes with it
        self._forget(resource)

    # ---- jobs
    def create_job(self, spec, planned, ctx, network, secrets) -> JobResource:
        job_id, identity_id, secret_ids = self._start("job", planned, ctx, network, secrets)
        tags = translate.tags(ctx)
        resource = JobResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="azure", region=ctx.region, provider_id=job_id,
                               status=ResourceStatus.CREATING, runtime_service_account=identity_id, cpu=spec.cpu, memory_mb=translate.app_memory_mb(spec),
                               tasks=spec.tasks, parallelism=spec.parallelism or spec.tasks, max_retries=spec.max_retries,
                               task_timeout_seconds=spec.task_timeout_seconds, network=network.id, secrets=secret_ids, labels=tags,
                               schedule={"cron": spec.schedule.cron, "time_zone": "UTC"} if spec.schedule else None)
        ctx.accepted(resource)
        principal = self._runtime(identity_id, ctx)
        self._read_secrets(principal, secret_ids, give=True, ctx=ctx)
        environment = self.ensure_environment(network, "job")
        counts = {"parallelism": spec.parallelism or spec.tasks, "replicaCompletionCount": spec.tasks}
        configuration = {"triggerType": "Schedule" if spec.schedule else "Manual", "replicaTimeout": spec.task_timeout_seconds,
                         "replicaRetryLimit": spec.max_retries}
        if spec.schedule:
            configuration["scheduleTriggerConfig"] = {"cronExpression": spec.schedule.cron, **counts}
        else:
            configuration["manualTriggerConfig"] = counts
        self.api.put(self.api.arm(job_id), {"location": ctx.region, "tags": tags, "identity": {"type": "UserAssigned", "userAssignedIdentities": {identity_id: {}}},
                                            "properties": {"environmentId": environment, "workloadProfileName": PROFILE, "configuration": configuration,
                                                           "template": {"containers": [self._container("job", JOB_STAND_IN, spec)]}}},
                     VERSION, timeout=self.ceiling("app"))
        return self.get_job(resource)

    def get_job(self, resource: JobResource) -> JobResource:
        found = self.api.find(self.api.arm(resource.provider_id), VERSION)
        if found is None:
            return resource.model_copy(update={"status": ResourceStatus.DELETED})
        return self._view(resource, found)

    def delete_job(self, resource: JobResource, ctx) -> None:
        self.api.delete(self.api.arm(resource.provider_id), VERSION, timeout=self.ceiling("app"))  # a schedule is part of the job
        self._forget(resource)
