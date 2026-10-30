"""Releases on a cluster (docs/DELIVERY-SPEC.md, phase 2): the contract, the plan, the Kubernetes objects, and a
release's whole life on a fake Kubernetes API, failure and rollback included."""
import json
import logging
import sqlite3

import httpx
import pydantic
import pytest
from delivery_fakes import CLUSTER_ID, DIGEST, cluster_response, make_config, principal, release_dict, two_clusters, with_door
from fleet_fakes import SECRET_VALUE, V1, V2, V3, World
from kube_fakes import NAMESPACE

from appdelivery import planner
from appdelivery.errors import DeliveryError, ErrorCode, problems_from
from appdelivery.infra import InfraRequest
from appdelivery.kube import KubeClient, deployment_body, secret_name, service_body
from appdelivery.models import ReleaseRequest

CLUSTER = {"cluster": True, "workload": "web", "replicas": 2}
DEPLOYMENTS = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments"


def cluster_release(image=V1, environment="dev", **overrides):
    return release_dict(environment=environment, image=image, target=CLUSTER, **overrides)


def deploy(world, image=V1, **overrides):
    return world.delivery.deploy(ReleaseRequest.model_validate(cluster_release(image, **overrides)), world.team)


# ---- the contract
@pytest.mark.parametrize("target", [{"cluster": True}, {"group": "web", "cluster": True, "workload": "web"}, {}, {"group": "web", "workload": "web"},
                                    {"group": "web", "replicas": 3}, {"cluster": True, "workload": "Web_App"}, {"cluster": True, "workload": "w", "replicas": 0}])
def test_a_target_is_a_group_or_a_named_workload_on_the_cluster(target):
    with pytest.raises(pydantic.ValidationError) as caught:
        ReleaseRequest.model_validate(release_dict(target=target))
    assert problems_from(caught.value)[0].code == ErrorCode.INVALID_RELEASE


def test_a_cluster_target_keys_its_releases_by_workload():
    release = ReleaseRequest.model_validate(cluster_release())
    assert release.target.key() == "cluster:web" and release.target.replicas == 2
    assert ReleaseRequest.model_validate(release_dict()).target.key() == "web"


# ---- the plan
def plan(answer, environment="prod", image=None):
    config = make_config()
    release = ReleaseRequest.model_validate(cluster_release(image or f"us-central1-docker.pkg.dev/acme-apps/shop/web@sha256:{DIGEST}", environment))
    return planner.plan(release, principal(config), config, InfraRequest.model_validate(answer))


def test_a_cluster_release_plans_the_namespace_workload_and_replicas():
    planned = plan(cluster_response())
    assert planned.target == "cluster" and planned.namespace == "shop" and planned.workload == "web" and planned.replicas == 2
    assert [item.resource for item in planned.machines] == [CLUSTER_ID]


@pytest.mark.parametrize(("change", "code"), [(lambda a: a["resources"].update(cluster=[]), "NOT_FOUND"),
                                              (lambda a: a["resources"]["cluster"][0].update(status="CREATING"), "INVALID_STATE"),
                                              (lambda a: a["resources"]["cluster"][0].update(namespace=None), "UNSUPPORTED_TARGET"),
                                              (lambda a: a["resources"]["cluster"][0].update(ca_certificate=None), "UNREACHABLE_TARGET"),
                                              (lambda a: a["access"][0]["resources"].remove(CLUSTER_ID), "NOT_GRANTED")])
def test_a_cluster_that_cannot_take_the_release_is_refused(change, code):
    answer = cluster_response()
    change(answer)
    with pytest.raises(DeliveryError) as caught:
        plan(answer)
    assert code in [item["code"] for item in caught.value.details.get("problems", [])] or caught.value.code.value == code


# ---- the Kubernetes objects
def test_the_deployment_never_drops_below_the_wanted_replicas_and_probes_the_health_path():
    release = ReleaseRequest.model_validate(cluster_release(health={"path": "/healthz", "timeout_seconds": 90}))
    body = deployment_body(NAMESPACE, "rel-000000000001", release)
    spec = body["spec"]
    assert spec["replicas"] == 2 and spec["strategy"]["rollingUpdate"] == {"maxUnavailable": 0, "maxSurge": 1} and spec["progressDeadlineSeconds"] == 90
    container = spec["template"]["spec"]["containers"][0]
    assert container["image"] == V1 and container["ports"] == [{"containerPort": 8080}]
    assert container["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": 8080}
    assert container["envFrom"] == [{"secretRef": {"name": "web-env-rel-000000000001"}}] and "env" not in container
    assert spec["template"]["spec"]["automountServiceAccountToken"] is False, "the workload gets no Kubernetes credential"
    assert set(container) == {"name", "image", "ports", "envFrom", "readinessProbe"}, "no command, volume or privilege"
    service = service_body(NAMESPACE, release)
    assert service["spec"]["type"] == "ClusterIP" and service["spec"]["ports"] == [{"name": "http", "port": 8080, "targetPort": 8080}]


# ---- a release's life on the fake cluster
def test_a_release_runs_on_the_cluster_with_its_own_secret():
    world = World(cluster=True)
    first = deploy(world)
    assert first.status == "RUNNING" and [(t.resource, t.status) for t in first.targets] == [(CLUSTER_ID, "HEALTHY")]
    assert world.kube.image() == V1 and world.kube.deployment()["spec"]["replicas"] == 2
    assert world.kube.names("Secret") == [secret_name("web", first.release_id)] and world.kube.names("Service") == ["web"]
    stored = world.kube.objects[f"/api/v1/namespaces/{NAMESPACE}/secrets/{secret_name('web', first.release_id)}"]["stringData"]
    assert stored == {"LOG_LEVEL": "info", "DB_HOST": "10.20.0.5", "DB_PASSWORD": SECRET_VALUE}
    assert world.fleet.calls == [], "no machine was touched"
    assert world.cluster_tokens == [world.config.environments["dev"].service_account], "the cluster is reached as the environment's account"


def test_later_releases_keep_two_secrets_and_the_service_keeps_its_address():
    world = World(cluster=True)
    first = deploy(world)
    second = deploy(world, V2)
    assert second.previous == first.release_id and second.can_roll_back and world.delivery.get(first.release_id, world.team).status == "SUPERSEDED"
    third = deploy(world, V3)
    assert world.kube.image() == V3
    assert world.kube.names("Secret") == sorted(secret_name("web", item.release_id) for item in (second, third)), "older than the previous are deleted"
    assert world.kube.objects[f"/api/v1/namespaces/{NAMESPACE}/services/web"]["spec"]["clusterIP"] == "10.0.0.10"


def test_an_unhealthy_release_puts_the_previous_one_back_and_keeps_its_rollback(caplog):
    caplog.set_level(logging.INFO, logger="appdelivery")
    world = World(cluster=True)
    first = deploy(world)
    second = deploy(world, V2)
    world.kube.bad_images.add(V3)
    failed = deploy(world, V3)
    assert failed.status == "FAILED" and failed.error["code"] == "ROLLOUT_FAILED" and failed.targets[0].status == "UNHEALTHY"
    assert "CrashLoopBackOff" in failed.targets[0].error["message"] and "previous release runs again" in failed.targets[0].error["message"]
    assert "[redacted]" in failed.targets[0].log and SECRET_VALUE not in failed.targets[0].log
    assert world.kube.image() == V2, "the previous release runs again"
    assert secret_name("web", failed.release_id) not in world.kube.names("Secret")
    running = world.delivery.get(second.release_id, world.team)
    assert running.status == "RUNNING" and running.can_roll_back, "on a cluster the previous release keeps its own Secret, so its rollback stays"
    rolled = world.delivery.rollback(second.release_id, world.team)
    assert rolled.status == "ROLLED_BACK" and world.kube.image() == V1 and world.delivery.get(first.release_id, world.team).status == "RUNNING"
    assert SECRET_VALUE not in caplog.text


def test_a_rollout_kubernetes_gave_up_on_puts_the_previous_one_back_while_the_timeout_is_still_shown():
    """On a real cluster the timed-out condition outlived the restore and was read as the restore failing (2026-10-29)."""
    world = World(cluster=True)
    deploy(world)
    second = deploy(world, V2)
    world.kube.slow_images.add(V3)
    world.kube.lagging = True
    failed = deploy(world, V3)
    assert failed.status == "FAILED" and "timed out progressing" in failed.targets[0].error["message"]
    assert "previous release runs again" in failed.targets[0].error["message"] and world.kube.image() == V2
    assert world.delivery.get(second.release_id, world.team).can_roll_back


def test_a_workloads_first_release_that_fails_leaves_nothing():
    world = World(cluster=True)
    world.kube.bad_images.add(V1)
    failed = deploy(world)
    assert failed.status == "FAILED" and "nothing ran there before" in failed.targets[0].error["message"]
    assert world.kube.names("Deployment") == [] and world.kube.names("Service") == [] and world.kube.names("Secret") == []


def test_rollback_then_remove():
    world = World(cluster=True)
    first = deploy(world)
    second = deploy(world, V2)
    rolled = world.delivery.rollback(second.release_id, world.team)
    assert rolled.status == "ROLLED_BACK" and world.kube.image() == V1
    assert world.kube.names("Secret") == [secret_name("web", first.release_id)], "only the release running keeps its Secret"
    restored = world.delivery.get(first.release_id, world.team)
    assert restored.status == "RUNNING" and not restored.can_roll_back
    removed = world.delivery.remove(first.release_id, world.team)
    assert removed.status == "REMOVED" and world.kube.objects == {}


def test_running_asks_the_cluster():
    world = World(cluster=True)
    first = deploy(world)
    group = world.delivery.running(first.request_id, world.team, live=True)["groups"][0]
    assert group["group"] == "cluster:web" and group["machines"][0]["matches"] and group["machines"][0]["running"]


def test_an_unreachable_cluster_fails_the_rollout_with_its_code():
    world = World(cluster=True)
    world.kube.unreachable = True
    failed = deploy(world)
    assert failed.status == "FAILED" and failed.targets[0].error["code"] == "CLUSTER_UNREACHABLE"


def test_a_machine_release_and_a_cluster_release_on_one_request_are_kept_apart():
    world = World(cluster=True)
    on_cluster = deploy(world)
    on_machines = world.delivery.deploy(ReleaseRequest.model_validate(release_dict(environment="dev", image=V2)), world.team)
    assert on_cluster.status == on_machines.status == "RUNNING" and on_machines.previous is None
    assert {group["group"] for group in world.delivery.running(on_cluster.request_id, world.team)["groups"]} == {"web", "cluster:web"}


def test_no_secret_value_reaches_the_service_state(tmp_path):
    world = World(database=str(tmp_path / "delivery.db"), cluster=True)
    world.kube.bad_images.add(V2)
    deploy(world)
    deploy(world, V2)
    dump = "\n".join(sqlite3.connect(tmp_path / "delivery.db").iterdump())
    assert SECRET_VALUE not in dump and "dev-token" not in dump


# ---- the client's HTTP
def client(status, body=None):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {})
    return KubeClient("35.201.0.7", "unused", lambda: "env-token", client=httpx.Client(transport=httpx.MockTransport(handle))), seen


def test_the_client_uses_the_environments_token_and_codes_refusals():
    kube, seen = client(200, {"items": []})
    kube.get(DEPLOYMENTS, labelSelector="a=b")
    assert str(seen[0].url) == f"https://35.201.0.7{DEPLOYMENTS}?labelSelector=a%3Db" and seen[0].headers["Authorization"] == "Bearer env-token"
    assert client(404)[0].get(f"{DEPLOYMENTS}/web") is None
    for status, code in ((401, ErrorCode.CLUSTER_UNREACHABLE), (403, ErrorCode.CLUSTER_UNREACHABLE), (422, ErrorCode.CLUSTER_ERROR), (500, ErrorCode.CLUSTER_ERROR)):
        with pytest.raises(DeliveryError) as caught:
            client(status)[0].create(DEPLOYMENTS, {"metadata": {"name": "web"}})
        assert caught.value.code == code
    with pytest.raises(DeliveryError) as caught:
        client(403)[0].get(DEPLOYMENTS)
    assert "container.clusterViewer" in caught.value.message and json.dumps(caught.value.to_dict())


# ---- several clusters on one request (phase 3, step 2)
def plan_on(cluster, answer):
    config = make_config()
    release = ReleaseRequest.model_validate(release_dict(environment="prod", image=f"us-central1-docker.pkg.dev/acme-apps/shop/web@sha256:{DIGEST}",
                                                         target={"cluster": cluster, "workload": "web"}))
    return planner.plan(release, principal(config), config, InfraRequest.model_validate(answer))


def test_a_target_names_one_of_the_requests_clusters():
    for name, resource in (("blue", "clu-b10e00000000"), ("green", "clu-6ee000000000")):
        planned = plan_on(name, two_clusters())
        assert [item.resource for item in planned.machines] == [resource] and planned.namespace == "shop"


@pytest.mark.parametrize("cluster, code, said", [(True, ErrorCode.INVALID_RELEASE, "2 clusters (blue, green); name one"),
                                                 ("red", ErrorCode.NOT_FOUND, "no cluster red; it has blue, green")])
def test_a_target_that_does_not_say_which_cluster_or_names_none_is_refused(cluster, code, said):
    with pytest.raises(DeliveryError) as caught:
        plan_on(cluster, two_clusters())
    assert caught.value.code == code and said in caught.value.message


def test_a_cluster_name_is_a_short_name():
    with pytest.raises((DeliveryError, pydantic.ValidationError)):
        ReleaseRequest.model_validate(release_dict(target={"cluster": "Shop_Blue", "workload": "web"}))


def test_releases_are_kept_per_cluster_and_workload():
    assert ReleaseRequest.model_validate(release_dict(target=CLUSTER)).target.key() == "cluster:web", "the key releases had before clusters had names"
    assert ReleaseRequest.model_validate(release_dict(target={"cluster": "cluster", "workload": "web"})).target.key() == "cluster:web"
    assert ReleaseRequest.model_validate(release_dict(target={"cluster": "blue", "workload": "web"})).target.key() == "cluster:blue:web"


def test_true_and_the_only_clusters_name_are_one_target():
    """A release asked for with cluster: true is kept under the only cluster's name, so the next one asked for by name replaces it."""
    data = cluster_response()
    data["resources"]["cluster"][0]["name"] = "shop-blue"
    infra = InfraRequest.model_validate(data)
    resolved = planner.resolve(ReleaseRequest.model_validate(release_dict(target=CLUSTER)), infra)
    assert resolved.target.cluster == "blue" and resolved.target.key() == "cluster:blue:web"
    unnamed = planner.resolve(ReleaseRequest.model_validate(release_dict(target=CLUSTER)), InfraRequest.model_validate(cluster_response()))
    assert unnamed.target.cluster == "cluster" and unnamed.target.key() == "cluster:web"


def test_a_release_by_name_replaces_one_asked_for_with_true():
    world = World(cluster=True)
    first = deploy(world)
    second = world.delivery.deploy(ReleaseRequest.model_validate(release_dict(environment="dev", image=V2, target={"cluster": "cluster", "workload": "web", "replicas": 2})),
                                   world.team)
    assert second.status == "RUNNING" and second.previous == first.release_id
    assert world.delivery.get(first.release_id, world.team).status == "SUPERSEDED"


# ---- exposing a workload through the cluster's front door (phase 3, step 6)
ROUTES = f"/apis/gateway.networking.k8s.io/v1/namespaces/{NAMESPACE}/httproutes"
HEALTH_CHECKS = f"/apis/networking.gke.io/v1/namespaces/{NAMESPACE}/healthcheckpolicies"
BACKEND_POLICIES = f"/apis/networking.gke.io/v1/namespaces/{NAMESPACE}/gcpbackendpolicies"


def exposed(answer, expose):
    config = make_config()
    release = ReleaseRequest.model_validate(release_dict(environment="prod", image=f"us-central1-docker.pkg.dev/acme-apps/shop/web@sha256:{DIGEST}",
                                                         target={"cluster": True, "workload": "web", "expose": expose}))
    return planner.plan(release, principal(config), config, InfraRequest.model_validate(answer))


def test_an_exposed_workload_plans_its_url():
    assert exposed(with_door(cluster_response()), {"hostname": "shop.example.com", "path": "/api"}).url == "https://shop.example.com/api"
    assert exposed(with_door(cluster_response()), {}).url == "https://shop.example.com/", "the door's first hostname"
    assert exposed(with_door(cluster_response(), hostnames=()), {"path": "/"}).url == "http://34.120.0.9/"
    assert exposed(cluster_response(), None).url is None


@pytest.mark.parametrize("answer, expose, code, said", [
    (cluster_response(), {}, ErrorCode.UNSUPPORTED_TARGET, "has no front door"),
    (with_door(cluster_response()), {"hostname": "other.example.com"}, ErrorCode.INVALID_RELEASE, "serves shop.example.com, not other.example.com"),
    (with_door(cluster_response(), hostnames=()), {"hostname": "shop.example.com"}, ErrorCode.INVALID_RELEASE, "plain HTTP on its address"),
])
def test_an_exposure_the_front_door_cannot_serve_is_refused(answer, expose, code, said):
    with pytest.raises(DeliveryError) as caught:
        exposed(answer, expose)
    assert caught.value.code == code and said in caught.value.message


def test_a_certificate_not_yet_active_is_a_warning():
    planned = exposed(with_door(cluster_response(), certificate="PROVISIONING"), {})
    assert any("certificate is PROVISIONING" in warning and "34.120.0.9" in warning for warning in planned.warnings)


@pytest.mark.parametrize("target", [{"group": "web", "expose": {}}, {"cluster": True, "workload": "web", "expose": {"hostname": "Shop.Example.com"}},
                                    {"cluster": True, "workload": "web", "expose": {"path": "api"}}])
def test_expose_belongs_to_a_cluster_target_and_names_a_host_and_path(target):
    with pytest.raises((DeliveryError, pydantic.ValidationError)):
        ReleaseRequest.model_validate(release_dict(target=target))


def door_world(hostnames=("shop.example.com",), security_policy=None):
    world = World(cluster=True)
    world.infra.answers["dev"] = with_door(world.infra.answers["dev"], hostnames=hostnames, security_policy=security_policy)
    return world


def deploy_exposed(world, image=V1, expose=None, health="/"):
    target = {"cluster": True, "workload": "web", "replicas": 2, "expose": {"path": "/"} if expose is None else expose}
    release = release_dict(environment="dev", image=image, target=target, health={"path": health, "timeout_seconds": 60})
    return world.delivery.deploy(ReleaseRequest.model_validate(release), world.team)


def test_a_healthy_exposed_release_is_routed_from_the_front_door():
    world = door_world()
    done = deploy_exposed(world, expose={"hostname": "shop.example.com", "path": "/"})
    assert done.status == "RUNNING"
    route = world.kube.objects[f"{ROUTES}/web"]
    assert route["spec"]["parentRefs"] == [{"group": "gateway.networking.k8s.io", "kind": "Gateway", "namespace": "cloudinfra-gateway", "name": "public", "sectionName": "https"}]
    assert route["spec"]["hostnames"] == ["shop.example.com"]
    assert route["spec"]["rules"] == [{"matches": [{"path": {"type": "PathPrefix", "value": "/"}}], "backendRefs": [{"name": "web", "port": 8080}]}]
    assert f"{HEALTH_CHECKS}/web" not in world.kube.objects, "the load balancer's own check of / is right"


def test_a_door_with_a_firewall_puts_it_on_each_exposed_workload_and_takes_it_off_with_the_route():
    world = door_world(security_policy="shop-cluster-waf")
    done = deploy_exposed(world)
    policy = world.kube.objects[f"{BACKEND_POLICIES}/web"]
    assert policy["spec"] == {"default": {"securityPolicy": "shop-cluster-waf"}, "targetRef": {"group": "", "kind": "Service", "name": "web"}}
    assert world.delivery.remove(done.release_id, world.team).status == "REMOVED"
    assert f"{BACKEND_POLICIES}/web" not in world.kube.objects and f"{ROUTES}/web" not in world.kube.objects


def test_a_door_without_a_firewall_writes_no_backend_policy():
    world = door_world()
    deploy_exposed(world)
    assert f"{BACKEND_POLICIES}/web" not in world.kube.objects


def test_a_health_path_other_than_root_gets_the_load_balancer_a_health_check():
    world = door_world(hostnames=())
    deploy_exposed(world, health="/healthz")
    check = world.kube.objects[f"{HEALTH_CHECKS}/web"]["spec"]
    assert check["default"]["config"]["httpHealthCheck"] == {"port": 8080, "requestPath": "/healthz"} and check["targetRef"]["name"] == "web"
    assert world.kube.objects[f"{ROUTES}/web"]["spec"]["parentRefs"][0]["sectionName"] == "http" and "hostnames" not in world.kube.objects[f"{ROUTES}/web"]["spec"]


def test_a_failed_release_leaves_the_route_to_the_previous_one():
    world = door_world()
    deploy_exposed(world, expose={"path": "/"})
    world.kube.bad_images.add(V3)
    failed = deploy_exposed(world, V3, expose={"path": "/v3"})
    assert failed.status == "FAILED" and world.kube.objects[f"{ROUTES}/web"]["spec"]["rules"][0]["matches"][0]["path"]["value"] == "/"


def test_a_release_without_expose_takes_the_workload_off_the_door_and_rollback_puts_it_back():
    world = door_world()
    first = deploy_exposed(world, expose={"path": "/"})
    second = world.delivery.deploy(ReleaseRequest.model_validate(cluster_release(V2)), world.team)
    assert second.status == "RUNNING" and f"{ROUTES}/web" not in world.kube.objects
    world.delivery.rollback(second.release_id, world.team)
    assert world.delivery.get(first.release_id, world.team).status == "RUNNING" and f"{ROUTES}/web" in world.kube.objects


def test_remove_takes_the_route_and_health_check_away():
    world = door_world()
    done = deploy_exposed(world, health="/healthz")
    world.delivery.remove(done.release_id, world.team)
    assert f"{ROUTES}/web" not in world.kube.objects and f"{HEALTH_CHECKS}/web" not in world.kube.objects


def test_a_first_release_that_fails_leaves_no_route():
    world = door_world()
    world.kube.bad_images.add(V1)
    assert deploy_exposed(world).status == "FAILED" and f"{ROUTES}/web" not in world.kube.objects
