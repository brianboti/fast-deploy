"""A fake Kubernetes API for the cluster tests, at the level ClusterDeployer uses it (get, create, replace, delete, log):
objects kept by path, a Deployment that becomes available unless its image is marked bad, and pods whose state and logs
follow from that. A bad image's pods wait in CrashLoopBackOff, and their log prints the environment, as some
applications do, so the tests can see secret values scrubbed."""
import copy

from appdelivery.errors import DeliveryError, ErrorCode
from appdelivery.kube import NAME_LABEL, RELEASE_LABEL

NAMESPACE = "shop"


class FakeKube:
    def __init__(self):
        self.objects: dict[str, dict] = {}  # "/…/deployments/web" -> object
        self.bad_images: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.unreachable = False

    # ---- the client interface
    def _check(self, method, path):
        self.calls.append((method, path))
        if self.unreachable:
            raise DeliveryError(ErrorCode.CLUSTER_UNREACHABLE, "The cluster's API could not be reached (ConnectError)", {"path": path})

    def get(self, path, **params):
        self._check("GET", path)
        if params.get("labelSelector"):
            wanted = dict(item.split("=", 1) for item in params["labelSelector"].split(","))
            if path.endswith("/pods"):
                return {"items": [pod for pod in self._pods() if all(pod["metadata"]["labels"].get(k) == v for k, v in wanted.items())]}
            return {"items": [copy.deepcopy(item) for key, item in self.objects.items() if key.startswith(path + "/")
                              and all(item["metadata"].get("labels", {}).get(k) == v for k, v in wanted.items())]}
        found = self.objects.get(path)
        if found is None:
            return None
        if found["kind"] == "Deployment":
            return self._with_status(found)
        return copy.deepcopy(found)

    def create(self, collection, body):
        self._check("POST", collection)
        path = f"{collection}/{body['metadata']['name']}"
        if path in self.objects:
            raise DeliveryError(ErrorCode.CLUSTER_ERROR, f"The cluster's API answered 409 to POST {collection}", {"status": 409})
        stored = copy.deepcopy(body)
        stored["metadata"].update(resourceVersion="1", generation=1)
        if stored["kind"] == "Service":
            stored["spec"]["clusterIP"] = "10.0.0.10"
        self.objects[path] = stored

    def replace(self, path, body):
        self._check("PUT", path)
        old = self.objects[path]
        assert body["metadata"].get("resourceVersion") == old["metadata"]["resourceVersion"], "a replace names the version it read"
        if old["kind"] == "Service":
            assert body["spec"].get("clusterIP") == old["spec"]["clusterIP"], "a Service keeps its address"
        stored = copy.deepcopy(body)
        stored["metadata"].update(resourceVersion=str(int(old["metadata"]["resourceVersion"]) + 1), generation=old["metadata"]["generation"] + 1)
        self.objects[path] = stored

    def delete(self, path):
        self._check("DELETE", path)
        self.objects.pop(path, None)

    def log(self, path, **params):
        self._check("GET", path)
        pod = next(item for item in self._pods() if path.endswith(f"/pods/{item['metadata']['name']}/log"))
        secret = self.objects.get(f"/api/v1/namespaces/{NAMESPACE}/secrets/{pod['secret']}") or {}
        environment = " ".join(f"{name}={value}" for name, value in secret.get("stringData", {}).items())
        return f"starting with {environment}\npanic: bad config\n"

    # ---- what the cluster would make of it
    def deployment(self, workload="web"):
        return self.objects.get(f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{workload}")

    def image(self, workload="web"):
        found = self.deployment(workload)
        return found["spec"]["template"]["spec"]["containers"][0]["image"] if found else None

    def names(self, kind):
        return sorted(path.rsplit("/", 1)[1] for path, item in self.objects.items() if item["kind"] == kind)

    def _with_status(self, deployment):
        found = copy.deepcopy(deployment)
        replicas = found["spec"]["replicas"]
        image = found["spec"]["template"]["spec"]["containers"][0]["image"]
        good = image not in self.bad_images
        found["status"] = {"observedGeneration": found["metadata"]["generation"], "replicas": replicas,
                           "updatedReplicas": replicas if good else 1, "availableReplicas": replicas if good else 0}
        return found

    def _pods(self):
        pods = []
        for item in self.objects.values():
            if item["kind"] != "Deployment":
                continue
            template = item["spec"]["template"]
            image = template["spec"]["containers"][0]["image"]
            release = template["metadata"]["labels"][RELEASE_LABEL]
            state = {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off restarting failed container"}} if image in self.bad_images else {"running": {}}
            pods.append({"metadata": {"name": f"{item['metadata']['name']}-{release}-0", "labels": {NAME_LABEL: item["metadata"]["name"], RELEASE_LABEL: release}},
                         "status": {"containerStatuses": [{"name": "app", "state": state}]},
                         "secret": template["spec"]["containers"][0]["envFrom"][0]["secretRef"]["name"]})
        return pods
