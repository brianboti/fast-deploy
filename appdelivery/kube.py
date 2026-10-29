"""Releases on a cluster (docs/DELIVERY-SPEC.md, phase 2): one workload in the namespace the infrastructure layer made
for the request, where it granted this environment's service account the ClusterRole edit.

A workload is a Deployment and a ClusterIP Service of its name, and each release gets its own Secret holding its
environment (`<workload>-env-<release id>`), so a release's values never change under it and the previous release can
be put back exactly. A rollout replaces the Deployment's template, keeps every old replica until a new one is ready
(maxUnavailable 0), and is healthy when every replica of the new generation is available; it fails when Kubernetes gives
up (the progress deadline, set to the health timeout) or a new pod cannot start. On failure the previous release's
Deployment is applied again, so on a cluster a failed rollout keeps the rollback. Secrets older than the previous
release are deleted. The cluster is reached with the environment's own Google token, trusting only its CA certificate."""
import base64
import ssl
import time
from dataclasses import dataclass

import httpx

from .errors import DeliveryError, ErrorCode
from .machine import HostReport
from .models import ReleaseRequest

MANAGED = {"app.kubernetes.io/managed-by": "appdelivery"}
NAME_LABEL = "app.kubernetes.io/name"
RELEASE_LABEL = "appdelivery/release"
POLL_SECONDS = 3
STUCK = ("ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CrashLoopBackOff", "CreateContainerConfigError", "CreateContainerError", "RunContainerError")


@dataclass(frozen=True)
class ClusterTarget:
    """The cluster as the infrastructure layer reports it: where its API is, the CA to trust, and the request's namespace."""
    resource: str
    name: str
    endpoint: str
    ca_certificate: str
    namespace: str


class KubeClient:
    """The Kubernetes API calls a release needs, with the environment's Google token; every refusal has a code."""

    def __init__(self, endpoint: str, ca_certificate: str, token, client: httpx.Client | None = None):
        self.base, self.token = f"https://{endpoint}", token
        if client is None:
            context = ssl.create_default_context(cadata=base64.b64decode(ca_certificate).decode())
            client = httpx.Client(verify=context, timeout=30, follow_redirects=False)
        self.client = client

    def request(self, method: str, path: str, json=None, params=None, missing_ok: bool = False, text: bool = False):
        try:
            response = self.client.request(method, self.base + path, json=json, params=params, headers={"Authorization": "Bearer " + self.token()})
        except httpx.RequestError as exc:
            raise DeliveryError(ErrorCode.CLUSTER_UNREACHABLE, f"The cluster's API could not be reached ({type(exc).__name__})", {"path": path}) from None
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code in (401, 403):
            raise DeliveryError(ErrorCode.CLUSTER_UNREACHABLE, f"The cluster refused this environment's service account ({response.status_code}) for {method} {path}: it "
                                "needs the request's admin grant (its namespace) and container.clusterViewer", {"path": path, "status": response.status_code})
        if response.status_code >= 400:
            raise DeliveryError(ErrorCode.CLUSTER_ERROR, f"The cluster's API answered {response.status_code} to {method} {path}",
                                {"path": path, "status": response.status_code, "said": response.text[:300]})
        if text:
            return response.text
        return response.json() if response.content else {}

    def get(self, path, **params):
        return self.request("GET", path, params=params or None, missing_ok=True)

    def create(self, path, body):
        return self.request("POST", path, body)

    def replace(self, path, body):
        return self.request("PUT", path, body)

    def delete(self, path):
        return self.request("DELETE", path, missing_ok=True)

    def log(self, path, **params) -> str:
        return self.request("GET", path, params=params, missing_ok=True, text=True) or ""


def paths(namespace: str, workload: str) -> dict[str, str]:
    return {"deployments": f"/apis/apps/v1/namespaces/{namespace}/deployments", "services": f"/api/v1/namespaces/{namespace}/services",
            "secrets": f"/api/v1/namespaces/{namespace}/secrets", "pods": f"/api/v1/namespaces/{namespace}/pods", "workload": workload}


def secret_name(workload: str, release_id: str) -> str:
    return f"{workload}-env-{release_id}"


def secret_body(namespace: str, workload: str, release_id: str, values: dict[str, str]) -> dict:
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": secret_name(workload, release_id), "namespace": namespace, "labels": {**MANAGED, NAME_LABEL: workload, RELEASE_LABEL: release_id}},
            "stringData": values}


def deployment_body(namespace: str, release_id: str, release: ReleaseRequest) -> dict:
    """The workload's Deployment for one release: its image, port, health probe and its own Secret; nothing else runs."""
    workload = release.target.workload
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": workload, "namespace": namespace, "labels": {**MANAGED, NAME_LABEL: workload}, "annotations": {RELEASE_LABEL: release_id}},
            "spec": {"replicas": release.target.replicas, "revisionHistoryLimit": 2, "progressDeadlineSeconds": release.health.timeout_seconds,
                     "selector": {"matchLabels": {NAME_LABEL: workload}},
                     "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}},
                     "template": {"metadata": {"labels": {**MANAGED, NAME_LABEL: workload, RELEASE_LABEL: release_id}},
                                  "spec": {"automountServiceAccountToken": False,
                                           "containers": [{"name": "app", "image": release.image, "ports": [{"containerPort": release.port}],
                                                           "envFrom": [{"secretRef": {"name": secret_name(workload, release_id)}}],
                                                           "readinessProbe": {"httpGet": {"path": release.health.path, "port": release.port}, "periodSeconds": 5,
                                                                              "failureThreshold": 3}}]}}}}


def service_body(namespace: str, release: ReleaseRequest) -> dict:
    workload = release.target.workload
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": workload, "namespace": namespace, "labels": {**MANAGED, NAME_LABEL: workload}},
            "spec": {"type": "ClusterIP", "selector": {NAME_LABEL: workload}, "ports": [{"name": "http", "port": release.port, "targetPort": release.port}]}}


class ClusterDeployer:
    """Runs a release's workload on a cluster as the environment's service account."""

    def __init__(self, token, kube=None, sleep=time.sleep, clock=time.monotonic):
        self.token, self.sleep, self.clock = token, sleep, clock
        self.kube = kube or (lambda target: KubeClient(target.endpoint, target.ca_certificate, token))

    # ---- applying
    def _apply(self, kube, collection: str, body: dict) -> None:
        name = body["metadata"]["name"]
        found = kube.get(f"{collection}/{name}")
        if found is None:
            kube.create(collection, body)
            return
        if body["kind"] == "Service":  # a Service's address and allocated fields are kept; only selector and ports change
            body = {**found, "spec": {**found["spec"], "selector": body["spec"]["selector"], "ports": body["spec"]["ports"]}}
        else:
            body = {**body, "metadata": {**body["metadata"], "resourceVersion": found["metadata"].get("resourceVersion")}}
        kube.replace(f"{collection}/{name}", body)

    def _wait(self, kube, where: dict, release_id: str, seconds: int) -> tuple[bool, str]:
        """(healthy, reason): every replica of the Deployment's newest generation available, or why not."""
        deadline = self.clock() + seconds
        reason = "it did not become available in time"
        while True:
            deployment = kube.get(f"{where['deployments']}/{where['workload']}") or {}
            spec, status = deployment.get("spec", {}), deployment.get("status", {})
            wanted = spec.get("replicas", 1)
            current = status.get("observedGeneration", 0) >= deployment.get("metadata", {}).get("generation", 1)
            if current and status.get("updatedReplicas", 0) == wanted and status.get("availableReplicas", 0) >= wanted and status.get("replicas", 0) == wanted:
                return True, ""
            for condition in status.get("conditions", []):
                if condition.get("type") == "Progressing" and condition.get("reason") == "ProgressDeadlineExceeded":
                    return False, condition.get("message") or "Kubernetes gave up: the progress deadline passed"
            stuck = self._stuck(kube, where, release_id)
            if stuck:
                return False, stuck
            if self.clock() >= deadline:
                return False, reason
            self.sleep(POLL_SECONDS)

    @staticmethod
    def _pods(kube, where: dict, release_id: str) -> list[dict]:
        found = kube.get(where["pods"], labelSelector=f"{NAME_LABEL}={where['workload']},{RELEASE_LABEL}={release_id}") or {}
        return found.get("items", [])

    def _stuck(self, kube, where: dict, release_id: str) -> str:
        """A reason a new pod cannot start, which waiting longer will not change."""
        for pod in self._pods(kube, where, release_id):
            for container in pod.get("status", {}).get("containerStatuses", []) or []:
                waiting = (container.get("state") or {}).get("waiting") or {}
                if waiting.get("reason") in STUCK:
                    return f"{pod['metadata']['name']}: {waiting['reason']}" + (f" ({waiting.get('message')[:200]})" if waiting.get("message") else "")
        return ""

    def _log(self, kube, where: dict, release_id: str) -> str | None:
        pods = self._pods(kube, where, release_id)
        if not pods:
            return None
        return kube.log(f"{where['pods']}/{pods[0]['metadata']['name']}/log", tailLines="50") or None

    # ---- the release operations
    def deploy(self, target: ClusterTarget, release_id: str, release: ReleaseRequest, values: dict[str, str],
               previous: tuple[str, ReleaseRequest] | None) -> HostReport:
        kube, where = self.kube(target), paths(target.namespace, release.target.workload)
        kube.create(where["secrets"], secret_body(target.namespace, release.target.workload, release_id, values))
        self._apply(kube, where["deployments"], deployment_body(target.namespace, release_id, release))
        self._apply(kube, where["services"], service_body(target.namespace, release))
        healthy, reason = self._wait(kube, where, release_id, release.health.timeout_seconds + 120)
        if healthy:
            keep = {release_id} | ({previous[0]} if previous else set())
            self._prune(kube, where, keep)
            return HostReport("healthy", {"image": release.image, "release": release_id, "previous": previous[0] if previous else ""})
        log = self._log(kube, where, release_id)
        restored = "none"
        if previous:
            self._apply(kube, where["deployments"], deployment_body(target.namespace, previous[0], previous[1]))
            back, _ = self._wait(kube, where, previous[0], previous[1].health.timeout_seconds + 120)
            restored = "yes" if back else "failed"
        else:
            kube.delete(f"{where['deployments']}/{where['workload']}")
            kube.delete(f"{where['services']}/{where['workload']}")
        kube.delete(f"{where['secrets']}/{secret_name(release.target.workload, release_id)}")
        fields = {"restored": restored, "reason": reason}
        if log:
            fields["log"] = base64.b64encode(log.encode()).decode()
        return HostReport("unhealthy", fields)

    def rollback(self, target: ClusterTarget, release_id: str, previous_id: str, previous: ReleaseRequest) -> HostReport:
        """Apply the previous release's Deployment again; its Secret is still there."""
        kube, where = self.kube(target), paths(target.namespace, previous.target.workload)
        if kube.get(f"{where['secrets']}/{secret_name(previous.target.workload, previous_id)}") is None:
            return HostReport("no_previous")
        self._apply(kube, where["deployments"], deployment_body(target.namespace, previous_id, previous))
        healthy, reason = self._wait(kube, where, previous_id, previous.health.timeout_seconds + 120)
        if not healthy:
            return HostReport("rollback_unhealthy", {"reason": reason})
        self._prune(kube, where, {previous_id})
        return HostReport("rolled_back", {"image": previous.image, "release": previous_id})

    def remove(self, target: ClusterTarget, workload: str) -> HostReport:
        kube, where = self.kube(target), paths(target.namespace, workload)
        kube.delete(f"{where['deployments']}/{workload}")
        kube.delete(f"{where['services']}/{workload}")
        self._prune(kube, where, set())
        return HostReport("removed")

    def status(self, target: ClusterTarget, workload: str) -> HostReport:
        kube, where = self.kube(target), paths(target.namespace, workload)
        deployment = kube.get(f"{where['deployments']}/{workload}")
        if deployment is None:
            return HostReport("status", {"image": "", "release": "", "running": "no"})
        containers = deployment["spec"]["template"]["spec"]["containers"]
        status, wanted = deployment.get("status", {}), deployment["spec"].get("replicas", 1)
        return HostReport("status", {"image": containers[0]["image"], "release": deployment["metadata"].get("annotations", {}).get(RELEASE_LABEL, ""),
                                     "running": "yes" if status.get("availableReplicas", 0) >= wanted else "no"})

    @staticmethod
    def _prune(kube, where: dict, keep: set[str]) -> None:
        found = kube.get(where["secrets"], labelSelector=f"{NAME_LABEL}={where['workload']},app.kubernetes.io/managed-by=appdelivery") or {}
        for secret in found.get("items", []):
            if secret["metadata"].get("labels", {}).get(RELEASE_LABEL) not in keep:
                kube.delete(f"{where['secrets']}/{secret['metadata']['name']}")
