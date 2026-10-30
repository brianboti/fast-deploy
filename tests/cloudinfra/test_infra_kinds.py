"""The first batch of typed kinds: compute groups, accelerators, cache, queue, secret, cluster, load balancer; the kinds listing; unknown kinds."""
import base64
import json

import pydantic
import pytest
from gcp_fakes import EVERYTHING_REQUEST, VM_REQUEST, make_config, make_orchestrator, make_provider, principal

from cloudinfra.models import InfrastructureRequest, RequestStatus, ResourceKind, ResourceStatus
from cloudinfra.models.errors import ErrorCode, InfrastructureError, problems_from
from cloudinfra.orchestration import planner
from cloudinfra.providers.gcp.provider import GCPProvider


# ---- the contract
def test_groups_accelerators_and_unknown_kinds():
    request = InfrastructureRequest.model_validate(EVERYTHING_REQUEST)
    assert request.kinds() == ["network", "storage", "compute", "database", "cache", "queue", "secret", "cluster", "load_balancer"]
    assert [group.group_name for group in request.compute_groups()] == ["web", "gpu"] and request.group("gpu").accelerator.type == "l4" and request.group(None) is None
    assert [queue.name for queue in request.queues()] == ["events", "jobs"] and [secret.generate_value for secret in request.secrets()] == [True, False]
    one = InfrastructureRequest.model_validate(VM_REQUEST)
    assert one.compute_groups()[0].group_name == "vm" and one.group(None) is one.compute_groups()[0]
    for bad, code, text in [
        ({"region": "us-central1", "loadbalancer": {}}, ErrorCode.UNSUPPORTED_RESOURCE_KIND, "did you mean load_balancer?"),
        ({"region": "us-central1", "vms": {"cpu": 2}}, ErrorCode.UNSUPPORTED_RESOURCE_KIND, "did you mean compute?"),
        ({"region": "us-central1", "banana": {}}, ErrorCode.UNSUPPORTED_RESOURCE_KIND, "kinds: identity, network, storage, compute"),
        ({"region": "us-central1", "compute": [{"cpu": 2, "memory_gb": 8}, {"cpu": 2, "memory_gb": 8}]}, ErrorCode.INVALID_REQUEST, "every group of a list needs a group name"),
        ({"region": "us-central1", "compute": [{"group": "a", "cpu": 2, "memory_gb": 8}, {"group": "a", "cpu": 2, "memory_gb": 8}]}, ErrorCode.INVALID_REQUEST, "unique"),
        ({"region": "us-central1", "compute": []}, ErrorCode.INVALID_REQUEST, "cannot be empty"),
        ({"region": "us-central1", "load_balancer": {}}, ErrorCode.INVALID_REQUEST, "needs a compute group"),
        ({"region": "us-central1", "compute": [{"group": "a", "cpu": 2, "memory_gb": 8}, {"group": "b", "cpu": 2, "memory_gb": 8}], "load_balancer": {}},
         ErrorCode.INVALID_REQUEST, "must name the group"),
        ({"region": "us-central1", "compute": {"cpu": 2, "memory_gb": 8}, "load_balancer": {"backend_group": "x"}}, ErrorCode.INVALID_REQUEST, "not a compute group"),
        ({"region": "us-central1", "compute": {"cpu": 2, "memory_gb": 8}, "load_balancer": {"backends": ["cmp-0123456789ab"]}}, ErrorCode.INVALID_REQUEST, "one or the other"),
        ({"region": "us-central1", "compute": {"cpu": 2, "memory_gb": 8}, "storage": {"group": "x"}}, ErrorCode.INVALID_REQUEST, "not a compute group"),
        ({"region": "us-central1", "queue": [{"name": "a"}, {"name": "a"}]}, ErrorCode.INVALID_REQUEST, "names must be unique"),
        ({"region": "us-central1", "secret": [{"name": f"s{i}"} for i in range(17)]}, ErrorCode.INVALID_REQUEST, "at most 16"),
        ({"region": "us-central1", "compute": {"cpu": 2, "memory_gb": 8, "accelerator": {"type": "v100"}}}, ErrorCode.INVALID_REQUEST, "accelerator.type"),
        ({"region": "us-central1", "cache": {"engine": "memcached"}}, ErrorCode.INVALID_REQUEST, "cache.engine"),
        ({"region": "us-central1", "cluster": {"kubernetes_version": "latest"}}, ErrorCode.INVALID_REQUEST, "kubernetes_version"),
    ]:
        with pytest.raises(pydantic.ValidationError) as caught:
            InfrastructureRequest.model_validate(bad)
        problems = problems_from(caught.value)
        assert problems and problems[0].code == code and text in problems[0].message, (bad, [item.message for item in problems])


def test_union_branches_do_not_double_report():
    with pytest.raises(pydantic.ValidationError) as caught:
        InfrastructureRequest.model_validate({"region": "us-central1", "compute": {"cpu": 0, "memory_gb": 16}})
    problems = problems_from(caught.value)
    assert len(problems) == 1 and problems[0].message.startswith("compute.cpu:")


# ---- validation and planning
def test_validator_rules_for_the_new_kinds():
    config = make_config(limits={"max_accelerators_per_instance": 2, "max_cache_memory_gb": 4, "max_cluster_nodes": 3})
    provider, _ = make_provider(config)
    from cloudinfra.orchestration import validator

    def problems(data):
        return validator.validate(InfrastructureRequest.model_validate(data), config, provider)
    assert problems(EVERYTHING_REQUEST) == []
    found = problems({"region": "us-central1", "compute": {"cpu": 48, "memory_gb": 192, "accelerator": {"type": "l4", "count": 4}}})
    assert [item.code for item in found] == [ErrorCode.LIMIT_EXCEEDED, ErrorCode.LIMIT_EXCEEDED, ErrorCode.LIMIT_EXCEEDED] and "accelerators" in found[-1].message
    assert [item.code for item in problems({"region": "us-central1", "cache": {"memory_gb": 8}})] == [ErrorCode.LIMIT_EXCEEDED, ErrorCode.INVALID_CACHE_CONFIGURATION]
    assert problems({"region": "us-central1", "cache": {"network_id": "net-0123456789ab"}}) == []
    cluster = problems({"region": "us-central1", "cluster": {"node_count": 5, "node_cpu": 3, "node_memory_gb": 9}})
    assert [item.code for item in cluster] == [ErrorCode.INVALID_CLUSTER_CONFIGURATION, ErrorCode.LIMIT_EXCEEDED]
    assert [item.code for item in problems({"region": "us-central1", "cache": {"version": "5.0"}, "network": {}})] == [ErrorCode.INVALID_CACHE_CONFIGURATION]
    none = make_config(limits={"max_accelerators_per_instance": 0})
    found = validator.validate(InfrastructureRequest.model_validate({"region": "us-central1", "compute": {"cpu": 4, "memory_gb": 16, "accelerator": {"type": "t4"}}}), none, provider)
    assert found[0].message == "Accelerators are not allowed by this service"


def test_adapter_without_a_kind_is_refused_before_any_call():
    config = make_config()
    provider, fake = make_provider(config)

    class Narrow(GCPProvider):
        supported_kinds = ("compute", "network")
    narrow = Narrow(config.providers.gcp, api=fake)
    from cloudinfra.orchestration import validator
    found = validator.validate(InfrastructureRequest.model_validate({"region": "us-central1", "cluster": {}}), config, narrow)
    assert found[0].code == ErrorCode.UNSUPPORTED_RESOURCE_KIND and "does not provide cluster" in found[0].message and fake.calls == []


def test_plan_for_everything_names_shapes_and_order():
    config = make_config()
    provider, _ = make_provider(config)
    request = InfrastructureRequest.model_validate(EVERYTHING_REQUEST)
    ctx = planner.context(request, "abcdef" + "0" * 26, "app-layer", config, provider)
    planned = planner.plan(request, ctx, provider)
    assert [(item.kind, item.name) for item in planned] == [
        ("network", "platform-net"), ("storage", "platform-gpu-disk-1"), ("compute", "platform-web-1"), ("compute", "platform-web-2"), ("compute", "platform-gpu-1"),
        ("database", "platform-db-abcdef"), ("cache", "platform-cache"), ("queue", "platform-events"), ("queue", "platform-jobs"), ("secret", "platform-api-key"),
        ("secret", "platform-token"), ("secret", "platform-db-password-abcdef"), ("secret", "platform-cache-auth-abcdef"), ("cluster", "platform-cluster"),
        ("load_balancer", "platform-lb")]
    by_name = {item.name: item for item in planned}
    assert by_name["platform-db-password-abcdef"].spec == {**by_name["platform-db-password-abcdef"].spec, "secret": "db-password", "versions": 1, "purpose": "database_password"}
    assert by_name["platform-cache-auth-abcdef"].spec["purpose"] == "cache_auth"
    assert by_name["platform-gpu-1"].spec["machine_type"] == "g2-standard-8" and by_name["platform-gpu-1"].spec["accelerator"] == {"type": "nvidia-l4", "count": 1}
    assert by_name["platform-gpu-disk-1"].spec["attach_to"] == "platform-gpu-1" and by_name["platform-web-1"].spec["network"] == "platform-net"
    assert by_name["platform-cache"].spec == {**by_name["platform-cache"].spec, "tier": "BASIC", "redis_version": "REDIS_7_2", "network": "platform-net"}
    assert by_name["platform-jobs"].provider_mapping == "pubsub.topics.create" and by_name["platform-api-key"].spec["versions"] == 1
    assert by_name["platform-cluster"].spec["machine_type"] == "e2-standard-2" and by_name["platform-cluster"].spec["node_count"] == 2
    assert by_name["platform-lb"].spec == {**by_name["platform-lb"].spec, "type": "http", "port": 8080, "backend_group": "web", "backends": ["platform-web-1", "platform-web-2"]}
    estimate = provider.estimate_cost(planned)
    kinds = {line.kind for line in estimate.lines}
    assert kinds == {"network", "storage", "compute", "database", "cache", "queue", "secret", "cluster", "load_balancer"} and estimate.monthly_usd > 500
    gpu = next(line for line in estimate.lines if line.resource == "platform-gpu-1")
    assert "nvidia-l4" in gpu.basis and gpu.monthly_usd > 400


# ---- the adapters, against the fake
def test_new_adapters_send_the_right_bodies_and_tear_down():
    config = make_config()
    provider, fake = make_provider(config)
    request = InfrastructureRequest.model_validate(EVERYTHING_REQUEST)
    ctx = planner.context(request, "a" * 32, "app-layer", config, provider)
    planned = {item.kind + ":" + item.name: item for item in planner.plan(request, ctx, provider)}
    network = provider.create_network(request.network, planned["network:platform-net"], ctx)
    gpu = provider.create_compute(request.group("gpu"), planned["compute:platform-gpu-1"], ctx, network)
    body = next(call[2] for call in fake.calls if call[0] == "POST" and call[1].endswith("/instances") and call[2].get("name") == "platform-gpu-1")
    assert body["machineType"].endswith("/g2-standard-8") and body["guestAccelerators"] == [{"acceleratorType": "zones/us-central1-a/acceleratorTypes/nvidia-l4", "acceleratorCount": 1}]
    assert body["scheduling"]["onHostMaintenance"] == "TERMINATE" and gpu.accelerator == "nvidia-l4 x1" and gpu.group == "gpu" and gpu.cpu == 8 and gpu.memory_gb == 32
    assert body["shieldedInstanceConfig"] == {"enableSecureBoot": False, "enableVtpm": True, "enableIntegrityMonitoring": True}, "drivers need Secure Boot off"
    cache = provider.create_cache(request.cache, planned["cache:platform-cache"], ctx, network)
    cache_body = next(call[2] for call in fake.calls if "redis.googleapis.com" in call[1] and call[0] == "POST")
    assert cache_body == {**cache_body, "tier": "BASIC", "memorySizeGb": 2, "redisVersion": "REDIS_7_2", "authEnabled": True, "transitEncryptionMode": "SERVER_AUTHENTICATION",
                          "connectMode": "DIRECT_PEERING", "authorizedNetwork": network.provider_id}
    assert cache.host == "10.30.0.3" and cache.port == 6379 and cache.status == ResourceStatus.READY and cache.network == network.id and "auth" not in cache_body
    with pytest.raises(InfrastructureError) as caught:
        provider.create_cache(request.cache, planned["cache:platform-cache"], ctx, None)
    assert caught.value.code == ErrorCode.INVALID_CACHE_CONFIGURATION
    events = provider.create_queue(request.queues()[0], planned["queue:platform-events"], ctx)
    jobs = provider.create_queue(request.queues()[1], planned["queue:platform-jobs"], ctx)
    assert events.topic == "projects/test-project/topics/platform-events" and events.subscription == "projects/test-project/subscriptions/platform-events" and jobs.subscription is None
    assert fake.subscriptions["platform-events"]["messageRetentionDuration"] == f"{168 * 3600}s" and fake.subscriptions["platform-events"]["expirationPolicy"] == {}
    key = provider.create_secret(request.secrets()[0], planned["secret:platform-api-key"], ctx)
    token = provider.create_secret(request.secrets()[1], planned["secret:platform-token"], ctx)
    assert key.versions == 1 and token.versions == 0 and len(fake.secret_versions["platform-api-key"]) == 1 and "platform-token" not in fake.secret_versions
    assert "versions" not in json.dumps([call[1] for call in fake.calls if "secretmanager" in call[1] and "addVersion" not in call[1]])
    cluster = provider.create_cluster(request.cluster, planned["cluster:platform-cluster"], ctx, network)
    cluster_body = next(call[2]["cluster"] for call in fake.calls if "container.googleapis.com" in call[1] and call[0] == "POST")
    assert cluster_body["nodePools"][0]["initialNodeCount"] == 2 and cluster_body["nodePools"][0]["config"]["machineType"] == "e2-standard-2"
    assert cluster_body["workloadIdentityConfig"] == {"workloadPool": "test-project.svc.id.goog"} and cluster_body["releaseChannel"] == {"channel": "REGULAR"}
    # GKE takes names only, and refuses a self link as malformed (found on a real project on 2026-10-29)
    assert cluster_body["network"] == "platform-net" and cluster_body["subnetwork"] == network.subnet.rsplit("/", 1)[-1] and "privateClusterConfig" not in cluster_body
    assert "cloud-platform" not in str(cluster_body["nodePools"][0]["config"]["oauthScopes"])
    assert cluster.endpoint == "35.201.0.7" and cluster.version == "1.31.1-gke.1" and cluster.status == ResourceStatus.READY
    web = [provider.create_compute(request.group("web"), planned[f"compute:platform-web-{index}"], ctx, network) for index in (1, 2)]
    balancer = provider.create_load_balancer(request.load_balancer, planned["load_balancer:platform-lb"], ctx, web, network)
    assert balancer.address == "34.120.0.1" and balancer.port == 8080 and balancer.backends == [machine.id for machine in web] and balancer.health_check_path == "/"
    assert [piece["kind"] for piece in balancer.components] == ["instance_group", "firewall", "health_check", "backend_service", "url_map", "target_proxy", "address", "forwarding_rule"]
    assert fake.instance_groups[("us-central1-a", "platform-lb-group")]["members"] == [machine.provider_id for machine in web]
    assert fake.firewalls["platform-lb-allow-health"]["sourceRanges"] == ["130.211.0.0/22", "35.191.0.0/16"] and fake.firewalls["platform-lb-allow-health"]["allowed"][0]["ports"] == ["8080"]
    assert fake.backendServices["platform-lb-backend"]["loadBalancingScheme"] == "EXTERNAL_MANAGED" and fake.forwardingRules["platform-lb-rule"]["portRange"] == "8080"
    assert fake.healthChecks["platform-lb-health"]["httpHealthCheck"] == {"port": 8080, "requestPath": "/"}
    provider.delete_load_balancer(balancer, ctx)
    assert not fake.forwardingRules and not fake.targetHttpProxies and not fake.urlMaps and not fake.backendServices and not fake.healthChecks and not fake.instance_groups
    assert "platform-lb-allow-health" not in fake.firewalls and "platform-lb-address" not in fake.addresses and provider.get_load_balancer(balancer).status == ResourceStatus.DELETED
    for resource in (cluster, cache, events, jobs, key, token):
        provider.delete(resource, ctx)
        assert provider.refresh(resource).status == ResourceStatus.DELETED
    assert not fake.clusters and not fake.redis and not fake.topics and not fake.subscriptions and not fake.secrets


def test_tcp_load_balancer_is_regional_and_passthrough():
    config = make_config()
    provider, fake = make_provider(config)
    request = InfrastructureRequest.model_validate({"region": "us-central1", "name": "db", "compute": {"count": 1, "cpu": 2, "memory_gb": 8}, "load_balancer": {"type": "tcp", "port": 5432}})
    ctx = planner.context(request, "b" * 32, "app-layer", config, provider)
    planned = planner.plan(request, ctx, provider)
    machine = provider.create_compute(request.compute, planned[0], ctx, None)
    balancer = provider.create_load_balancer(request.load_balancer, planned[1], ctx, [machine], None)
    assert balancer.address == "35.200.0.1" and balancer.health_check_path is None
    assert [piece["kind"] for piece in balancer.components] == ["instance_group", "firewall", "health_check", "backend_service", "address", "forwarding_rule"]
    assert fake.regional["forwardingRules"]["db-lb-rule"]["ports"] == ["5432"] and fake.regional["backendServices"]["db-lb-backend"]["loadBalancingScheme"] == "EXTERNAL"
    assert fake.firewalls["db-lb-allow-health"]["network"].endswith("/global/networks/default")
    assert fake.firewalls["db-lb-allow-health"]["sourceRanges"] == ["35.191.0.0/16", "209.85.204.0/22", "209.85.152.0/22"], "passthrough probes come from the 209.85 ranges too"
    with pytest.raises(InfrastructureError) as caught:
        provider.create_load_balancer(request.load_balancer, planned[1], ctx, [], None)
    assert caught.value.code == ErrorCode.INVALID_LOAD_BALANCER_CONFIGURATION


# ---- the lifecycle
def test_everything_provisions_in_order_and_destroys_in_reverse(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path, config=make_config(tmp_path, limits={"max_monthly_cost_usd": 5000.0}))
    who = principal(orchestrator.config)
    response = orchestrator.provision(InfrastructureRequest.model_validate(EVERYTHING_REQUEST), who, wait_seconds=45)
    assert response.status == RequestStatus.READY, response.error
    counts = {kind: len(getattr(response.resources, kind)) for kind in ("network", "storage", "compute", "database", "cache", "queue", "secret", "cluster", "load_balancer")}
    assert counts == {"network": 1, "storage": 1, "compute": 3, "database": 1, "cache": 1, "queue": 2, "secret": 4, "cluster": 1, "load_balancer": 1}
    endpoints = response.endpoints
    assert endpoints.load_balancer_address == response.resources.load_balancer[0].address and endpoints.load_balancer_address.startswith("34.120.0.")
    assert endpoints.cache_host == "10.30.0.3" and endpoints.cache_port == 6379 and endpoints.cluster_endpoint == "35.201.0.7"
    assert endpoints.queue_topics == ["projects/test-project/topics/platform-events", "projects/test-project/topics/platform-jobs"]
    rid = response.request_id[:6]
    assert endpoints.secret_names == ["projects/test-project/secrets/platform-api-key", "projects/test-project/secrets/platform-token",
                                      f"projects/test-project/secrets/platform-db-password-{rid}", f"projects/test-project/secrets/platform-cache-auth-{rid}"]
    assert endpoints.database_host == "10.20.0.3" and len(response.endpoints.internal_ips) == 3
    secrets_by_purpose = {secret.purpose: secret for secret in response.resources.secret}
    credentials = response.credentials
    assert credentials.database_password is None and credentials.database_password_secret == secrets_by_purpose["database_password"].id
    assert credentials.cache_auth_secret == secrets_by_purpose["cache_auth"].id
    assert [secret.name for secret in response.resources.secret if secret.purpose is None] == ["platform-api-key", "platform-token"]
    assert response.resources.database[0].password_secret == credentials.database_password_secret and response.resources.cache[0].auth_secret == credentials.cache_auth_secret
    password = fake.sql_users["platform-db-" + response.request_id[:6]][0]["password"]
    assert [base64.b64decode(value).decode() for value in fake.secret_versions[f"platform-db-password-{rid}"]] == [password]
    assert [base64.b64decode(value).decode() for value in fake.secret_versions[f"platform-cache-auth-{rid}"]] == ["fake-redis-auth-string"]
    assert password not in json.dumps(response.model_dump(mode="json")) and "fake-redis-auth-string" not in json.dumps(response.model_dump(mode="json"))
    groups = {machine.name: machine.group for machine in response.resources.compute}
    assert groups == {"platform-web-1": "web", "platform-web-2": "web", "platform-gpu-1": "gpu"}
    assert response.resources.storage[0].attached_to == next(m.id for m in response.resources.compute if m.group == "gpu")
    assert response.resources.load_balancer[0].backends == [m.id for m in response.resources.compute if m.group == "web"]
    creates = [call[1] for call in fake.calls if call[0] in ("POST", "PUT")]

    def kind_of(url):
        ends = {"/global/networks": "network", "/disks": "disk", "/instances": "instance" if "compute" in url else "sql" if "sqladmin" in url else "redis", "/secrets": "secret",
                "/clusters": "cluster", "/forwardingRules": "lb"}
        if "topics/" in url:
            return "topic"
        return next((kind for suffix, kind in ends.items() if url.endswith(suffix)), None)
    order = [kind for kind in (kind_of(url) for url in creates) if kind]
    positions = [order.index(kind) for kind in ("network", "disk", "instance", "sql", "redis", "topic", "secret", "cluster", "lb")]
    assert positions == sorted(positions), order
    fake.calls.clear()
    destroyed = orchestrator.destroy(response.request_id, who, wait_seconds=45)
    assert destroyed.status == RequestStatus.DESTROYED, destroyed.error
    deletes = [call[1] for call in fake.calls if call[0] == "DELETE"]
    first = {"lb": next(i for i, url in enumerate(deletes) if "forwardingRules" in url), "cluster": next(i for i, url in enumerate(deletes) if "container" in url),
             "secret": next(i for i, url in enumerate(deletes) if "secretmanager" in url), "topic": next(i for i, url in enumerate(deletes) if "topics/" in url),
             "redis": next(i for i, url in enumerate(deletes) if "redis" in url), "sql": next(i for i, url in enumerate(deletes) if "sqladmin" in url),
             "instance": next(i for i, url in enumerate(deletes) if "/instances/" in url and "compute" in url), "network": next(i for i, url in enumerate(deletes) if "/global/networks/" in url)}
    assert first["lb"] < first["cluster"] < first["secret"] < first["topic"] < first["redis"] < first["sql"] < first["instance"] < first["network"]
    assert not fake.instances and not fake.clusters and not fake.redis and not fake.topics and not fake.secrets and not fake.networks and not fake.forwardingRules


def test_failure_in_the_middle_cleans_up_every_new_kind(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path, config=make_config(tmp_path, limits={"max_monthly_cost_usd": 5000.0}))
    fake.fail_on.append(("POST", "container.googleapis.com"))
    response = orchestrator.provision(InfrastructureRequest.model_validate(EVERYTHING_REQUEST), principal(orchestrator.config), wait_seconds=45)
    assert response.status == RequestStatus.FAILED and "at cluster platform-cluster" in response.error.message
    cleanup = response.error.details["cleanup"]
    assert len(cleanup["deleted"]) == 13 and cleanup["remaining"] == []
    assert not fake.instances and not fake.redis and not fake.topics and not fake.secrets and not fake.networks and not fake.sql


def test_primitives_for_the_new_kinds_and_an_external_load_balancer(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    who = principal(orchestrator.config)
    network_id = orchestrator.create_network(who, region="us-central1", name="shared", wait_seconds=45).resources.network[0].id
    cache = orchestrator.create_cache(who, region="us-central1", network_id=network_id, memory_gb=2, wait_seconds=45)
    assert cache.status == RequestStatus.READY and cache.resources.cache[0].network == network_id
    queue = orchestrator.create_queue(who, region="us-central1", queue_name="events", subscription=False, wait_seconds=45)
    assert queue.resources.queue[0].subscription is None and queue.endpoints.queue_topics
    secret = orchestrator.create_secret(who, region="us-central1", secret_name="api-key", generate_value=True, wait_seconds=45)
    assert secret.resources.secret[0].versions == 1 and "value" not in json.dumps(secret.model_dump(mode="json")).lower().replace("generate_value", "")
    cluster = orchestrator.create_cluster(who, region="us-central1", network_id=network_id, node_count=1, wait_seconds=45)
    assert cluster.resources.cluster[0].network == network_id and cluster.endpoints.cluster_endpoint
    machines = orchestrator.create_compute(who, region="us-central1", cpu=2, memory_gb=8, count=2, network_id=network_id, wait_seconds=45)
    ids = [machine.id for machine in machines.resources.compute]
    balancer = orchestrator.create_load_balancer(who, region="us-central1", backends=ids, port=8000, wait_seconds=45)
    assert balancer.status == RequestStatus.READY and balancer.resources.load_balancer[0].backends == ids and balancer.endpoints.load_balancer_address
    assert balancer.resources.load_balancer[0].backend_group == "external"
    gpu = orchestrator.create_compute(who, region="us-central1", cpu=4, memory_gb=16, accelerator="t4", wait_seconds=45)
    assert gpu.resources.compute[0].machine_type == "n1-custom-4-16384" and gpu.resources.compute[0].accelerator == "nvidia-tesla-t4 x1"
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.create_load_balancer(who, region="us-central1", backends=["cmp-000000000000"])
    assert caught.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(InfrastructureError) as caught:
        orchestrator.delete_resource(network_id, who, ResourceKind.network)
    assert caught.value.code == ErrorCode.INVALID_STATE and len(caught.value.details["tenants"]) == 4
    gone = orchestrator.delete_resource(balancer.resources.load_balancer[0].id, who, ResourceKind.load_balancer, wait_seconds=45)
    assert gone.status == "DELETED" and all(fake.instances), "the machines behind a deleted load balancer stay"
    for request_id in (cache.request_id, queue.request_id, secret.request_id, cluster.request_id):
        assert orchestrator.destroy(request_id, who, wait_seconds=45).status == RequestStatus.DESTROYED


def test_plan_warnings_cover_every_new_kind(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path, config=make_config(tmp_path, limits={"max_monthly_cost_usd": 5000.0}))
    plan = orchestrator.plan(InfrastructureRequest.model_validate(EVERYTHING_REQUEST), principal(orchestrator.config))
    text = " ".join(plan.warnings)
    for expected in ("gpu: 1 l4 per machine as g2-standard-8", "AUTH string", "management fee", "public on port 8080", "shared-core", "whole internet"):
        assert expected in text, (expected, plan.warnings)
    assert fake.calls == [] and len(plan.planned) == 15 and "extended support" not in text
    assert "db-password secret" in text and "cache-auth secret" in text and "the image has no GPU driver" in text
    old = orchestrator.plan(InfrastructureRequest.model_validate({"region": "us-central1", "database": {"version": "13", "public_access": True}}), principal(orchestrator.config))
    assert any("postgresql 13 is kept by the provider only under paid extended support; 18 is its current default" in warning for warning in old.warnings), old.warnings


def test_list_resource_kinds_describes_the_contract(tmp_path):
    orchestrator, _ = make_orchestrator(tmp_path)
    found = orchestrator.resource_kinds()
    assert [item.kind for item in found.kinds] == ["identity", "network", "storage", "compute", "database", "cache", "queue", "warehouse", "nosql", "secret", "cluster",
                                                  "service", "job",
                                                  "load_balancer", "dns", "monitor", "budget"]
    assert found.providers == ["gcp"] and all(item.providers == ["gcp"] for item in found.kinds)
    compute = next(item for item in found.kinds if item.kind == "compute")
    assert compute.several_per_request and {field.name for field in compute.fields} >= {"cpu", "memory_gb", "accelerator", "group", "network_id"}
    cpu = next(field for field in compute.fields if field.name == "cpu")
    assert cpu.required and cpu.type == "integer" and next(field for field in compute.fields if field.name == "count").default == 1
    assert "UNSUPPORTED_RESOURCE_KIND" in found.note and next(item for item in found.kinds if item.kind == "cache").several_per_request is False
