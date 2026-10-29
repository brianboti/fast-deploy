"""A create Google accepted whose wait then failed: the resource is known by its provider id before the wait, so cleanup,
destroy and a restart delete it. Found on 2026-10-29, when a cluster was left running after its request was destroyed
(docs/DELIVERY-SPIKE.md section 11)."""
import pytest
from gcp_fakes import FULL_REQUEST, VM_REQUEST, make_config, make_orchestrator, principal

from cloudinfra.models import InfrastructureRequest, RequestStatus, ResourceStatus
from cloudinfra.models.errors import ErrorCode, InfrastructureError

LB_REQUEST = {"provider": "gcp", "region": "us-central1", "compute": {"group": "web", "count": 2, "cpu": 2, "memory_gb": 8}, "load_balancer": {"backend_group": "web"}}
LB_PARTS = ("instance_groups", "firewalls", "healthChecks", "backendServices", "urlMaps", "targetHttpProxies", "addresses", "forwardingRules")


class Died(BaseException):
    """The process ending in the middle of a wait, as a restart does."""


def fail_wait_after(fake, wait, fragment, error=None, gone=False):
    """The next `wait` after a POST to a URL containing `fragment` fails: Google took the create, the wait for it did not
    finish (an expired token, a timeout, a 5xx). With `gone`, Google never made it, as when the operation itself failed."""
    post, original, armed = fake.post, getattr(fake, wait), []

    def posting(url, json=None, **params):
        answer = post(url, json=json, **params)
        if fragment in url and not armed:
            armed.append(url)
        return answer

    def waiting(operation, *args, **kwargs):
        if armed and armed[0] is not None:
            armed[0] = None
            if gone:
                for store in (fake.clusters, fake.instances, fake.disks, fake.redis, fake.sql):
                    store.clear()
            raise error or InfrastructureError(ErrorCode.PROVIDER_AUTH_FAILED, "Google Cloud refused the service's credential")
        return original(operation, *args, **kwargs)
    fake.post = posting
    setattr(fake, wait, waiting)


def provision(orchestrator, request):
    return orchestrator.provision(InfrastructureRequest.model_validate(request), principal(orchestrator.config), wait_seconds=45)


def failed_resource(response, kind):
    [resource] = getattr(response.resources, kind)
    return resource


WAITS = [
    ("cluster", {"provider": "gcp", "region": "us-central1", "cluster": {"node_count": 1}}, "wait_container", "container.googleapis.com", ("clusters",)),
    ("compute", VM_REQUEST, "wait_compute", "/instances", ("instances",)),
    ("storage", {**VM_REQUEST, "storage": {"type": "persistent", "size_gb": 20}}, "wait_compute", "/disks", ("disks",)),
    ("network", {**VM_REQUEST, "network": {"public_access": True}}, "wait_compute", "/subnetworks", ("networks", "subnets", "firewalls")),
    ("database", {"provider": "gcp", "region": "us-central1", "database": {"public_access": True}}, "wait_sql", "sqladmin.googleapis.com/v1/projects/test-project/instances",
     ("sql",)),
    ("cache", {"provider": "gcp", "region": "us-central1", "network": {}, "cache": {"memory_gb": 1}}, "wait_longrunning", "redis.googleapis.com", ("redis",)),
    ("load_balancer", LB_REQUEST, "wait_compute", "/global/urlMaps", LB_PARTS),
]


@pytest.mark.parametrize(("kind", "request_body", "wait", "fragment", "stores"), WAITS, ids=[item[0] for item in WAITS])
def test_a_create_whose_wait_fails_is_cleaned_up(tmp_path, kind, request_body, wait, fragment, stores):
    orchestrator, fake = make_orchestrator(tmp_path)
    fail_wait_after(fake, wait, fragment)
    response = provision(orchestrator, request_body)
    assert response.status == RequestStatus.FAILED and f"at {kind}" in response.error.message and response.error.details["cause"] == "PROVIDER_AUTH_FAILED"
    cleanup = response.error.details["cleanup"]
    resource = failed_resource(response, kind)
    assert resource.provider_id and resource.id in cleanup["deleted"] and cleanup["remaining"] == []
    assert resource.status == ResourceStatus.DELETED
    assert all(not getattr(fake, store) for store in stores), {store: list(getattr(fake, store)) for store in stores if getattr(fake, store)}


@pytest.mark.parametrize(("request_body", "plant", "stores"), [
    (FULL_REQUEST, ("wait_compute", "/global/addresses"), ("addresses", "networks")),
    ({**VM_REQUEST, "queue": {"name": "events"}}, ("PUT", "/subscriptions/"), ("topics", "subscriptions")),
    ({**VM_REQUEST, "secret": {"name": "api-key", "generate_value": True}}, ("POST", ":addVersion"), ("secrets",)),
], ids=["services-range", "queue-topic", "secret"])
def test_a_failure_after_the_first_part_leaves_nothing(tmp_path, request_body, plant, stores):
    """A private database's network gains a reserved range before the peering; a queue's topic comes before its
    subscription; a secret is made before its first version. A failure after the first part still deletes it."""
    orchestrator, fake = make_orchestrator(tmp_path)
    if plant[0].startswith("wait_"):
        fail_wait_after(fake, *plant)
    else:
        fake.fail_on.append(plant)
    response = provision(orchestrator, request_body)
    assert response.status == RequestStatus.FAILED and response.error.details["cleanup"]["remaining"] == []
    assert all(not getattr(fake, store) for store in stores), {store: list(getattr(fake, store)) for store in stores if getattr(fake, store)}
    assert not fake.instances


def test_destroy_deletes_a_cluster_whose_wait_failed(tmp_path):
    """The 2026-10-29 run: cleanup kept by the operator, the cluster RUNNING at Google, then the request destroyed."""
    orchestrator, fake = make_orchestrator(tmp_path, config=make_config(tmp_path, cleanup_on_failure=False))
    fail_wait_after(fake, "wait_container", "container.googleapis.com")
    response = provision(orchestrator, {"provider": "gcp", "region": "us-central1", "cluster": {"node_count": 1}})
    cluster = failed_resource(response, "cluster")
    assert response.status == RequestStatus.FAILED and cluster.status == ResourceStatus.FAILED and cluster.provider_id
    assert response.error.details["cleanup"] == {"policy": "kept", "deleted": [], "remaining": [cluster.id]}
    assert fake.clusters, "Google made it"
    destroyed = orchestrator.destroy(response.request_id, principal(orchestrator.config), wait_seconds=45)
    assert destroyed.status == RequestStatus.DESTROYED and not fake.clusters
    assert failed_resource(destroyed, "cluster").status == ResourceStatus.DELETED


@pytest.mark.parametrize("cleanup_on_failure", [True, False], ids=["cleanup", "destroy"])
def test_a_create_google_accepted_but_never_made_is_deleted_quietly(tmp_path, cleanup_on_failure):
    orchestrator, fake = make_orchestrator(tmp_path, config=make_config(tmp_path, cleanup_on_failure=cleanup_on_failure))
    fail_wait_after(fake, "wait_container", "container.googleapis.com", InfrastructureError(ErrorCode.PROVIDER_ERROR, "Kubernetes Engine operation failed"), gone=True)
    response = provision(orchestrator, {"provider": "gcp", "region": "us-central1", "cluster": {"node_count": 1}})
    assert response.status == RequestStatus.FAILED and not fake.clusters
    if cleanup_on_failure:
        assert response.error.details["cleanup"]["remaining"] == [] and failed_resource(response, "cluster").status == ResourceStatus.DELETED
    else:
        destroyed = orchestrator.destroy(response.request_id, principal(orchestrator.config), wait_seconds=45)
        assert destroyed.status == RequestStatus.DESTROYED and destroyed.error is None


def test_a_restart_during_the_wait_deletes_what_google_accepted(tmp_path):
    orchestrator, fake = make_orchestrator(tmp_path)
    fail_wait_after(fake, "wait_container", "container.googleapis.com", Died())
    with pytest.raises(Died):
        provision(orchestrator, {"provider": "gcp", "region": "us-central1", "cluster": {"node_count": 1}})
    assert fake.clusters
    [found] = orchestrator.recover()
    assert found["now"] == "FAILED" and not fake.clusters
    response = orchestrator.status(found["request_id"], principal(orchestrator.config), refresh=False)
    assert failed_resource(response, "cluster").status == ResourceStatus.DELETED
    assert "had begun to create, is deleted now" in response.error.message and "not yet been answered" in response.error.message
