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
        self.now = 0.0  # the fake's own clock: a sleep moves it on, so a wait that never ends runs out instead of hanging a test
        super().__init__(project, token=lambda: "fake-token", sleep=self._sleep, clock=lambda: self.now)
        self.calls = []
        self.fail_on = []
        self.instances, self.disks, self.networks, self.subnets, self.firewalls, self.addresses, self.sql, self.buckets, self.sql_users, self.sql_databases = {}, {}, {}, {}, {}, {}, {}, {}, {}, {}
        self.topics, self.subscriptions, self.secrets, self.secret_versions, self.clusters, self.redis, self.instance_groups, self.regional = {}, {}, {}, {}, {}, {}, {}, {}
        for collection in ("healthChecks", "backendServices", "urlMaps", "targetHttpProxies", "targetHttpsProxies", "forwardingRules", "sslCertificates"):
            setattr(self, collection, {})
        self.bucket_objects = set()
        self.peerings = set()
        self.namespaces: dict[str, set[str]] = {}  # cluster endpoint -> namespaces
        self.role_bindings: dict[tuple[str, str, str], dict] = {}  # (endpoint, namespace, name) -> {"role", "member"}
        self.namespace_labels: dict[tuple[str, str], dict] = {}  # (endpoint, namespace) -> its labels
        self.cluster_objects: dict[tuple[str, str], dict] = {}  # (endpoint, path) -> other objects the adapter put in a cluster
        self.order: list[tuple[str, str]] = []  # deletes, in the order they reached the fake
        self.gateway_kinds_missing = 0  # how many more Gateway API writes find no such kind yet, as just after a cluster runs
        self.service_accounts: dict[str, dict] = {}  # email -> the account
        self.run_services: dict[str, dict] = {}  # Cloud Run services by name
        self.run_jobs: dict[str, dict] = {}  # Cloud Run jobs by name
        self.zones: dict[str, dict] = {}  # Cloud DNS managed zones by name, each with its rrsets
        self.monitoring: dict[str, dict] = {}  # Cloud Monitoring channels, uptime checks and alert policies by full name
        self.datasets: dict[str, dict] = {}  # BigQuery datasets by id, with "tables" a test may set
        self.firestore: dict[str, dict] = {}  # Firestore databases by id
        self.budgets: dict[str, dict] = {}  # Cloud Billing budgets by full name
        self.sinks: dict[str, dict] = {}  # Cloud Logging sinks by full name
        self.key_rings: set[str] = set()  # Cloud KMS key rings by full name; never deleted, as Cloud KMS keeps them
        self.crypto_keys: dict[str, dict] = {}  # Cloud KMS keys by full name, with their "versions"
        self.billing_info = {"billingEnabled": True, "billingAccountName": "billingAccounts/01ABCD-234567-89EFAB"}
        self.snapshots: dict[str, dict] = {}  # disk snapshots by name, as a schedule takes them (take_snapshot)
        self.schedules: dict[str, dict] = {}  # Cloud Scheduler jobs by full name
        self.repositories = {"us-central1-docker.pkg.dev/test-project/apps", "us-central1-docker.pkg.dev/test-project/shared"}
        self.account_unknown_to_gke_for = 0  # how many more cluster creates GKE refuses because it does not know the new node account yet
        self.unknown_for = 0  # how many more grants naming a new service account IAM refuses as unknown, as it does for seconds after a create
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

    def _sleep(self, seconds):
        self.now += seconds

    def request(self, method, url, json=None, params=None):
        self.calls.append((method, url, json))
        for fail_method, fragment in self.fail_on:
            if method == fail_method and fragment in url:
                raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"planted failure on {method} {fragment}", {"http_status": 400, "planted": True})
        host, path = url.split("://", 1)[1].split("/", 1)
        path = "/" + path.split("?", 1)[0]
        handlers = (
            (r"^/v1/projects/[^/]+/serviceAccounts$", self._service_accounts) if host.startswith("iam") else (r"^$", None),
            (r"^/v1/projects/[^/]+/serviceAccounts/([^/:]+):(getIamPolicy|setIamPolicy)$", self._account_policy) if host.startswith("iam") else (r"^$", None),
            (r"^/v1/projects/[^/]+/serviceAccounts/([^/]+)$", self._service_account) if host.startswith("iam") else (r"^$", None),
            (r"^/v2/projects/[^/]+/locations/([^/]+)/services$", self._run_services_collection) if host.startswith("run") else (r"^$", None),
            (r"^/dns/v1/projects/[^/]+/managedZones$", self._zones_collection) if host.startswith("dns") else (r"^$", None),
            (r"^/bigquery/v2/projects/[^/]+/datasets$", self._datasets) if host.startswith("bigquery") else (r"^$", None),
            (r"^/bigquery/v2/projects/[^/]+/datasets/([^/]+)$", self._dataset) if host.startswith("bigquery") else (r"^$", None),
            (r"^/v1/projects/[^/]+/databases$", self._firestore_collection) if host.startswith("firestore") else (r"^$", None),
            (r"^/v1/projects/[^/]+/databases/([^/]+)$", self._firestore_item) if host.startswith("firestore") else (r"^$", None),
            (r"^/v3/projects/[^/]+/(notificationChannels|uptimeCheckConfigs|alertPolicies)$", self._monitoring_collection) if host.startswith("monitoring") else (r"^$", None),
            (r"^/v2/projects/[^/]+/sinks$", self._sinks) if host.startswith("logging") else (r"^$", None),
            (r"^/v1/projects/[^/]+/locations/[^/]+/keyRings$", self._key_rings) if host.startswith("cloudkms") else (r"^$", None),
            (r"^/v1/(projects/[^/]+/locations/[^/]+/keyRings/[^/]+)/cryptoKeys$", self._crypto_keys) if host.startswith("cloudkms") else (r"^$", None),
            (r"^/v1/(projects/[^/]+/locations/[^/]+/keyRings/[^/]+/cryptoKeys/[^/:]+)(?::(getIamPolicy|setIamPolicy))?$", self._crypto_key)
            if host.startswith("cloudkms") else (r"^$", None),
            (r"^/v1/(projects/[^/]+/locations/[^/]+/keyRings/[^/]+/cryptoKeys/[^/]+)/cryptoKeyVersions$", self._key_versions) if host.startswith("cloudkms") else (r"^$", None),
            (r"^/v1/(projects/[^/]+/locations/[^/]+/keyRings/[^/]+/cryptoKeys/[^/]+/cryptoKeyVersions/[^/:]+):destroy$", self._destroy_version)
            if host.startswith("cloudkms") else (r"^$", None),
            (r"^/storage/v1/projects/[^/]+/serviceAccount$", lambda m, *a: {"email_address": "service-123456789@gs-project-accounts.iam.gserviceaccount.com"}),
            (r"^/v1beta1/projects/[^/]+/services/sqladmin\.googleapis\.com:generateServiceIdentity$",
             lambda m, *a: {"name": "operations/x", "done": True, "response": {"email": "service-123456789@gcp-sa-cloud-sql.iam.gserviceaccount.com"}}),
            (r"^/v2/(projects/[^/]+/sinks/[^/]+)$", self._sink) if host.startswith("logging") else (r"^$", None),
            (r"^/v1/projects/[^/]+/billingInfo$", lambda m, *a: dict(self.billing_info)) if host.startswith("cloudbilling") else (r"^$", None),
            (r"^/v1/(billingAccounts/[^/]+)/budgets$", self._budgets) if host.startswith("billingbudgets") else (r"^$", None),
            (r"^/v1/(billingAccounts/[^/]+/budgets/[^/]+)$", self._budget) if host.startswith("billingbudgets") else (r"^$", None),
            (r"^/v3/(projects/[^/]+/(?:notificationChannels|uptimeCheckConfigs|alertPolicies)/[^/]+)$", self._monitoring_item) if host.startswith("monitoring") else (r"^$", None),
            (r"^/dns/v1/projects/[^/]+/managedZones/([^/:]+):(getIamPolicy|setIamPolicy)$", self._zone_policy) if host.startswith("dns") else (r"^$", None),
            (r"^/dns/v1/projects/[^/]+/managedZones/([^/:]+)(/changes|/rrsets)?$", self._zone) if host.startswith("dns") else (r"^$", None),
            (r"^/v2/projects/[^/]+/locations/([^/]+)/jobs$", self._run_jobs_collection) if host.startswith("run") else (r"^$", None),
            (r"^/v2/projects/[^/]+/locations/[^/]+/jobs/([^/:]+):(getIamPolicy|setIamPolicy)$", self._run_job_policy) if host.startswith("run") else (r"^$", None),
            (r"^/v2/projects/[^/]+/locations/[^/]+/jobs/([^/:]+)$", self._run_job) if host.startswith("run") else (r"^$", None),
            (r"^/v1/projects/[^/]+/locations/[^/]+/jobs$", self._schedules_collection) if host.startswith("cloudscheduler") else (r"^$", None),
            (r"^/v1/(projects/[^/]+/locations/[^/]+/jobs/[^/]+)$", self._schedule) if host.startswith("cloudscheduler") else (r"^$", None),
            (r"^/v2/projects/[^/]+/locations/[^/]+/services/([^/:]+):(getIamPolicy|setIamPolicy)$", self._run_policy) if host.startswith("run") else (r"^$", None),
            (r"^/v2/projects/[^/]+/locations/[^/]+/services/([^/:]+)$", self._run_service) if host.startswith("run") else (r"^$", None),
            (r"^/v1/projects/([^/:]+):(getIamPolicy|setIamPolicy)$", self._project_policy) if host.startswith("cloudresourcemanager") else (r"^$", None),
            (r"^/v1/projects/([^/]+)/locations/([^/]+)/repositories/([^/:]+):(getIamPolicy|setIamPolicy)$", self._repository_policy)
            if host.startswith("artifactregistry") else (r"^$", None),
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
            (r"^/compute/v1/projects/[^/]+/regions/([^/]+)/(healthChecks|backendServices|forwardingRules|addresses|routers|resourcePolicies)$", self._regional_collection),
            (r"^/compute/v1/projects/[^/]+/regions/([^/]+)/(healthChecks|backendServices|forwardingRules|addresses|routers|resourcePolicies)/([^/]+)$", self._regional_item),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/instances$", self._instances),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/instances/([^/]+)$", self._instance),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/instances/([^/]+)/(stop|start|resume|setMachineType|attachDisk)$", self._instance_action),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/instances/([^/]+)/(getIamPolicy|setIamPolicy)$", self._instance_policy),
            (r"^/compute/v1/projects/[^/]+/aggregated/instances$", self._aggregated),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/disks$", self._disks),
            (r"^/compute/v1/projects/[^/]+/zones/([^/]+)/disks/([^/]+)$", self._disk),
            (r"^/compute/v1/projects/[^/]+/global/(networks|firewalls|addresses|healthChecks|backendServices|urlMaps|targetHttpProxies|targetHttpsProxies|forwardingRules|sslCertificates)$",
             self._global_collection),
            (r"^/compute/v1/projects/[^/]+/global/(networks|firewalls|addresses|healthChecks|backendServices|urlMaps|targetHttpProxies|targetHttpsProxies|forwardingRules|sslCertificates)/([^/]+)$",
             self._global_item),
            (r"^/compute/v1/projects/[^/]+/regions/([^/]+)/subnetworks$", self._subnets),
            (r"^/compute/v1/projects/[^/]+/regions/([^/]+)/subnetworks/([^/]+)$", self._subnet),
            (r"^/compute/v1/projects/[^/]+/regions/([^/]+)$", self._region),
            (r"^/compute/v1/projects/[^/]+/global/snapshots$", self._snapshots),
            (r"^/compute/v1/projects/[^/]+/global/snapshots/([^/]+)$", self._snapshot),
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
        assert all(item["email"] in self.service_accounts for item in body.get("serviceAccounts", [])), "a machine runs as an account that exists"
        if body["disks"][0].get("diskEncryptionKey"):
            self.may_use_key(body["disks"][0]["diskEncryptionKey"]["kmsKeyName"], "service-123456789@compute-system.iam.gserviceaccount.com")
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

    # ---- service accounts, and the project's and repositories' policies
    def _service_accounts(self, found, method, body, params):
        email = f"{body['accountId']}@{self.project}.iam.gserviceaccount.com"
        assert 6 <= len(body["accountId"]) <= 30, "IAM takes an account id of 6 to 30 characters"
        if email in self.service_accounts:
            raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, "Google Cloud refused the request: already exists", {"http_status": 409})
        self.service_accounts[email] = {"email": email, **body["serviceAccount"]}
        return self.service_accounts[email]

    def _service_account(self, found, method, body, params):
        if found.group(1) not in self.service_accounts:
            raise self._not_found(found.group(1))
        if method == "DELETE":
            return self.service_accounts.pop(found.group(1)) and {}
        return self.service_accounts[found.group(1)]

    def _account_policy(self, found, method, body, params):
        if found.group(1) not in self.service_accounts:
            raise self._not_found(found.group(1))
        writing = found.group(2) == "setIamPolicy"
        return self._policy(("account", found.group(1)), writing, body.get("policy"))

    # ---- Cloud Run
    def _run_services_collection(self, found, method, body, params):
        name = params["serviceId"]
        if name in self.run_services:
            raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, "Google Cloud refused the request: already exists", {"http_status": 409})
        account = body["template"].get("serviceAccount")
        assert account in self.service_accounts, "a service runs as an account that exists"
        revision = f"{name}-00001-abc"
        self.run_services[name] = {**body, "name": f"projects/{self.project}/locations/{found.group(1)}/services/{name}", "uri": f"https://{name}-xyz.a.run.app",
                                   "reconciling": False, "terminalCondition": {"type": "Ready", "state": "CONDITION_SUCCEEDED"}, "createTime": "2026-10-29T10:00:00Z",
                                   "latestReadyRevision": f"projects/{self.project}/locations/{found.group(1)}/services/{name}/revisions/{revision}",
                                   "trafficStatuses": [{"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST", "percent": 100, "revision": revision}]}
        return {"name": f"operations/run-{self.counter}", "done": False}

    def _run_service(self, found, method, body, params):
        name = found.group(1)
        if name not in self.run_services:
            raise self._not_found(name)
        if method == "DELETE":
            self.order.append(("run DELETE", name))
            del self.run_services[name]
            return {"name": "operations/run-delete", "done": False}
        return copy.deepcopy(self.run_services[name])

    def _run_policy(self, found, method, body, params):
        if found.group(1) not in self.run_services:
            raise self._not_found(found.group(1))
        writing = found.group(2) == "setIamPolicy"
        return self._policy(("run", found.group(1)), writing, body.get("policy"))

    def _run_jobs_collection(self, found, method, body, params):
        name = params["jobId"]
        if name in self.run_jobs:
            raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, "Google Cloud refused the request: already exists", {"http_status": 409})
        assert body["template"]["template"]["serviceAccount"] in self.service_accounts, "a job runs as an account that exists"
        self.run_jobs[name] = {**body, "name": f"projects/{self.project}/locations/{found.group(1)}/jobs/{name}", "reconciling": False,
                               "terminalCondition": {"type": "Ready", "state": "CONDITION_SUCCEEDED"}, "createTime": "2026-10-29T10:00:00Z"}
        return {"name": f"operations/job-{self.counter}", "done": False}

    def _run_job(self, found, method, body, params):
        name = found.group(1)
        if name not in self.run_jobs:
            raise self._not_found(name)
        if method == "DELETE":
            assert not [item for item in self.schedules.values() if name in item["httpTarget"]["uri"]], "the schedule goes before the job"
            del self.run_jobs[name]
            return {"name": "operations/job-delete", "done": False}
        return copy.deepcopy(self.run_jobs[name])

    def _run_job_policy(self, found, method, body, params):
        if found.group(1) not in self.run_jobs:
            raise self._not_found(found.group(1))
        writing = found.group(2) == "setIamPolicy"
        return self._policy(("job", found.group(1)), writing, body.get("policy"))

    def _schedules_collection(self, found, method, body, params):
        assert body["httpTarget"]["oauthToken"]["serviceAccountEmail"] in self.service_accounts, "the schedule runs as an account that exists"
        if body["name"] in self.schedules:
            raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, "Google Cloud refused the request: already exists", {"http_status": 409})
        self.schedules[body["name"]] = dict(body)
        return self.schedules[body["name"]]

    def _schedule(self, found, method, body, params):
        if found.group(1) not in self.schedules:
            raise self._not_found(found.group(1))
        if method == "DELETE":
            return self.schedules.pop(found.group(1)) and {}
        return self.schedules[found.group(1)]

    # ---- BigQuery and Firestore
    def _datasets(self, found, method, body, params):
        dataset = body["datasetReference"]["datasetId"]
        assert all(char.isalnum() or char == "_" for char in dataset), "a dataset id holds letters, digits and underscores"
        if dataset in self.datasets:
            raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, "Google Cloud refused the request: Already Exists", {"http_status": 409})
        self.datasets[dataset] = {**body, "tables": 0, "access": [{"role": "OWNER", "userByEmail": "cloudinfra@test-project.iam.gserviceaccount.com"}]}
        return self.datasets[dataset]

    def _dataset(self, found, method, body, params):
        dataset = found.group(1)
        if dataset not in self.datasets:
            raise self._not_found(dataset)
        if method == "DELETE":
            if self.datasets[dataset]["tables"]:
                raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Google Cloud refused the request: Dataset {dataset} is still in use", {"http_status": 400})
            del self.datasets[dataset]
            return {}
        if method == "PATCH":
            self.datasets[dataset].update(copy.deepcopy(body))
        return copy.deepcopy(self.datasets[dataset])

    def _firestore_collection(self, found, method, body, params):
        database = params["databaseId"]
        assert 4 <= len(database) <= 63 and body["type"] == "FIRESTORE_NATIVE", "a native-mode database with an id of 4 to 63 characters"
        self.firestore[database] = dict(body)
        return {"name": f"projects/{self.project}/databases/{database}/operations/create", "done": True}

    def _firestore_item(self, found, method, body, params):
        database = found.group(1)
        if database not in self.firestore:
            raise self._not_found(database)
        if method == "DELETE":
            del self.firestore[database]
            return {"name": f"projects/{self.project}/databases/{database}/operations/delete", "done": True}
        return self.firestore[database]

    # ---- Cloud Monitoring
    def _monitoring_collection(self, found, method, body, params):
        kind = found.group(1)
        if kind == "alertPolicies":
            for channel in body["notificationChannels"]:
                assert channel in self.monitoring, "an alert names channels that exist"
            check = re.search(r'check_id="([^"]+)"', body["conditions"][0]["conditionThreshold"]["filter"]).group(1)
            assert f"projects/{self.project}/uptimeCheckConfigs/{check}" in self.monitoring, "an alert watches a check that exists"
        self.counter += 1
        name = f"projects/{self.project}/{kind}/{kind[:5].lower()}{self.counter}"
        self.monitoring[name] = {**body, "name": name}
        return self.monitoring[name]

    def _monitoring_item(self, found, method, body, params):
        name = found.group(1)
        if name not in self.monitoring:
            raise self._not_found(name)
        if method == "DELETE":
            if "/notificationChannels/" in name:
                assert not [item for item in self.monitoring.values() if name in item.get("notificationChannels", [])], "a channel goes after the alerts using it"
                assert not [item for item in self.budgets.values() if name in item["notificationsRule"]["monitoringNotificationChannels"]], "and after the budgets"
            del self.monitoring[name]
            return {}
        return self.monitoring[name]

    # ---- Cloud KMS
    def _key_rings(self, found, method, body, params):
        name = f"{found.group(0)[4:]}/{params['keyRingId']}"
        if name in self.key_rings:
            raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, "Google Cloud refused the request: KeyRing already exists", {"http_status": 409})
        self.key_rings.add(name)
        return {"name": name}

    def _crypto_keys(self, found, method, body, params):
        assert found.group(1) in self.key_rings, "a key goes in a ring that exists"
        name = f"{found.group(1)}/cryptoKeys/{params['cryptoKeyId']}"
        version = {"name": f"{name}/cryptoKeyVersions/1", "state": "ENABLED"}
        self.crypto_keys[name] = {**body, "name": name, "primary": version, "versions": [version]}
        return self.crypto_keys[name]

    def _crypto_key(self, found, method, body, params):
        name = found.group(1)
        if name not in self.crypto_keys:
            raise self._not_found(name)
        if found.group(2):
            return self._policy(("key", name), found.group(2) == "setIamPolicy", (body or {}).get("policy"))
        return {key: value for key, value in self.crypto_keys[name].items() if key != "versions"}

    def _key_versions(self, found, method, body, params):
        return {"cryptoKeyVersions": copy.deepcopy(self.crypto_keys[found.group(1)]["versions"])}

    def _destroy_version(self, found, method, body, params):
        key = found.group(1).rsplit("/cryptoKeyVersions/", 1)[0]
        version = next(item for item in self.crypto_keys[key]["versions"] if item["name"] == found.group(1))
        version["state"] = "DESTROY_SCHEDULED"
        return version

    def may_use_key(self, key: str, agent: str) -> None:
        """What Google checks before it encrypts with a customer's key: its service agent holds the role on that key."""
        assert key in self.crypto_keys, f"{key} does not exist"
        assert f"serviceAccount:{agent}" in self.members(("key", key), "roles/cloudkms.cryptoKeyEncrypterDecrypter"), f"{agent} may not use {key}"

    # ---- Cloud Logging sinks
    def _sinks(self, found, method, body, params):
        assert params.get("uniqueWriterIdentity") == "true", "a sink writes as an identity of its own"
        name = f"projects/{self.project}/sinks/{body['name']}"
        self.sinks[name] = {**body, "writerIdentity": "serviceAccount:service-123456789@gcp-sa-logging.iam.gserviceaccount.com"}
        return self.sinks[name]

    def _sink(self, found, method, body, params):
        name = found.group(1)
        if name not in self.sinks:
            raise self._not_found(name)
        if method == "DELETE":
            del self.sinks[name]
            return {}
        return self.sinks[name]

    # ---- Cloud Billing budgets
    def _budgets(self, found, method, body, params):
        assert len(body["notificationsRule"]["monitoringNotificationChannels"]) <= 5, "a budget takes at most five channels"
        [values] = body["budgetFilter"]["labels"].values()
        assert len(body["budgetFilter"]["labels"]) == 1 and len(values) == 1, "a budget filters on one label with one value"
        name = f"{found.group(1)}/budgets/b{len(self.budgets) + 1}"
        self.budgets[name] = {**body, "name": name}
        return self.budgets[name]

    def _budget(self, found, method, body, params):
        name = found.group(1)
        if name not in self.budgets:
            raise self._not_found(name)
        if method == "DELETE":
            del self.budgets[name]
            return {}
        return self.budgets[name]

    # ---- Cloud DNS
    def _zones_collection(self, found, method, body, params):
        if body["name"] in self.zones:
            raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, "Google Cloud refused the request: already exists", {"http_status": 409})
        assert body["dnsName"].endswith("."), "a zone's dnsName ends with a dot"
        servers = [f"ns-cloud-a{n}.googledomains.com." for n in range(1, 5)]
        self.zones[body["name"]] = {**body, "nameServers": servers, "rrsets": [{"name": body["dnsName"], "type": "NS", "ttl": 21600, "rrdatas": servers},
                                                                                {"name": body["dnsName"], "type": "SOA", "ttl": 21600, "rrdatas": ["soa"]}]}
        return {key: value for key, value in self.zones[body["name"]].items() if key != "rrsets"}

    def _zone(self, found, method, body, params):
        name, part = found.group(1), found.group(2)
        if name not in self.zones:
            raise self._not_found(name)
        zone = self.zones[name]
        if part == "/rrsets":
            return {"rrsets": copy.deepcopy(zone["rrsets"])}
        if part == "/changes":
            for item in body.get("deletions", []):
                assert item in zone["rrsets"], "a deletion names a record set as it is"
                zone["rrsets"].remove(item)
            for item in body.get("additions", []):
                assert item["name"].endswith(zone["dnsName"]) and item["name"].endswith("."), "a record set is in its zone, by full name"
                assert not [have for have in zone["rrsets"] if (have["name"], have["type"]) == (item["name"], item["type"])], "one record set per name and type"
                zone["rrsets"].append(dict(item))
            return {"status": "done"}
        if method == "DELETE":
            if [item for item in zone["rrsets"] if item["type"] not in ("NS", "SOA")]:
                raise InfrastructureError(ErrorCode.PROVIDER_ERROR, "Google Cloud refused the request: The resource named in the request is not empty", {"http_status": 400})
            del self.zones[name]
            return {}
        return {key: value for key, value in zone.items() if key != "rrsets"}

    def _zone_policy(self, found, method, body, params):
        if found.group(1) not in self.zones:
            raise self._not_found(found.group(1))
        return self._policy(("zone", found.group(1)), found.group(2) == "setIamPolicy", body.get("policy"))

    def _known(self, policy):
        for binding in policy.get("bindings", []):
            for member in binding.get("members", []):
                if member.startswith("serviceAccount:") and member.endswith(".iam.gserviceaccount.com") and member[15:].startswith("node-"):
                    if member[15:] not in self.service_accounts or self.unknown_for > 0:
                        self.unknown_for = max(0, self.unknown_for - 1)
                        raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Google Cloud refused the request: Service account {member[15:]} does not exist.",
                                                  {"http_status": 400})

    def _project_policy(self, found, method, body, params):
        writing = found.group(2) == "setIamPolicy"
        if writing:
            self._known(body["policy"])
        return self._policy(("project",), writing, body.get("policy"))

    def _repository_policy(self, found, method, body, params):
        registry = f"{found.group(2)}-docker.pkg.dev/{found.group(1)}/{found.group(3)}"
        if registry not in self.repositories:
            raise self._not_found(registry)
        writing = found.group(4) == "setIamPolicy"
        if writing:
            self._known(body["policy"])
        return self._policy(("repository", registry), writing, body.get("policy"))

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
        if body.get("diskEncryptionKey"):
            self.may_use_key(body["diskEncryptionKey"]["kmsKeyName"], "service-123456789@compute-system.iam.gserviceaccount.com")
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
        if method == "GET":  # a list, by IPAddress when filtered so
            wanted = re.fullmatch(r'IPAddress="([^"]+)"', params.get("filter", "")) if params.get("filter") else None
            return {"items": [item for item in store.values() if wanted is None or item.get("IPAddress") == wanted.group(1)]}
        if body["name"] in store:
            raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, "Google Cloud refused the request: already exists", {"http_status": 409})
        if found.group(1) == "sslCertificates":
            body = {**body, "managed": {**body["managed"], "status": "PROVISIONING"}}
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
            used = [disk for disk in self.disks.values() if any(link.endswith(f"/resourcePolicies/{name}") for link in disk.get("resourcePolicies", []))]
            if found.group(2) == "resourcePolicies" and used:
                raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Google Cloud refused the request: The resource_policy resource '{name}' is already being used",
                                          {"http_status": 400})
            del store[name]
            return self._op()
        return store[name]

    def take_snapshot(self, disk: str, policy: str):
        """What the schedule does each day: a snapshot carrying the schedule's labels."""
        labels = self.regional["resourcePolicies"][policy]["snapshotSchedulePolicy"]["snapshotProperties"]["labels"]
        name = f"{disk}-snap-{len(self.snapshots) + 1}"
        self.snapshots[name] = {"name": name, "labels": dict(labels)}
        return name

    def _snapshots(self, found, method, body, params):
        wanted = dict(re.findall(r'labels\.([a-z0-9_-]+)="([^"]*)"', params.get("filter", "")))
        return {"items": [item for item in self.snapshots.values() if all(item["labels"].get(key) == value for key, value in wanted.items())]}

    def _snapshot(self, found, method, body, params):
        if found.group(1) not in self.snapshots:
            raise self._not_found(found.group(1))
        del self.snapshots[found.group(1)]
        return self._op()

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
        account = cluster["nodePools"][0]["config"].get("serviceAccount")
        assert account is None or account in self.service_accounts, "a node pool runs as an account that exists"
        if account and self.account_unknown_to_gke_for > 0:
            self.account_unknown_to_gke_for -= 1
            raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f'Google Cloud refused the request: Failed precondition: failed to check status for "{account}"; '
                                      "Verify if principal exists and is valid.", {"http_status": 400})
        for field in ("network", "subnetwork"):
            if "/" in (cluster.get(field) or ""):  # as GKE does: these fields take a name only
                raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f'Google Cloud refused the request: Malformed value for {field} field: "{cluster[field]}".',
                                          {"http_status": 400})
        endpoint = f"35.201.0.{7 + len(self.clusters)}"  # each cluster its own address
        self.clusters[(found.group(1), cluster["name"])] = {**cluster, "status": "RUNNING", "endpoint": endpoint, "currentMasterVersion": "1.31.1-gke.1", "createTime": "2026-10-28T10:00:00Z",
                                                            "masterAuth": {"clusterCaCertificate": "RkFLRSBDQQ=="},
                                                            "currentNodeCount": cluster["nodePools"][0]["initialNodeCount"]}
        return {"name": f"operation-{self.counter}", "status": "DONE", "selfLink": f"https://container.googleapis.com/v1/projects/{self.project}/locations/{found.group(1)}/operations/op"}

    def _cluster(self, found, method, body, params):
        key = (found.group(1), found.group(2))
        if key not in self.clusters:
            raise self._not_found(key[1])
        if method == "DELETE":
            self.order.append(("cluster DELETE", key[1]))
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
        if body.get("diskEncryptionConfiguration"):
            self.may_use_key(body["diskEncryptionConfiguration"]["kmsKeyName"], "service-123456789@gcp-sa-cloud-sql.iam.gserviceaccount.com")
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
        if body.get("encryption"):
            self.may_use_key(body["encryption"]["defaultKmsKeyName"], "service-123456789@gs-project-accounts.iam.gserviceaccount.com")
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
        self.fake.namespace_labels.setdefault((self.endpoint, name), dict(labels))

    def put(self, collection, body):
        self._maybe_fail()
        if "gateway.networking.k8s.io" in collection and self.fake.gateway_kinds_missing > 0:
            self.fake.gateway_kinds_missing -= 1
            raise InfrastructureError(ErrorCode.NOT_FOUND, "The cluster's API answered 404", {"http_status": 404})
        self.fake.cluster_objects[(self.endpoint, f"{collection}/{body['metadata']['name']}")] = body
        if body["kind"] == "Gateway":  # GKE's controller makes the load balancer: a forwarding rule per listener on the named address
            address = self.fake.addresses[body["spec"]["addresses"][0]["value"]]["address"]
            for listener in body["spec"]["listeners"]:
                name = f"gkegw-{listener['name']}-{address}"
                self.fake.forwardingRules[name] = {"name": name, "IPAddress": address, "portRange": str(listener["port"]), "gateway": True}

    def delete(self, path):
        self._maybe_fail()
        self.fake.order.append(("kubernetes DELETE", path))
        if self.fake.cluster_objects.pop((self.endpoint, path), None) and path.endswith("/gateways/public"):
            self.fake.forwardingRules = {name: rule for name, rule in self.fake.forwardingRules.items() if not rule.get("gateway")}  # as GKE's controller does

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
