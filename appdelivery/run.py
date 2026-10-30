"""Releases on a managed container service, Cloud Run (docs/DELIVERY-SPEC.md, phase 4): each release is a revision named
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
from .models import ReleaseRequest

RUN = "https://run.googleapis.com/v2"
STAND_IN = "us-docker.pkg.dev/cloudrun/container/hello"
REVISION, LATEST = "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION", "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
PROBE_PERIOD = 5
SETTLING = (900, 3)


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

    def __init__(self, token, client: httpx.Client | None = None):
        self.token, self.client = token, client or httpx.Client(timeout=60)

    def request(self, method: str, path: str, json=None, params=None, missing_ok: bool = False):
        try:
            response = self.client.request(method, f"{RUN}/{path}", json=json, params=params, headers={"Authorization": f"Bearer {self.token()}"})
        except httpx.RequestError as exc:
            raise DeliveryError(ErrorCode.SERVICE_UNREACHABLE, f"Cloud Run could not be reached ({type(exc).__name__})", {"path": path}) from None
        if response.status_code == 404 and missing_ok:
            return None
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
        self.run, self.sleep, self.clock = run or RunClient(token), sleep, clock

    def _settle(self, target: ServiceTarget) -> dict:
        started = self.clock()
        while True:
            found = self.run.request("GET", target.provider_id)
            if not found.get("reconciling"):
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

    def _patch(self, target: ServiceTarget, body: dict, mask: str) -> dict:
        self.run.request("PATCH", target.provider_id, json=body, params={"updateMask": mask})
        return self._settle(target)

    def _route(self, target: ServiceTarget, revision: str) -> dict:
        return self._patch(target, {"traffic": [{"type": REVISION, "revision": revision, "percent": 100}]}, "traffic")

    def deploy(self, target: ServiceTarget, release_id: str, release: ReleaseRequest, values: dict[str, str | SecretRef],
               previous: tuple[str, ReleaseRequest] | None) -> HostReport:
        found = self._settle(target)
        serving, revision = self._serving(found), revision_name(target, release_id)
        template = copy.deepcopy(found.get("template") or {})
        template["containers"] = [container(release, values, (template.get("containers") or [{}])[0].get("resources"))]
        template["revision"] = revision
        held = [{"type": REVISION, "revision": serving, "percent": 100}] if serving else [{"type": LATEST, "percent": 100}]
        after = self._patch(target, {"template": template, "traffic": held, "etag": found.get("etag")}, "template,traffic")
        if (after.get("latestReadyRevision") or "").rsplit("/", 1)[-1] != revision:  # Cloud Run gives a revision that never became ready no traffic
            return HostReport("unhealthy", {"restored": "yes" if previous else "none", "reason": self._reason(after)})
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
