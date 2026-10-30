"""Releases on a managed container service or job, Cloud Run (docs/DELIVERY-SPEC.md, phase 4). On a service each release is a revision named
after it, made with the traffic pinned where it was, and given all of it only once Cloud Run says it is ready. A revision
that fails its startup probe never gets traffic, so a failure needs no restore and leaves the running release's rollback
as it was. Rollback moves the traffic to the previous release's revision, which Cloud Run keeps; remove puts Google's
sample image back. Secrets reach the container as references pinned to a version, read by its runtime account at start;
their values never pass through this service.

Every call is the environment's own token; this service changes the template's container, its revision name and the
traffic, and nothing the infrastructure layer set (identity, limits, scaling, ingress, egress)."""
import copy
import time
from dataclasses import dataclass

import httpx

from .bindings import SecretRef
from .errors import DeliveryError, ErrorCode
from .machine import HostReport
from .models import ReleaseRequest, RunResponse

RUN = "https://run.googleapis.com/v2"
STAND_IN = "us-docker.pkg.dev/cloudrun/container/hello"
JOB_STAND_IN = "us-docker.pkg.dev/cloudrun/container/job"
RELEASE_LABEL = "appdelivery-release"  # on a job's run template: the release the job holds
REVISION, LATEST = "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION", "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
# A revision with no traffic and no tag cannot be reached, so Cloud Run retires it at once and never starts it (found on a
# real project on 2026-10-30): the new one gets this tag until it has proved itself, as Google's no-traffic deploy does.
CANDIDATE = "candidate"
PROBE_PERIOD = 5
SETTLING = (900, 3)
# A grant cloudinfra just made reaches a service's revisions minutes after the service itself (found on a real project on
# 2026-10-30: a PATCH went through while reading the new revision was refused); a 403 is tried again for this long.
NOT_YET_GRANTED = (300, 15)


@dataclass(frozen=True)
class ServiceTarget:
    """The service as the infrastructure layer reports it: its id, its provider name and its full name at Cloud Run."""
    resource: str
    name: str
    provider_id: str


def revision_name(target: ServiceTarget, release_id: str) -> str:
    return f"{target.name}-{release_id}"


def container(release: ReleaseRequest, values: dict[str, str | SecretRef], resources: dict | None) -> dict:
    """The release's container: its image, port, environment and a startup probe on its health path, within the limits the
    infrastructure layer set."""
    env = []
    for name, value in values.items():
        if isinstance(value, SecretRef):
            env.append({"name": name, "valueSource": {"secretKeyRef": {"secret": value.resource_name, "version": value.version}}})
        else:
            env.append({"name": name, "value": value})
    probe = {"httpGet": {"path": release.health.path, "port": release.port}, "periodSeconds": PROBE_PERIOD, "timeoutSeconds": PROBE_PERIOD,
             "failureThreshold": max(1, -(-release.health.timeout_seconds // PROBE_PERIOD))}
    found = {"image": release.image, "ports": [{"containerPort": release.port}], "env": env, "startupProbe": probe}
    if resources:
        found["resources"] = resources
    return found


class RunClient:
    """The Cloud Run Admin API calls a release needs, with the environment's Google token; every refusal has a code."""

    def __init__(self, token, client: httpx.Client | None = None, sleep=time.sleep, clock=time.monotonic):
        self.token, self.client, self.sleep, self.clock = token, client or httpx.Client(timeout=60), sleep, clock

    def request(self, method: str, path: str, json=None, params=None, missing_ok: bool = False):
        started = self.clock()
        while True:
            try:
                response = self.client.request(method, f"{RUN}/{path}", json=json, params=params, headers={"Authorization": f"Bearer {self.token()}"})
            except httpx.RequestError as exc:
                raise DeliveryError(ErrorCode.SERVICE_UNREACHABLE, f"Cloud Run could not be reached ({type(exc).__name__})", {"path": path}) from None
            fresh_grant = response.status_code == 403 and "artifactregistry" not in response.text
            if not fresh_grant or self.clock() - started + NOT_YET_GRANTED[1] > NOT_YET_GRANTED[0]:
                break
            self.sleep(NOT_YET_GRANTED[1])
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code == 403 and "artifactregistry" in response.text:  # Cloud Run checks the deployer can read the image (found 2026-10-30)
            raise DeliveryError(ErrorCode.SERVICE_UNREACHABLE, "Cloud Run refused the image: this environment's service account needs Artifact Registry Reader on "
                                "the image's repository, which the operator grants once with allowed_registries", {"path": path, "status": 403, "said": response.text[:300]})
        if response.status_code in (401, 403):
            raise DeliveryError(ErrorCode.SERVICE_UNREACHABLE, f"Cloud Run refused this environment's service account ({response.status_code}) for {method} {path}: it "
                                "needs the request's admin grant on the service", {"path": path, "status": response.status_code, "said": response.text[:300]})
        if response.status_code >= 400:
            raise DeliveryError(ErrorCode.SERVICE_ERROR, f"Cloud Run answered {response.status_code} to {method} {path}",
                                {"path": path, "status": response.status_code, "said": response.text[:300]})
        return response.json() if response.content else {}


class RunDeployer:
    """Runs a release's revision on a Cloud Run service as the environment's service account."""

    def __init__(self, token, run=None, sleep=time.sleep, clock=time.monotonic):
        self.run, self.sleep, self.clock = run or RunClient(token, sleep=sleep, clock=clock), sleep, clock

    def _settle(self, target: ServiceTarget) -> dict:
        """The service or job once Cloud Run is done with the last change. Right after a write it can still report the
        state before it, not reconciling, so it is done only once it has seen the generation written (found on a real
        project on 2026-10-30, as Kubernetes does with Deployments)."""
        started = self.clock()
        while True:
            found = self.run.request("GET", target.provider_id)
            if not found.get("reconciling") and int(found.get("observedGeneration") or 0) >= int(found.get("generation") or 0):
                return found
            if self.clock() - started >= SETTLING[0]:
                raise DeliveryError(ErrorCode.SERVICE_ERROR, f"Cloud Run did not finish with {target.name} in {SETTLING[0]} seconds", {"service": target.name})
            self.sleep(SETTLING[1])

    @staticmethod
    def _serving(found: dict) -> str | None:
        return next((item.get("revision") for item in found.get("trafficStatuses", []) if item.get("percent") == 100 and item.get("revision")), None)

    @staticmethod
    def _reason(found: dict) -> str:
        failed = [item for item in [found.get("terminalCondition") or {}, *found.get("conditions", [])] if item.get("state") == "CONDITION_FAILED"]
        return "; ".join(dict.fromkeys(item.get("message") or item.get("reason") or item.get("type", "") for item in failed)) or "it did not become ready"

    def _proved(self, target: ServiceTarget, revision: str, service: dict) -> tuple[bool, str]:
        """(ready, why not) for a revision, as the revision itself reports it once Cloud Run is done with it."""
        started = self.clock()
        while True:
            found = self.run.request("GET", f"{target.provider_id}/revisions/{revision}", missing_ok=True)
            if found is None:
                return False, self._reason(service)
            ready = next((item for item in found.get("conditions", []) if item.get("type") == "Ready"), {})
            if not found.get("reconciling") and ready.get("state") in ("CONDITION_SUCCEEDED", "CONDITION_FAILED"):
                if ready.get("state") == "CONDITION_SUCCEEDED" and ready.get("revisionReason") != "RETIRED":
                    return True, ""
                return False, ready.get("message") or ready.get("revisionReason") or self._reason(service)
            if self.clock() - started >= SETTLING[0]:
                return False, f"{revision} did not finish in {SETTLING[0]} seconds"
            self.sleep(SETTLING[1])

    def _patch(self, target: ServiceTarget, body: dict, mask: str) -> dict:
        self.run.request("PATCH", target.provider_id, json=body, params={"updateMask": mask})
        return self._settle(target)

    def _route(self, target: ServiceTarget, revision: str) -> dict:
        """All traffic on the revision, once Cloud Run reports it there: right after the change it may still report the
        traffic as it was (found on a real project on 2026-10-30, moving back to a revision that had been idle)."""
        found = self._patch(target, {"traffic": [{"type": REVISION, "revision": revision, "percent": 100}]}, "traffic")
        started = self.clock()
        while self._serving(found) != revision and self.clock() - started < SETTLING[0]:
            if (found.get("terminalCondition") or {}).get("state") == "CONDITION_FAILED" and not found.get("reconciling"):
                break
            self.sleep(SETTLING[1])
            found = self.run.request("GET", target.provider_id)
        return found

    def deploy(self, target: ServiceTarget, release_id: str, release: ReleaseRequest, values: dict[str, str | SecretRef],
               previous: tuple[str, ReleaseRequest] | None) -> HostReport:
        found = self._settle(target)
        serving, revision = self._serving(found), revision_name(target, release_id)
        template = copy.deepcopy(found.get("template") or {})
        template["containers"] = [container(release, values, (template.get("containers") or [{}])[0].get("resources"))]
        template["revision"] = revision
        held = [{"type": REVISION, "revision": serving, "percent": 100}] if serving else [{"type": LATEST, "percent": 100}]
        candidate = {"type": REVISION, "revision": revision, "percent": 0, "tag": CANDIDATE}
        after = self._patch(target, {"template": template, "traffic": [*held, candidate], "etag": found.get("etag")}, "template,traffic")
        ready, reason = self._proved(target, revision, after)
        if not ready:  # it never gets traffic; only the tag goes
            self._patch(target, {"traffic": held}, "traffic")
            return HostReport("unhealthy", {"restored": "yes" if previous else "none", "reason": reason})
        routed = self._route(target, revision)
        if self._serving(routed) != revision:
            return HostReport("unhealthy", {"restored": "yes" if previous else "none", "reason": f"traffic did not move to {revision}: {self._reason(routed)}"})
        return HostReport("healthy", {"image": release.image, "release": release_id, "previous": previous[0] if previous else ""})

    def rollback(self, target: ServiceTarget, release_id: str, previous_id: str, previous: ReleaseRequest) -> HostReport:
        """All traffic back on the previous release's revision; Cloud Run keeps it, and it was ready once."""
        revision = revision_name(target, previous_id)
        if self.run.request("GET", f"{target.provider_id}/revisions/{revision}", missing_ok=True) is None:
            return HostReport("no_previous")
        if self._serving(self._route(target, revision)) != revision:
            return HostReport("rollback_unhealthy", {"reason": f"traffic did not move to {revision}"})
        return HostReport("rolled_back", {"image": previous.image, "release": previous_id})

    def remove(self, target: ServiceTarget, workload: str | None = None) -> HostReport:
        """Google's sample image back, with all traffic: the service stays, as the infrastructure layer made it."""
        found = self._settle(target)
        template = copy.deepcopy(found.get("template") or {})
        resources = (template.get("containers") or [{}])[0].get("resources")
        template["containers"] = [{"image": STAND_IN, **({"resources": resources} if resources else {})}]
        template.pop("revision", None)
        after = self._patch(target, {"template": template, "traffic": [{"type": LATEST, "percent": 100}], "etag": found.get("etag")}, "template,traffic")
        serving = self._serving(after)
        if serving is None or serving.startswith(f"{target.name}-rel-"):
            raise DeliveryError(ErrorCode.REMOVE_FAILED, f"{target.name} still serves {serving or 'nothing'}", {"service": target.name})
        return HostReport("removed")

    def status(self, target: ServiceTarget, workload: str | None = None) -> HostReport:
        found = self.run.request("GET", target.provider_id)
        serving = self._serving(found)
        if serving is None:
            return HostReport("status", {"image": "", "release": "", "running": "no"})
        revision = self.run.request("GET", f"{target.provider_id}/revisions/{serving}", missing_ok=True) or {}
        image = ((revision.get("containers") or [{}])[0]).get("image", "")
        release = serving.removeprefix(f"{target.name}-") if serving.startswith(f"{target.name}-rel-") else ""
        return HostReport("status", {"image": image, "release": release, "running": "yes" if release else "no"})


@dataclass(frozen=True)
class JobTarget:
    """The job as the infrastructure layer reports it: its id, its provider name and its full name at Cloud Run."""
    resource: str
    name: str
    provider_id: str


def job_container(release: ReleaseRequest, values: dict[str, str | SecretRef], resources: dict | None) -> dict:
    """The release's container for a job: its image and environment, within the limits the infrastructure layer set. A job
    serves nothing, so it has no port or probe; it succeeds when its tasks exit 0."""
    found = container(release, values, resources)
    return {key: value for key, value in found.items() if key not in ("ports", "startupProbe")}


class JobDeployer(RunDeployer):
    """A Cloud Run job's container, as the environment's service account: a release replaces it whole (jobs take no
    update mask; the etag guards the write), rollback puts the previous release's back, remove the stand-in, and run
    starts an execution."""

    def _job(self, target: JobTarget) -> dict:
        return self._settle(target)

    def _put(self, target: JobTarget, found: dict, task_container: dict, release_id: str | None) -> dict:
        job = copy.deepcopy(found)
        execution = job.setdefault("template", {})
        task = execution.setdefault("template", {})
        task["containers"] = [task_container]
        labels = dict(execution.get("labels") or {})
        if release_id:
            labels[RELEASE_LABEL] = release_id
        else:
            labels.pop(RELEASE_LABEL, None)
        execution["labels"] = labels
        for key in ("reconciling", "terminalCondition", "conditions", "latestCreatedExecution", "executionCount", "observedGeneration", "generation", "uid",
                    "createTime", "updateTime", "creator", "lastModifier"):
            job.pop(key, None)
        self.run.request("PATCH", target.provider_id, json=job)
        after = self._settle(target)
        condition = after.get("terminalCondition") or {}
        if condition.get("state") == "CONDITION_FAILED":
            raise DeliveryError(ErrorCode.SERVICE_ERROR, f"Cloud Run did not take the job's new container: {condition.get('message', 'no reason given')}",
                                {"job": target.name})
        return after

    @staticmethod
    def _resources(found: dict) -> dict | None:
        return (((found.get("template") or {}).get("template") or {}).get("containers") or [{}])[0].get("resources")

    def deploy(self, target: JobTarget, release_id: str, release: ReleaseRequest, values: dict[str, str | SecretRef],
               previous: tuple[str, ReleaseRequest] | None) -> HostReport:
        found = self._job(target)
        self._put(target, found, job_container(release, values, self._resources(found)), release_id)
        return HostReport("healthy", {"image": release.image, "release": release_id, "previous": previous[0] if previous else ""})

    def rollback(self, target: JobTarget, release_id: str, previous_id: str, previous: ReleaseRequest, values: dict[str, str | SecretRef] | None = None) -> HostReport:
        """The previous release's container again: its image and settings, its secrets pinned to their versions now."""
        found = self._job(target)
        self._put(target, found, job_container(previous, values or {}, self._resources(found)), previous_id)
        return HostReport("rolled_back", {"image": previous.image, "release": previous_id})

    def remove(self, target: JobTarget, workload: str | None = None) -> HostReport:
        found = self._job(target)
        resources = self._resources(found)
        self._put(target, found, {"image": JOB_STAND_IN, **({"resources": resources} if resources else {})}, None)
        return HostReport("removed")

    def status(self, target: JobTarget, workload: str | None = None) -> HostReport:
        found = self.run.request("GET", target.provider_id)
        execution = found.get("template") or {}
        image = ((execution.get("template") or {}).get("containers") or [{}])[0].get("image", "")
        release = (execution.get("labels") or {}).get(RELEASE_LABEL, "")
        return HostReport("status", {"image": image, "release": release, "running": "yes" if release else "no"})

    def start(self, target: JobTarget, release_id: str) -> RunResponse:
        """Start a run now; the job's latest execution is the one this started."""
        self.run.request("POST", f"{target.provider_id}:run", json={})
        found = self.run.request("GET", target.provider_id)
        latest = (found.get("latestCreatedExecution") or {}).get("name", "")
        return self.execution(target, release_id, latest.rsplit("/", 1)[-1])

    def execution(self, target: JobTarget, release_id: str, name: str) -> RunResponse:
        found = self.run.request("GET", f"{target.provider_id}/executions/{name}", missing_ok=True)
        if found is None:
            raise DeliveryError(ErrorCode.NOT_FOUND, f"The job {target.name} has no run {name}", {"execution": name})
        succeeded, failed, cancelled = found.get("succeededCount", 0), found.get("failedCount", 0), found.get("cancelledCount", 0)
        tasks = found.get("taskCount", 1)
        if not found.get("completionTime"):
            status = "RUNNING"
        elif cancelled:
            status = "CANCELLED"
        else:
            status = "SUCCEEDED" if succeeded >= tasks and not failed else "FAILED"
        return RunResponse(release_id=release_id, execution=name, status=status, tasks=tasks, succeeded=succeeded, failed=failed, running=found.get("runningCount", 0),
                           started_at=found.get("startTime"), finished_at=found.get("completionTime"), log_url=found.get("logUri"))
