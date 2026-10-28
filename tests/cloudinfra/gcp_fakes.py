"""A fake Google Cloud for the unit tests: every endpoint the adapter calls, answered from memory, with planted failures.

No test here touches a cloud. The fake replaces GcpApi.request, so the adapter's URL building, bodies and operation waits
run as in production, and `calls` records every one for assertions.
"""
import hashlib
import re

from cloudinfra.config import Config
from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.orchestration.provisioner import InlineExecutor, Orchestrator
from cloudinfra.providers import ProviderRegistry
from cloudinfra.providers.gcp.api import GcpApi
from cloudinfra.providers.gcp.provider import GCPProvider
from cloudinfra.state.state_manager import StateManager

PROJECT = "test-project"
TOKEN = "test-token"


def config_dict(**overrides):
    base = {"database": ":memory:", "allowed_hosts": ["127.0.0.1:*", "localhost:*", "testserver"], "providers": {"gcp": {"project_id": PROJECT, "allowed_regions": ["us-central1", "europe-west1"]}},
            "principals": [{"name": "app-layer", "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(), "can_provision": True, "can_destroy": True},
                           {"name": "reader", "token_sha256": hashlib.sha256(b"reader-token").hexdigest()},
                           {"name": "operator", "token_sha256": hashlib.sha256(b"operator-token").hexdigest(), "can_provision": True, "can_destroy": True, "scope": "all"}]}
    base.update(overrides)
    return base


def make_config(tmp_path=None, **overrides):
    data = config_dict(**overrides)
    if tmp_path is not None:
        data["database"] = str(tmp_path / "cloudinfra.db")
    return Config.model_validate(data)


class FakeGcpApi(GcpApi):
    """Google's REST surface, in memory. `fail_on` holds (method, url fragment) pairs that raise a provider error once reached."""

    def __init__(self, project=PROJECT):
        super().__init__(project, token=lambda: "fake-token", sleep=lambda seconds: None)
        self.calls = []
        self.fail_on = []
        self.instances, self.disks, self.networks, self.subnets, self.firewalls, self.addresses, self.sql, self.buckets, self.sql_users, self.sql_databases = {}, {}, {}, {}, {}, {}, {}, {}, {}, {}
        self.bucket_objects = set()
        self.peerings = set()
        self.quotas = {"CPUS": {"limit": 24, "usage": 0}, "INSTANCES": {"limit": 10, "usage": 0}, "DISKS_TOTAL_GB": {"limit": 4096, "usage": 0}, "IN_USE_ADDRESSES": {"limit": 8, "usage": 0}}
        self.monitoring_value = 0.42
        self.counter = 0

    def _op(self, kind="compute"):
        self.counter += 1
        if kind == "sql":
            return {"name": f"sql-op-{self.counter}", "status": "DONE"}
        if kind == "sn":
            return {"name": f"operations/sn-{self.counter}", "done": True}
        return {"name": f"op-{self.counter}", "status": "DONE", "selfLink": f"https://compute.googleapis.com/compute/v1/projects/{self.project}/global/operations/op-{self.counter}"}

    def request(self, method, url, json=None, params=None):
        self.calls.append((method, url, json))
        for fail_method, fragment in self.fail_on:
            if method == fail_method and fragment in url:
                raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"planted failure on {method} {fragment}", {"http_status": 400, "planted": True})
        path = url.split("googleapis.com", 1)[1]
        handlers = (
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/instances$", self._instances),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/instances/([^/]+)$", self._instance),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/instances/([^/]+)/(stop|start|setMachineType|attachDisk)$", self._instance_action),
            (r"^/compute/v1/projects/[^/]+/aggregated/instances$", self._aggregated),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/disks$", self._disks),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/disks/([^/]+)$", self._disk),
            (r"^/compute/v1/projects/[^/]+/global/(networks|firewalls|addresses)$", self._global_collection),
            (r"^/compute/v1/projects/[^/]+/global/(networks|firewalls|addresses)/([^/]+)$", self._global_item),
            (r"^/compute/v1/projects/[^/]+/regions/([^/]+)/subnetworks$", self._subnets),
            (r"^/compute/v1/projects/[^/]+/regions/([^/]+)/subnetworks/([^/]+)$", self._subnet),
            (r"^/compute/v1/projects/[^/]+/regions/([^/]+)$", self._region),
            (r"^/compute/v1/projects/[^/]+/global/operations/[^/]+/wait$", lambda m, *a: self._op()),
            (r"^/v1/projects/[^/]+/instances$", self._sql_instances),
            (r"^/v1/projects/[^/]+/instances/([^/]+)$", self._sql_instance),
            (r"^/v1/projects/[^/]+/instances/([^/]+)/(databases|users)$", self._sql_children),
            (r"^/v1/projects/[^/]+/operations/([^/]+)$", lambda m, *a: {"name": m.group(1), "status": "DONE"}),
            (r"^/storage/v1/b$", self._buckets),
            (r"^/storage/v1/b/([^/]+)$", self._bucket),
            (r"^/v1/services/servicenetworking\.googleapis\.com/connections$", self._peering),
            (r"^/v1/services/servicenetworking\.googleapis\.com/connections/[^:]+:deleteConnection$", self._unpeer),
            (r"^/v1/operations/[^/]+$", lambda m, *a: {"name": "operations/x", "done": True}),
            (r"^/v1/projects/[^/]+$", lambda m, *a: {"projectNumber": "123456789"}),
            (r"^/v3/projects/[^/]+/timeSeries$", lambda m, *a: {"timeSeries": [{"points": [{"value": {"doubleValue": self.monitoring_value}}]}]}),
        )
        for pattern, handler in handlers:
            found = re.match(pattern, path)
            if found:
                return handler(found, method, json or {}, params or {})
        raise AssertionError(f"the fake has no handler for {method} {path}")

    @staticmethod
    def _not_found(what):
        return InfrastructureError(ErrorCode.NOT_FOUND, f"Google Cloud has no such resource: {what}", {"http_status": 404})

    # ---- compute
    def _instances(self, found, method, body, params):
        zone = found.group(1)
        assert method == "POST"
        key = (zone, body["name"])
        if key in self.instances:
            raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, "already exists", {"http_status": 409})
        number = len(self.instances) + 2
        interface = {"networkIP": f"10.10.0.{number}", **{k: v for k, v in body["networkInterfaces"][0].items()}}
        if "accessConfigs" in interface:
            interface["accessConfigs"] = [{**interface["accessConfigs"][0], "natIP": f"34.0.0.{number}"}]
        self.instances[key] = {**body, "status": "RUNNING", "zone": f"zones/{zone}", "networkInterfaces": [interface], "creationTimestamp": "2026-10-28T10:00:00Z",
                               "selfLink": f"https://compute.googleapis.com/compute/v1/projects/{self.project}/zones/{zone}/instances/{body['name']}"}
        self.quotas["CPUS"]["usage"] += 1
        return self._op()

    def _instance(self, found, method, body, params):
        key = (found.group(1), found.group(2))
        if key not in self.instances:
            raise self._not_found(key[1])
        if method == "DELETE":
            del self.instances[key]
            return self._op()
        return self.instances[key]

    def _instance_action(self, found, method, body, params):
        key, action = (found.group(1), found.group(2)), found.group(3)
        if key not in self.instances:
            raise self._not_found(key[1])
        instance = self.instances[key]
        if action == "stop":
            instance["status"] = "TERMINATED"
        elif action == "start":
            instance["status"] = "RUNNING"
        elif action == "setMachineType":
            assert instance["status"] == "TERMINATED", "a machine type is set on a stopped instance"
            instance["machineType"] = body["machineType"]
        else:
            instance.setdefault("disks", []).append({"source": body["source"], "deviceName": body["deviceName"], "autoDelete": body["autoDelete"]})
        return self._op()

    def _aggregated(self, found, method, body, params):
        wanted = dict(re.findall(r'labels\.([a-z0-9_-]+)="([^"]*)"', params.get("filter", "")))
        items = {}
        for (zone, _), instance in self.instances.items():
            if all((instance.get("labels") or {}).get(key) == value for key, value in wanted.items()):
                items.setdefault(f"zones/{zone}", {"instances": []})["instances"].append(instance)
        return {"items": items}

    def _disks(self, found, method, body, params):
        zone = found.group(1)
        self.disks[(zone, body["name"])] = {**body, "status": "READY", "selfLink": f"https://compute.googleapis.com/compute/v1/projects/{self.project}/zones/{zone}/disks/{body['name']}"}
        return self._op()

    def _disk(self, found, method, body, params):
        key = (found.group(1), found.group(2))
        if key not in self.disks:
            raise self._not_found(key[1])
        if method == "DELETE":
            del self.disks[key]
            return self._op()
        return self.disks[key]

    def _global_collection(self, found, method, body, params):
        store = getattr(self, found.group(1))
        store[body["name"]] = {**body, "selfLink": f"https://compute.googleapis.com/compute/v1/projects/{self.project}/global/{found.group(1)}/{body['name']}"}
        return self._op()

    def _global_item(self, found, method, body, params):
        store, name = getattr(self, found.group(1)), found.group(2)
        if name not in store:
            raise self._not_found(name)
        if method == "DELETE":
            if found.group(1) == "networks" and any(subnet["network"].endswith("/" + name) for subnet in self.subnets.values()):
                raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, "network still has subnets", {"http_status": 400})
            del store[name]
            return self._op()
        return store[name]

    def _subnets(self, found, method, body, params):
        self.subnets[(found.group(1), body["name"])] = dict(body)
        return self._op()

    def _subnet(self, found, method, body, params):
        key = (found.group(1), found.group(2))
        if key not in self.subnets:
            raise self._not_found(key[1])
        if method == "DELETE":
            del self.subnets[key]
            return self._op()
        return self.subnets[key]

    def _region(self, found, method, body, params):
        return {"name": found.group(1), "quotas": [{"metric": metric, **values} for metric, values in self.quotas.items()]}

    # ---- sql
    def _sql_instances(self, found, method, body, params):
        name = body["name"]
        private = body["settings"]["ipConfiguration"].get("privateNetwork")
        addresses = [{"type": "PRIVATE", "ipAddress": "10.20.0.3"}] if private else [{"type": "PRIMARY", "ipAddress": "35.0.0.9"}]
        self.sql[name] = {**body, "state": "RUNNABLE", "ipAddresses": addresses, "connectionName": f"{self.project}:{body['region']}:{name}", "createTime": "2026-10-28T10:00:00Z"}
        return self._op("sql")

    def _sql_instance(self, found, method, body, params):
        name = found.group(1)
        if name not in self.sql:
            raise self._not_found(name)
        if method == "DELETE":
            assert self.sql[name]["settings"].get("deletionProtectionEnabled") is False, "deletion protection must be turned off before a delete"
            del self.sql[name]
            return self._op("sql")
        if method == "PATCH":
            self.sql[name]["settings"].update(body.get("settings", {}))
            return self._op("sql")
        return self.sql[name]

    def _sql_children(self, found, method, body, params):
        store = self.sql_databases if found.group(2) == "databases" else self.sql_users
        store.setdefault(found.group(1), []).append(dict(body))
        return self._op("sql")

    # ---- storage
    def _buckets(self, found, method, body, params):
        assert params.get("project") == self.project
        self.buckets[body["name"]] = {**body, "id": body["name"], "timeCreated": "2026-10-28T10:00:00Z"}
        return self.buckets[body["name"]]

    def _bucket(self, found, method, body, params):
        name = found.group(1)
        if name not in self.buckets:
            raise self._not_found(name)
        if method == "DELETE":
            if name in self.bucket_objects:
                raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, "The bucket you tried to delete is not empty.", {"http_status": 409})
            del self.buckets[name]
            return {}
        return self.buckets[name]

    # ---- service networking
    def _peering(self, found, method, body, params):
        self.peerings.add(body["network"])
        return self._op("sn")

    def _unpeer(self, found, method, body, params):
        self.peerings.discard(body["consumerNetwork"])
        return self._op("sn")


def make_provider(config, fake=None):
    fake = fake or FakeGcpApi()
    return GCPProvider(config.providers.gcp, api=fake), fake


def make_orchestrator(tmp_path, config=None, fake=None, executor=None):
    """An orchestrator over a fake Google Cloud, running provisioning inline unless an executor is given."""
    config = config or make_config(tmp_path)
    provider, fake = make_provider(config, fake)
    state = StateManager(config.database)
    registry = ProviderRegistry(config, {"gcp": provider})
    return Orchestrator(config, state, registry, executor or InlineExecutor(), sleep=lambda seconds: None), fake


def principal(config, name="app-layer"):
    return next(item for item in config.principals if item.name == name)


VM_REQUEST = {"provider": "gcp", "region": "us-central1", "compute": {"cpu": 4, "memory_gb": 16, "os": "ubuntu"}}
FULL_REQUEST = {"provider": "gcp", "region": "us-central1", "name": "analytics", "labels": {"team": "data"},
                "compute": {"count": 2, "cpu": 4, "memory_gb": 16, "os": "ubuntu", "public_ip": True},
                "storage": {"type": "persistent", "size_gb": 200},
                "database": {"engine": "postgresql", "size": "small"},
                "network": {"public_access": True}}
