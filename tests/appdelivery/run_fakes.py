"""A fake Cloud Run Admin API for the service tests, at the level RunDeployer uses it (request): one service whose
revisions are made from the template it is given, ready unless their image is marked bad, and whose traffic goes where it
is told, as Cloud Run does. A revision that is not ready gets no traffic and leaves the service's reported state on the
last serving one."""
import copy

from delivery_fakes import SERVICE_NAME

from appdelivery.errors import DeliveryError, ErrorCode
from appdelivery.run import LATEST, REVISION, STAND_IN

FIRST = "shop-service-00001-abc"


class FakeRun:
    def __init__(self):
        self.revisions: dict[str, dict] = {FIRST: {"containers": [{"image": STAND_IN}], "ready": True}}
        self.template = {"serviceAccount": "run-aaaaaaaaaa-service@acme-infra.iam.gserviceaccount.com", "scaling": {"maxInstanceCount": 10},
                         "containers": [{"image": STAND_IN, "resources": {"limits": {"cpu": "1", "memory": "512Mi"}}}]}
        self.traffic = [{"type": LATEST, "percent": 100}]
        self.latest_ready, self.counter, self.etag = FIRST, 1, "e1"
        self.bad_images: set[str] = set()
        self.patches: list[tuple[str, dict]] = []
        self.unreachable = False

    def _statuses(self):
        found = []
        for item in self.traffic:
            revision = item.get("revision") if item["type"] == REVISION else self.latest_ready
            found.append({"type": item["type"], "revision": revision, "percent": item["percent"]})
        return found

    def _service(self):
        failed = [name for name, revision in self.revisions.items() if not revision["ready"]]
        condition = {"type": "Ready", "state": "CONDITION_SUCCEEDED"}
        if failed and failed[-1] == list(self.revisions)[-1]:
            condition = {"type": "Ready", "state": "CONDITION_FAILED", "message": f"Revision '{failed[-1]}' is not ready: the startup probe failed"}
        return {"name": SERVICE_NAME, "template": copy.deepcopy(self.template), "traffic": copy.deepcopy(self.traffic), "trafficStatuses": self._statuses(),
                "latestReadyRevision": f"{SERVICE_NAME}/revisions/{self.latest_ready}", "reconciling": False, "etag": self.etag, "terminalCondition": condition}

    def serving(self):
        return next(item["revision"] for item in self._statuses() if item["percent"] == 100)

    def request(self, method, path, json=None, params=None, missing_ok=False):
        if self.unreachable:
            raise DeliveryError(ErrorCode.SERVICE_UNREACHABLE, "Cloud Run refused this environment's service account (403)", {"path": path})
        if path == SERVICE_NAME and method == "GET":
            return self._service()
        if path.startswith(f"{SERVICE_NAME}/revisions/") and method == "GET":
            name = path.rsplit("/", 1)[-1]
            if name not in self.revisions:
                if missing_ok:
                    return None
                raise DeliveryError(ErrorCode.SERVICE_ERROR, "Cloud Run answered 404", {"path": path})
            return {"name": name, "containers": copy.deepcopy(self.revisions[name]["containers"])}
        assert path == SERVICE_NAME and method == "PATCH", f"no fake for {method} {path}"
        mask = params["updateMask"].split(",")
        self.patches.append((params["updateMask"], copy.deepcopy(json)))
        if "template" in mask:
            assert json.get("etag") == self.etag, "a template change names the version it read"
            template = json["template"]
            assert template["serviceAccount"] == self.template["serviceAccount"] and template["scaling"] == self.template["scaling"], "infrastructure settings untouched"
            self.counter += 1
            name = template.get("revision") or f"shop-service-{self.counter:05d}-xyz"
            ready = template["containers"][0]["image"] not in self.bad_images
            self.revisions[name] = {"containers": copy.deepcopy(template["containers"]), "ready": ready}
            self.template, self.etag = copy.deepcopy(template), f"e{self.counter}"
            if ready:
                self.latest_ready = name
        if "traffic" in mask:
            for item in json["traffic"]:
                if item["type"] == REVISION:
                    assert self.revisions.get(item["revision"], {}).get("ready"), "traffic goes only to a ready revision"
            self.traffic = copy.deepcopy(json["traffic"])
        return {"name": "operations/x"}

    def image(self):
        return self.revisions[self.serving()]["containers"][0]["image"]

    def env(self):
        return {item["name"]: item.get("value", item.get("valueSource")) for item in self.revisions[self.serving()]["containers"][0].get("env", [])}
