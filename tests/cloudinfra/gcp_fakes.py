"""A fake Google Cloud for the unit tests: every endpoint the adapter calls, answered from memory, with planted failures.

No test here touches a cloud. The fake replaces GcpApi.request, so the adapter's URL building, bodies and operation waits
run as in production, and `calls` records every one for assertions.
"""
import copy
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
    # The sweeper's thread waits a day before its first sweep, so tests drive Sweeper.tick() themselves; lifetimes and idle
    # stop need the sweeper on. Members in example.com may be granted, so a database's password secret can be.
    base = {"database": ":memory:", "allowed_hosts": ["127.0.0.1:*", "localhost:*", "testserver"], "sweep_interval_seconds": 86400,
            "access": {"allowed_domains": ["example.com"]},
            "providers": {"gcp": {"project_id": PROJECT, "allowed_regions": ["us-central1", "europe-west1"]}},
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
        self.topics, self.subscriptions, self.secrets, self.secret_versions, self.clusters, self.redis, self.instance_groups, self.regional = {}, {}, {}, {}, {}, {}, {}, {}
        for collection in ("healthChecks", "backendServices", "urlMaps", "targetHttpProxies", "forwardingRules"):
            setattr(self, collection, {})
        self.bucket_objects = set()
        self.peerings = set()
        self.namespaces: dict[str, set[str]] = {}  # cluster endpoint -> namespaces
        self.role_bindings: dict[tuple[str, str, str], dict] = {}  # (endpoint, namespace, name) -> {"role", "member"}
        self.kube_fail = None  # an InfrastructureError the fake Kubernetes API raises once, when set
        self.policies = {}  # IAM policies by resource: ("instance", zone, name), ("tunnel", zone, id), ("secret", name), ("bucket", name), ("topics" | "subscriptions", name)
        self.started_at = "2026-10-28T10:00:00Z"  # what lastStartTimestamp says after a create or a start
        self.quotas = {"CPUS": {"limit": 24, "usage": 0}, "INSTANCES": {"limit": 10, "usage": 0}, "DISKS_TOTAL_GB": {"limit": 4096, "usage": 0}, "IN_USE_ADDRESSES": {"limit": 8, "usage": 0}}
        self.monitoring_value = 0.42
        self.monitoring_by_instance = {}  # an instance id's own CPU share, when a test sets one; the filter names the id
        self.monitoring_filters = []
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
        host, path = url.split("://", 1)[1].split("/", 1)
        path = "/" + path.split("?", 1)[0]
        handlers = (
            (r"^/v1/projects/([^/]+)/iap_tunnel/zones/([^/]+)/instances/([^/:]+):(getIamPolicy|setIamPolicy)$", self._tunnel_policy) if host.startswith("iap") else (r"^$", None),
            (r"^/v1/projects/[^/]+/(topics|subscriptions)/([^/:]+):(getIamPolicy|setIamPolicy)$", self._pubsub_policy) if host.startswith("pubsub") else (r"^$", None),
            (r"^/v1/projects/[^/]+/(topics|subscriptions)/([^/]+)$", self._pubsub) if host.startswith("pubsub") else (r"^$", None),
            (r"^/v1/projects/[^/]+/secrets$", self._secrets) if host.startswith("secretmanager") else (r"^$", None),
            (r"^/v1/projects/[^/]+/secrets/([^/:]+)(:addVersion|:getIamPolicy|:setIamPolicy)?$", self._secret) if host.startswith("secretmanager") else (r"^$", None),
            (r"^/v1/projects/[^/]+/locations/([^/]+)/clusters$", self._clusters) if host.startswith("container") else (r"^$", None),
            (r"^/v1/projects/[^/]+/locations/([^/]+)/clusters/([^/]+)$", self._cluster) if host.startswith("container") else (r"^$", None),
            (r"^/v1/projects/[^/]+/locations/([^/]+)/operations/([^/]+)$", lambda m, *a: {"name": m.group(2), "status": "DONE", "done": True}),
            (r"^/v1/projects/[^/]+/locations/([^/]+)/instances$", self._redis_instances) if host.startswith("redis") else (r"^$", None),
            (r"^/v1/projects/[^/]+/locations/([^/]+)/instances/([^/]+)$", self._redis_instance) if host.startswith("redis") else (r"^$", None),
            (r"^/v1/projects/[^/]+/locations/([^/]+)/instances/([^/]+)/authString$", self._redis_auth) if host.startswith("redis") else (r"^$", None),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/instanceGroups$", self._instance_groups),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/instanceGroups/([^/]+)$", self._instance_group),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/instanceGroups/([^/]+)/(addInstances|setNamedPorts|removeInstances)$", self._instance_group_action),
            (r"^/compute/v1/projects/[^/]+/regions/([^/]+)/(healthChecks|backendServices|forwardingRules|addresses)$", self._regional_collection),
            (r"^/compute/v1/projects/[^/]+/regions/([^/]+)/(healthChecks|backendServices|forwardingRules|addresses)/([^/]+)$", self._regional_item),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/instances$", self._instances),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/instances/([^/]+)$", self._instance),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/instances/([^/]+)/(stop|start|resume|setMachineType|attachDisk)$", self._instance_action),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/instances/([^/]+)/(getIamPolicy|setIamPolicy)$", self._instance_policy),
            (r"^/compute/v1/projects/[^/]+/aggregated/instances$", self._aggregated),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/disks$", self._disks),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/disks/([^/]+)$", self._disk),
            (r"^/compute/v1/projects/[^/]+/global/(networks|firewalls|addresses|healthChecks|backendServices|urlMaps|targetHttpProxies|forwardingRules)$", self._global_collection),
            (r"^/compute/v1/projects/[^/]+/global/(networks|firewalls|addresses|healthChecks|backendServices|urlMaps|targetHttpProxies|forwardingRules)/([^/]+)$", self._global_item),
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
            (r"^/storage/v1/b/([^/]+)/iam$", self._bucket_policy),
            (r"^/v1/services/servicenetworking\.googleapis\.com/connections$", self._peering),
            (r"^/v1/services/servicenetworking\.googleapis\.com/connections/[^:]+:deleteConnection$", self._unpeer),
            (r"^/v1/operations/[^/]+$", lambda m, *a: {"name": "operations/x", "done": True}),
            (r"^/v1/projects/[^/]+$", lambda m, *a: {"projectNumber": "123456789"}),
            (r"^/v3/projects/[^/]+/timeSeries$", self._time_series),
        )
        for pattern, handler in handlers:
            found = handler and re.match(pattern, path)
            if found:
                return handler(found, method, json or {}, params or {})
        raise AssertionError(f"the fake has no handler for {method} {host}{path}")

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
                               "id": str(7000000000000000000 + number), "lastStartTimestamp": self.started_at,
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
        elif action == "start" and instance["status"] == "SUSPENDED":
            raise InfrastructureError(ErrorCode.PROVIDER_ERROR, "a suspended instance is resumed, not started", {"http_status": 400})
        elif action in ("start", "resume"):
            instance["status"] = "RUNNING"
            instance["lastStartTimestamp"] = self.started_at
        elif action == "setMachineType":
            assert instance["status"] == "TERMINATED", "a machine type is set on a stopped instance"
            instance["machineType"] = body["machineType"]
        else:
            instance.setdefault("disks", []).append({"source": body["source"], "deviceName": body["deviceName"], "autoDelete": body["autoDelete"]})
        return self._op()

    def _policy(self, key, writing, body):
        if writing:
            policy = copy.deepcopy(body)
            policy["etag"] = str(int(self.policies.get(key, {}).get("etag", "0")) + 1)
            self.policies[key] = policy
        return copy.deepcopy(self.policies.get(key, {"version": 1, "etag": "0"}))

    def members(self, key, role):
        """Who holds a role in one stored policy."""
        return next((binding["members"] for binding in self.policies.get(key, {}).get("bindings", []) if binding["role"] == role), [])

    def _instance_policy(self, found, method, body, params):
        key = (found.group(1), found.group(2))
        if key not in self.instances:
            raise self._not_found(key[1])
        writing = found.group(3) == "setIamPolicy"
        return self._policy(("instance", *key), writing, body.get("policy") if writing else None)

    def _tunnel_policy(self, found, method, body, params):
        assert method == "POST" and found.group(1) == "123456789", "the IAP tunnel policy is addressed by the project's number"
        assert found.group(3).isdigit(), "the IAP tunnel policy names the machine's numeric id"
        writing = found.group(4) == "setIamPolicy"
        return self._policy(("tunnel", found.group(2), found.group(3)), writing, body.get("policy") if writing else None)

    def _pubsub_policy(self, found, method, body, params):
        store, name = (self.topics if found.group(1) == "topics" else self.subscriptions), found.group(2)
        if name not in store:
            raise self._not_found(name)
        writing = found.group(3) == "setIamPolicy"
        return self._policy((found.group(1), name), writing, body.get("policy") if writing else None)

    def _bucket_policy(self, found, method, body, params):
        if found.group(1) not in self.buckets:
            raise self._not_found(found.group(1))
        return self._policy(("bucket", found.group(1)), method == "PUT", body)

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
        if found.group(1) == "addresses" and body.get("purpose") != "VPC_PEERING":
            store[body["name"]]["address"] = f"34.120.0.{len(store)}"
        return self._op()

    def _regional_collection(self, found, method, body, params):
        store = self.regional.setdefault(found.group(2), {})
        store[body["name"]] = {**body, "region": found.group(1)}
        if found.group(2) == "addresses":
            store[body["name"]]["address"] = f"35.200.0.{len(store)}"
        return self._op()

    def _regional_item(self, found, method, body, params):
        store, name = self.regional.setdefault(found.group(2), {}), found.group(3)
        if name not in store:
            raise self._not_found(name)
        if method == "DELETE":
            del store[name]
            return self._op()
        return store[name]

    def _instance_groups(self, found, method, body, params):
        self.instance_groups[(found.group(1), body["name"])] = {**body, "members": []}
        return self._op()

    def _instance_group(self, found, method, body, params):
        key = (found.group(1), found.group(2))
        if key not in self.instance_groups:
            raise self._not_found(key[1])
        if method == "DELETE":
            del self.instance_groups[key]
            return self._op()
        return self.instance_groups[key]

    def _instance_group_action(self, found, method, body, params):
        key = (found.group(1), found.group(2))
        if key not in self.instance_groups:
            raise self._not_found(key[1])
        if found.group(3) == "addInstances":
            self.instance_groups[key]["members"].extend(item["instance"] for item in body["instances"])
        return self._op()

    # ---- pubsub, secrets, clusters, redis
    def _pubsub(self, found, method, body, params):
        store, name = (self.topics if found.group(1) == "topics" else self.subscriptions), found.group(2)
        if method == "PUT":
            store[name] = dict(body)
            return store[name]
        if name not in store:
            raise self._not_found(name)
        if method == "DELETE":
            del store[name]
            return {}
        return store[name]

    def _secrets(self, found, method, body, params):
        if params["secretId"] in self.secrets:  # secret ids are unique in a project
            raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, f"Secret [{params['secretId']}] already exists", {"http_status": 409})
        self.secrets[params["secretId"]] = dict(body)
        return {"name": f"projects/{self.project}/secrets/{params['secretId']}"}

    def _secret(self, found, method, body, params):
        name = found.group(1)
        if name not in self.secrets:
            raise self._not_found(name)
        if found.group(2) in (":getIamPolicy", ":setIamPolicy"):
            writing = found.group(2) == ":setIamPolicy"
            return self._policy(("secret", name), writing, body.get("policy") if writing else None)
        if found.group(2):
            assert set(body) == {"payload"} and set(body["payload"]) == {"data"}
            self.secret_versions.setdefault(name, []).append(body["payload"]["data"])
            return {"name": f"projects/{self.project}/secrets/{name}/versions/{len(self.secret_versions[name])}"}
        if method == "DELETE":
            del self.secrets[name]
            return {}
        return {"name": f"projects/{self.project}/secrets/{name}"}

    def _clusters(self, found, method, body, params):
        cluster = body["cluster"]
        self.clusters[(found.group(1), cluster["name"])] = {**cluster, "status": "RUNNING", "endpoint": "35.201.0.7", "currentMasterVersion": "1.31.1-gke.1", "createTime": "2026-10-28T10:00:00Z",
                                                            "masterAuth": {"clusterCaCertificate": "RkFLRSBDQQ=="},
                                                            "currentNodeCount": cluster["nodePools"][0]["initialNodeCount"]}
        return {"name": f"operation-{self.counter}", "status": "DONE", "selfLink": f"https://container.googleapis.com/v1/projects/{self.project}/locations/{found.group(1)}/operations/op"}

    def _cluster(self, found, method, body, params):
        key = (found.group(1), found.group(2))
        if key not in self.clusters:
            raise self._not_found(key[1])
        if method == "DELETE":
            del self.clusters[key]
            return {"name": "operation-delete", "status": "DONE", "selfLink": f"https://container.googleapis.com/v1/projects/{self.project}/locations/{key[0]}/operations/op"}
        return self.clusters[key]

    def _redis_instances(self, found, method, body, params):
        name = params["instanceId"]
        self.redis[(found.group(1), name)] = {**body, "name": f"projects/{self.project}/locations/{found.group(1)}/instances/{name}", "state": "READY", "host": "10.30.0.3", "port": 6379,
                                              "createTime": "2026-10-28T10:00:00Z", "serverCaCerts": [{"cert": "-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----"}]}
        return {"name": f"projects/{self.project}/locations/{found.group(1)}/operations/redis-op", "done": True}

    def _redis_instance(self, found, method, body, params):
        key = (found.group(1), found.group(2))
        if key not in self.redis:
            raise self._not_found(key[1])
        if method == "DELETE":
            del self.redis[key]
            return {"name": f"projects/{self.project}/locations/{key[0]}/operations/redis-del", "done": True}
        return self.redis[key]

    def _time_series(self, found, method, body, params):
        condition = params.get("filter", "")
        self.monitoring_filters.append(condition)
        named = re.search(r'resource\.labels\.instance_id="([0-9]+)"', condition)
        value = self.monitoring_by_instance.get(named.group(1), self.monitoring_value) if named else self.monitoring_value
        return {"timeSeries": [{"points": [{"value": {"doubleValue": value}}]}]}

    def _redis_auth(self, found, method, body, params):
        assert method == "GET"
        if (found.group(1), found.group(2)) not in self.redis:
            raise self._not_found(found.group(2))
        return {"authString": "fake-redis-auth-string"}

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


class FakeKubeApi:
    """A cluster's Kubernetes API, as far as the adapter uses it: namespaces and RoleBindings, kept on the fake."""

    def __init__(self, fake: FakeGcpApi, endpoint: str, ca_certificate: str):
        assert endpoint and ca_certificate, "the adapter must know the cluster's address and CA before calling it"
        self.fake, self.endpoint = fake, endpoint

    def _maybe_fail(self):
        if self.fake.kube_fail is not None:
            error, self.fake.kube_fail = self.fake.kube_fail, None
            raise error

    def ensure_namespace(self, name, labels):
        self._maybe_fail()
        self.fake.namespaces.setdefault(self.endpoint, set()).add(name)

    def put_role_binding(self, namespace, name, cluster_role, member, labels):
        self._maybe_fail()
        assert namespace in self.fake.namespaces.get(self.endpoint, set()), f"no namespace {namespace}"
        self.fake.role_bindings[(self.endpoint, namespace, name)] = {"role": cluster_role, "member": member, "labels": labels}

    def delete_role_binding(self, namespace, name):
        self._maybe_fail()
        self.fake.role_bindings.pop((self.endpoint, namespace, name), None)


def make_provider(config, fake=None):
    fake = fake or FakeGcpApi()
    return GCPProvider(config.providers.gcp, api=fake, kube=lambda endpoint, ca: FakeKubeApi(fake, endpoint, ca)), fake


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
EVERYTHING_REQUEST = {"provider": "gcp", "region": "us-central1", "name": "platform", "network": {"public_access": True},
                      "compute": [{"group": "web", "count": 2, "cpu": 2, "memory_gb": 8}, {"group": "gpu", "cpu": 8, "memory_gb": 32, "accelerator": {"type": "l4"}}],
                      "storage": {"size_gb": 50, "group": "gpu"}, "database": {}, "cache": {"memory_gb": 2}, "queue": [{"name": "events"}, {"name": "jobs", "subscription": False}],
                      "secret": [{"name": "api-key", "generate_value": True}, {"name": "token"}], "cluster": {"node_count": 2}, "load_balancer": {"backend_group": "web", "port": 8080}}
FULL_REQUEST = {"provider": "gcp", "region": "us-central1", "name": "analytics", "labels": {"team": "data"},
                "compute": {"count": 2, "cpu": 4, "memory_gb": 16, "os": "ubuntu", "public_ip": True},
                "storage": {"type": "persistent", "size_gb": 200},
                "database": {"engine": "postgresql", "size": "small"},
                "network": {"public_access": True}}
