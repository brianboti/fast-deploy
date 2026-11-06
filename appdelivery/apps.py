"""Releases on Azure Container Apps (docs/research/RESEARCH-2026-11-03-azure.md, sections 5.5 and 7.3): Cloud Run's
counterpart, with the same promises as run.py.

A service runs in Multiple revision mode, as cloudinfra makes it. Each release is a revision whose suffix is the release
id, made with the traffic pinned on the revision serving now; it gets all of it only once Container Apps reports it
provisioned and healthy, and the revisions before the one it replaced are deactivated (a revision with replicas costs
while it is active). A revision that fails never gets traffic and is deactivated, so a failure needs no restore. Rollback
activates the previous release's revision again and moves the traffic back; remove puts Microsoft's sample image back.

Secrets reach the container as Key Vault references pinned to a version, read by the app's runtime identity (which
cloudinfra let read the secrets its block names) at start; their values never pass through this service. A reference is
an app-wide setting in Container Apps, not a revision's, so each release's references are named after it, and the serving
release's are kept beside the new one's so that its revision still starts after a rollback. The registry is read by the
same runtime identity, which cloudinfra grants Repository Reader under the operator's app_registries prefixes.

Container Apps has one target port per app: a release may change it only while the stand-in serves.

Every call is the environment's own token for Resource Manager; this service changes the template's container, its
revision suffix, the traffic, the Key Vault references and the registry entry, and nothing else cloudinfra set (identity,
limits, scaling, ingress exposure, the Entra sign-in, which is a child resource this service never writes)."""
import copy
import math
import secrets as tokens
import time

import httpx

from .bindings import SecretRef
from .errors import DeliveryError, ErrorCode
from .machine import HostReport
from .models import ReleaseRequest, RunResponse
from .run import JobTarget, ServiceTarget

API = "https://management.azure.com"
VERSION = "2026-07-01"  # Microsoft.App, as cloudinfra's Azure adapter calls it
# cloudinfra's stand-ins (cloudinfra/providers/azure/apps.py; this package never imports it)
STAND_IN, STAND_IN_PORT = "mcr.microsoft.com/k8se/quickstart:latest", 80
JOB_STAND_IN = "mcr.microsoft.com/k8se/quickstart-jobs:latest"
RELEASE_TAG = "appdelivery-release"  # on a job: the release it holds, "none" once removed
PROBE_PERIOD = 5
MAX_FAILURES = 10  # Container Apps takes a probe's failureThreshold from 1 to 10
SETTLING = (900, 5)
# Azure RBAC takes up to ten minutes to apply a new role assignment: a 403 is tried again for that long
NOT_YET_GRANTED = (600, 15)
BUSY = (6, 10)  # 409 (another operation on the app) and 429 or 5xx, tried again this often, this far apart
DONE = ("Succeeded", "Failed", "Canceled")


class AppsClient:
    """Resource Manager calls on an app or job cloudinfra named, with the environment's token; every refusal has a code. The
    ids come from cloudinfra's answer, checked against the Container Apps id shape (infra.AZURE_APP) before they reach here."""

    def __init__(self, token, client: httpx.Client | None = None, sleep=time.sleep, clock=time.monotonic):
        self.token, self.client, self.sleep, self.clock = token, client or httpx.Client(timeout=60, follow_redirects=False), sleep, clock

    def request(self, method: str, resource_id: str, json=None, path: str = "", missing_ok: bool = False):
        started, busy = self.clock(), 0
        while True:
            try:
                response = self.client.request(method, f"{API}{resource_id}{path}", json=json, params={"api-version": VERSION},
                                               headers={"Authorization": f"Bearer {self.token()}"})
            except httpx.RequestError as exc:
                raise DeliveryError(ErrorCode.SERVICE_UNREACHABLE, f"Azure Resource Manager could not be reached ({type(exc).__name__})", {"path": path}) from None
            if response.status_code == 403 and self.clock() - started + NOT_YET_GRANTED[1] <= NOT_YET_GRANTED[0]:
                self.sleep(NOT_YET_GRANTED[1])
                continue
            if (response.status_code in (409, 429) or response.status_code >= 500) and busy < BUSY[0]:
                busy += 1
                self.sleep(BUSY[1])
                continue
            break
        if response.status_code == 404 and missing_ok:
            return None
        name = resource_id.rsplit("/", 1)[-1]
        if response.status_code in (401, 403):
            raise DeliveryError(ErrorCode.SERVICE_UNREACHABLE, f"Azure refused this environment's managed identity ({response.status_code}) for {method} {name}{path}: "
                                "it needs the request's admin grant, which gives the operator's deploy role on a service", {"path": path, "status": response.status_code,
                                                                                                                            "said": response.text[:300]})
        if response.status_code >= 400:
            raise DeliveryError(ErrorCode.SERVICE_ERROR, f"Azure answered {response.status_code} to {method} {name}{path}",
                                {"path": path, "status": response.status_code, "said": response.text[:300]})
        return response.json() if response.content else {}


def revision_name(target: ServiceTarget, release_id: str) -> str:
    return f"{target.name}--{release_id}"


def secret_name(release_id: str, variable: str) -> str:
    """A reference's name in the app: the release's id and the variable's name, as Container Apps names allow."""
    return f"{release_id}-{variable.lower().replace('_', '-')}"


def probe(release: ReleaseRequest) -> dict:
    """A startup probe that gives the container the release's health timeout, within Container Apps' bounds."""
    failures = min(MAX_FAILURES, max(1, math.ceil(release.health.timeout_seconds / PROBE_PERIOD)))
    period = max(PROBE_PERIOD, math.ceil(release.health.timeout_seconds / failures))
    return {"type": "Startup", "httpGet": {"path": release.health.path, "port": release.port}, "periodSeconds": period,
            "timeoutSeconds": min(PROBE_PERIOD, period), "failureThreshold": failures}


def references(release_id: str, values: dict[str, str | SecretRef], identity: str | None) -> tuple[list[dict], list[dict]]:
    """(the container's environment, the app's Key Vault references) of a release."""
    env, refs = [], []
    for name, value in values.items():
        if isinstance(value, SecretRef):
            refs.append({"name": secret_name(release_id, name), "keyVaultUrl": f"{value.resource_name}/{value.version}", "identity": identity})
            env.append({"name": name, "secretRef": secret_name(release_id, name)})
        else:
            env.append({"name": name, "value": value})
    return env, refs


def _resources(template: dict) -> dict | None:
    return ((template or {}).get("containers") or [{}])[0].get("resources")


def _registries(configuration: dict, release: ReleaseRequest, identity: str | None) -> list[dict]:
    """The app's registry entries, with the release's registry read as the runtime identity."""
    found = [dict(item) for item in (configuration.get("registries") or [])]
    server = release.registry()
    if server.endswith(".azurecr.io") and identity and not any(item.get("server") == server for item in found):
        found.append({"server": server, "identity": identity})
    return found


def _settled(client: AppsClient, resource_id: str, what: str) -> dict:
    """The app or job once Resource Manager is done with the last change."""
    started = client.clock()
    while True:
        found = client.request("GET", resource_id)
        state = (found.get("properties") or {}).get("provisioningState")
        if state in DONE or state is None:
            return found
        if client.clock() - started >= SETTLING[0]:
            raise DeliveryError(ErrorCode.SERVICE_ERROR, f"Azure did not finish with {what} in {SETTLING[0]} seconds", {"resource": what})
        client.sleep(SETTLING[1])


class AppsDeployer:
    """Runs a release's revision on a container app as the environment's managed identity."""

    def __init__(self, token, client: httpx.Client | None = None, apps: AppsClient | None = None, sleep=time.sleep, clock=time.monotonic):
        self.apps, self.sleep, self.clock = apps or AppsClient(token, client, sleep, clock), sleep, clock

    def _settle(self, target: ServiceTarget) -> dict:
        return _settled(self.apps, target.provider_id, target.name)

    @staticmethod
    def _serving(found: dict) -> str | None:
        properties = found.get("properties") or {}
        for item in ((properties.get("configuration") or {}).get("ingress") or {}).get("traffic") or []:
            if item.get("weight") == 100:
                return item.get("revisionName") or (properties.get("latestReadyRevisionName") if item.get("latestRevision") else None)
        return None

    def _revision(self, target: ServiceTarget, name: str) -> dict | None:
        return self.apps.request("GET", target.provider_id, path=f"/revisions/{name}", missing_ok=True)

    def _proved(self, target: ServiceTarget, revision: str) -> tuple[bool, str]:
        """(ready, why not) of a revision, once Container Apps is done provisioning it. A revision that has no replica yet (scaled
        to zero, healthState None) is taken as ready once provisioned: its startup probe runs at its first replica."""
        started = self.clock()
        while True:
            found = self._revision(target, revision)
            properties = (found or {}).get("properties") or {}
            provisioned, health, running = properties.get("provisioningState"), properties.get("healthState"), properties.get("runningState")
            if provisioned == "Failed" or health == "Unhealthy" or running in ("Failed", "Degraded"):
                return False, properties.get("provisioningError") or properties.get("runningStateDetails") or f"{revision} is {health or running or provisioned}"
            if provisioned == "Provisioned" and health in ("Healthy", "None", None) and running not in ("Activating", "Processing"):
                return True, ""
            if self.clock() - started >= SETTLING[0]:
                return False, f"{revision} did not become ready in {SETTLING[0]} seconds"
            self.sleep(SETTLING[1])

    def _patch(self, target: ServiceTarget, body: dict) -> dict:
        self.apps.request("PATCH", target.provider_id, json=body)
        found = self._settle(target)
        if (found.get("properties") or {}).get("provisioningState") == "Failed":
            raise DeliveryError(ErrorCode.SERVICE_ERROR, f"Azure did not take the change to {target.name}", {"service": target.name})
        return found

    def _route(self, target: ServiceTarget, revision: str) -> dict:
        found = self._patch(target, {"properties": {"configuration": {"ingress": {"traffic": [{"revisionName": revision, "weight": 100}]}}}})
        started = self.clock()
        while self._serving(found) != revision and self.clock() - started < SETTLING[0]:
            self.sleep(SETTLING[1])
            found = self.apps.request("GET", target.provider_id)
        return found

    def _deactivate_others(self, target: ServiceTarget, keep: set[str]) -> None:
        """Every active revision but these deactivated: an active revision's replicas cost, and only these may serve."""
        listed = self.apps.request("GET", target.provider_id, path="/revisions") or {}
        for item in listed.get("value", []):
            name = item.get("name")
            if name and name not in keep and (item.get("properties") or {}).get("active"):
                self.apps.request("POST", target.provider_id, path=f"/revisions/{name}/deactivate")

    def deploy(self, target: ServiceTarget, release_id: str, release: ReleaseRequest, values: dict[str, str | SecretRef],
               previous: tuple[str, ReleaseRequest] | None) -> HostReport:
        found = self._settle(target)
        properties = found.get("properties") or {}
        configuration, template = properties.get("configuration") or {}, copy.deepcopy(properties.get("template") or {})
        serving, revision = self._serving(found), revision_name(target, release_id)
        port = (configuration.get("ingress") or {}).get("targetPort")
        if serving and "--rel-" in serving and port not in (None, release.port):
            return HostReport("unhealthy", {"restored": "yes", "reason": f"Container Apps serves one port per app: {target.name} serves port {port} and this release "
                                                                          f"asks {release.port}; remove the running release first to change it"})
        env, refs = references(release_id, values, target.runtime)
        kept_names = set()
        if serving:
            running = (((self._revision(target, serving) or {}).get("properties") or {}).get("template") or {}).get("containers") or []
            kept_names = {item.get("secretRef") for container in running for item in container.get("env") or [] if item.get("secretRef")}
        kept = [{key: value for key, value in item.items() if key != "value"} for item in configuration.get("secrets") or [] if item.get("name") in kept_names]
        container = {"name": "app", "image": release.image, "env": env, "probes": [probe(release)]}
        if _resources(template):
            container["resources"] = _resources(template)
        template.update(containers=[container], revisionSuffix=release_id)
        held = [{"revisionName": serving, "weight": 100}] if serving else [{"latestRevision": True, "weight": 100}]
        self._patch(target, {"properties": {"configuration": {"secrets": kept + refs, "registries": _registries(configuration, release, target.runtime),
                                                              "ingress": {"targetPort": release.port, "traffic": held}}, "template": template}})
        ready, reason = self._proved(target, revision)
        if not ready:  # it never got traffic; it stops costing
            if self._revision(target, revision) is not None:
                self.apps.request("POST", target.provider_id, path=f"/revisions/{revision}/deactivate")
            return HostReport("unhealthy", {"restored": "yes" if previous else "none", "reason": reason})
        if serving != revision and self._serving(self._route(target, revision)) != revision:
            return HostReport("unhealthy", {"restored": "yes" if previous else "none", "reason": f"traffic did not move to {revision}"})
        self._deactivate_others(target, {revision})
        return HostReport("healthy", {"image": release.image, "release": release_id, "previous": previous[0] if previous else ""})

    def rollback(self, target: ServiceTarget, release_id: str, previous_id: str, previous: ReleaseRequest) -> HostReport:
        """The previous release's revision active again and serving; Container Apps keeps it, with its references."""
        revision = revision_name(target, previous_id)
        found = self._revision(target, revision)
        if found is None:
            return HostReport("no_previous")
        if not (found.get("properties") or {}).get("active"):
            self.apps.request("POST", target.provider_id, path=f"/revisions/{revision}/activate")
            ready, reason = self._proved(target, revision)
            if not ready:
                return HostReport("rollback_unhealthy", {"reason": reason})
        if self._serving(self._route(target, revision)) != revision:
            return HostReport("rollback_unhealthy", {"reason": f"traffic did not move to {revision}"})
        self._deactivate_others(target, {revision})
        return HostReport("rolled_back", {"image": previous.image, "release": previous_id})

    def remove(self, target: ServiceTarget, workload: str | None = None) -> HostReport:
        """Microsoft's sample image back, on its port, with all traffic and no references: the app stays as cloudinfra made it."""
        found = self._settle(target)
        template = copy.deepcopy((found.get("properties") or {}).get("template") or {})
        container = {"name": "app", "image": STAND_IN, **({"resources": _resources(template)} if _resources(template) else {})}
        template.update(containers=[container], revisionSuffix=f"stand-in-{tokens.token_hex(3)}")
        self._patch(target, {"properties": {"configuration": {"secrets": [], "ingress": {"targetPort": STAND_IN_PORT, "traffic": [{"latestRevision": True, "weight": 100}]}},
                                            "template": template}})
        after = self._settle(target)
        serving = self._serving(after)
        if serving is None or "--rel-" in serving:
            raise DeliveryError(ErrorCode.REMOVE_FAILED, f"{target.name} still serves {serving or 'nothing'}", {"service": target.name})
        self._deactivate_others(target, {serving})
        return HostReport("removed")

    def status(self, target: ServiceTarget, workload: str | None = None) -> HostReport:
        found = self.apps.request("GET", target.provider_id)
        serving = self._serving(found)
        if serving is None:
            return HostReport("status", {"image": "", "release": "", "running": "no"})
        revision = self._revision(target, serving) or {}
        image = ((((revision.get("properties") or {}).get("template") or {}).get("containers") or [{}])[0]).get("image", "")
        release = serving.split("--", 1)[1] if "--rel-" in serving else ""
        return HostReport("status", {"image": image, "release": release, "running": "yes" if release else "no"})


class AppsJobDeployer:
    """A Container Apps job's container, as the environment's managed identity: a release replaces it whole, with its own
    references; rollback puts the previous release's back with its references pinned again, remove the stand-in, and start
    begins a run (Jobs Contributor, which cloudinfra gives admin)."""

    def __init__(self, token, client: httpx.Client | None = None, apps: AppsClient | None = None, sleep=time.sleep, clock=time.monotonic):
        self.apps, self.sleep, self.clock = apps or AppsClient(token, client, sleep, clock), sleep, clock

    def _job(self, target: JobTarget) -> dict:
        return _settled(self.apps, target.provider_id, target.name)

    def _put(self, target: JobTarget, found: dict, container: dict, refs: list[dict], registries: list[dict], release_id: str | None) -> None:
        properties = found.get("properties") or {}
        template = copy.deepcopy(properties.get("template") or {})
        resources = _resources(template)
        template["containers"] = [{**container, **({"resources": resources} if resources else {})}]
        tags = {**(found.get("tags") or {}), RELEASE_TAG: release_id or "none"}
        self.apps.request("PATCH", target.provider_id, json={"tags": tags, "properties": {"configuration": {"secrets": refs, "registries": registries}, "template": template}})
        after = self._job(target)
        if (after.get("properties") or {}).get("provisioningState") == "Failed":
            raise DeliveryError(ErrorCode.SERVICE_ERROR, "Azure did not take the job's new container", {"job": target.name})

    def _release(self, target: JobTarget, release_id: str, release: ReleaseRequest, values: dict) -> None:
        found = self._job(target)
        env, refs = references(release_id, values, target.runtime)
        configuration = (found.get("properties") or {}).get("configuration") or {}
        self._put(target, found, {"name": "job", "image": release.image, "env": env}, refs, _registries(configuration, release, target.runtime), release_id)

    def deploy(self, target: JobTarget, release_id: str, release: ReleaseRequest, values: dict[str, str | SecretRef],
               previous: tuple[str, ReleaseRequest] | None) -> HostReport:
        self._release(target, release_id, release, values)
        return HostReport("healthy", {"image": release.image, "release": release_id, "previous": previous[0] if previous else ""})

    def rollback(self, target: JobTarget, release_id: str, previous_id: str, previous: ReleaseRequest, values: dict[str, str | SecretRef] | None = None) -> HostReport:
        self._release(target, previous_id, previous, values or {})
        return HostReport("rolled_back", {"image": previous.image, "release": previous_id})

    def remove(self, target: JobTarget, workload: str | None = None) -> HostReport:
        found = self._job(target)
        registries = list(((found.get("properties") or {}).get("configuration") or {}).get("registries") or [])
        self._put(target, found, {"name": "job", "image": JOB_STAND_IN}, [], registries, None)
        return HostReport("removed")

    def status(self, target: JobTarget, workload: str | None = None) -> HostReport:
        found = self.apps.request("GET", target.provider_id)
        image = ((((found.get("properties") or {}).get("template") or {}).get("containers") or [{}])[0]).get("image", "")
        release = (found.get("tags") or {}).get(RELEASE_TAG, "")
        release = "" if release == "none" else release
        return HostReport("status", {"image": image, "release": release, "running": "yes" if release else "no"})

    @staticmethod
    def _tasks(found: dict) -> int:
        configuration = (found.get("properties") or {}).get("configuration") or {}
        trigger = configuration.get("manualTriggerConfig") or configuration.get("scheduleTriggerConfig") or {}
        return int(trigger.get("replicaCompletionCount") or 1)

    def start(self, target: JobTarget, release_id: str) -> RunResponse:
        """Start a run now, with the job's own template (a start's body could carry another image and command: never sent)."""
        started = self.apps.request("POST", target.provider_id, path="/start", json={}) or {}
        name = started.get("name") or (started.get("id") or "").rsplit("/", 1)[-1]
        if not name:
            listed = (self.apps.request("GET", target.provider_id, path="/executions") or {}).get("value", [])
            newest = max(listed, key=lambda item: (item.get("properties") or {}).get("startTime") or "", default={})
            name = newest.get("name", "")
        return self.execution(target, release_id, name)

    def execution(self, target: JobTarget, release_id: str, name: str) -> RunResponse:
        found = self.apps.request("GET", target.provider_id, path=f"/executions/{name}", missing_ok=True)
        if found is None:
            raise DeliveryError(ErrorCode.NOT_FOUND, f"The job {target.name} has no run {name}", {"execution": name})
        properties = found.get("properties") or {}
        state = properties.get("status") or "Unknown"
        status = {"Succeeded": "SUCCEEDED", "Failed": "FAILED", "Degraded": "FAILED", "Stopped": "CANCELLED"}.get(state, "RUNNING")
        tasks = self._tasks(self.apps.request("GET", target.provider_id))
        # Container Apps reports a run's state, not each task's: the counts follow it
        return RunResponse(release_id=release_id, execution=name, status=status, tasks=tasks, succeeded=tasks if status == "SUCCEEDED" else 0,
                           failed=tasks if status == "FAILED" else 0, running=tasks if status == "RUNNING" else 0, started_at=properties.get("startTime"),
                           finished_at=properties.get("endTime"), log_url=None)
