"""Releases on a managed container service, Cloud Run (docs/DELIVERY-SPEC.md, phase 4, step 2): a revision per release,
traffic moved only to a ready one, rollback by traffic, remove by the stand-in, secrets as pinned references whose values
never pass through this service."""
import json
import sqlite3

import pydantic
import pytest
from delivery_fakes import DIGEST, SERVICE_ID, make_config, principal, release_dict, service_resource, service_response
from fleet_fakes import SECRET_VALUE, V1, V2, V3, World
from run_fakes import FIRST

from appdelivery import planner
from appdelivery.errors import DeliveryError, ErrorCode
from appdelivery.infra import InfraRequest
from appdelivery.models import ReleaseRequest
from appdelivery.run import REVISION, STAND_IN

SERVICE = {"service": True}


def service_release(image=V1, environment="dev", **overrides):
    return release_dict(environment=environment, image=image, target=SERVICE, **overrides)


def deploy(world, image=V1, **overrides):
    return world.delivery.deploy(ReleaseRequest.model_validate(service_release(image, **overrides)), world.team)


def revision(release_id):
    return f"shop-service-{release_id}"


# ---- the contract
@pytest.mark.parametrize("target", [{"service": True, "workload": "web"}, {"service": True, "group": "web"}, {"service": True, "cluster": True},
                                    {"service": "Shop_Api"}, {"service": True, "expose": {}}])
def test_a_service_target_names_only_its_service(target):
    with pytest.raises((DeliveryError, pydantic.ValidationError)):
        ReleaseRequest.model_validate(release_dict(target=target))


def test_releases_are_kept_per_service():
    assert ReleaseRequest.model_validate(release_dict(target={"service": "api"})).target.key() == "service:api"
    assert ReleaseRequest.model_validate(release_dict(target={"service": "service"})).target.key() == "service:service"


# ---- the plan
def plan(answer, **overrides):
    config = make_config()
    release = ReleaseRequest.model_validate(release_dict(**{"environment": "prod", "image": f"us-central1-docker.pkg.dev/acme-apps/shop/web@sha256:{DIGEST}",
                                                            "target": SERVICE, **overrides}))
    return planner.plan(release, principal(config), config, InfraRequest.model_validate(answer))


def test_a_service_release_plans_the_service_and_its_address():
    planned = plan(service_response())
    assert planned.target == "service" and [item.resource for item in planned.machines] == [SERVICE_ID] and planned.url == "https://shop-service-xyz.a.run.app"


def two_services():
    data = service_response()
    data["resources"]["service"] = [service_resource("svc-a00000000000", "shop-api"), service_resource("svc-b00000000000", "shop-web")]
    data["access"][0]["resources"] += ["svc-a00000000000", "svc-b00000000000"]
    return data


@pytest.mark.parametrize("answer, overrides, code, said", [
    (two_services(), {}, ErrorCode.INVALID_RELEASE, "2 services (api, web); name one"),
    (two_services(), {"target": {"service": "db"}}, ErrorCode.NOT_FOUND, "no service db"),
    (service_response(), {"env": {"PORT": {"value": "9999"}}}, ErrorCode.INVALID_BINDING, "Cloud Run sets PORT"),
])
def test_a_service_release_that_cannot_run_is_refused(answer, overrides, code, said):
    with pytest.raises(DeliveryError) as caught:
        plan(answer, **overrides)
    assert caught.value.code == code and said in caught.value.message


def test_a_service_the_environment_was_not_granted_on_is_refused():
    data = service_response()
    data["access"][0]["resources"].remove(SERVICE_ID)
    with pytest.raises(DeliveryError) as caught:
        plan(data)
    assert caught.value.code == ErrorCode.NOT_GRANTED


# ---- a release's life on Cloud Run
def test_a_release_is_a_revision_named_after_it_that_gets_all_traffic_once_ready():
    world = World(service=True)
    done = deploy(world, env={"GREETING": {"value": "hello"}, "API_KEY": {"secret": "db-password"}})
    assert done.status == "RUNNING" and done.targets[0].status == "HEALTHY"
    assert world.run.serving() == revision(done.release_id) and world.run.image() == V1
    first_mask, first = world.run.patches[0]
    assert first_mask == "template,traffic" and first["traffic"] == [{"type": REVISION, "revision": FIRST, "percent": 100}], "made with the traffic held where it was"
    container = first["template"]["containers"][0]
    assert container["ports"] == [{"containerPort": 8080}] and container["startupProbe"]["httpGet"] == {"path": "/healthz", "port": 8080}
    assert container["resources"] == {"limits": {"cpu": "1", "memory": "512Mi"}}, "the limits the infrastructure layer set"
    assert world.run.env() == {"GREETING": "hello", "API_KEY": {"secretKeyRef": {"secret": "projects/infra-project/secrets/shop-db-password", "version": "7"}}}
    assert world.run.patches[1] == ("traffic", {"traffic": [{"type": REVISION, "revision": revision(done.release_id), "percent": 100}]})


def test_a_revision_that_is_not_ready_gets_no_traffic_and_the_running_release_keeps_its_rollback():
    world = World(service=True)
    first = deploy(world)
    second = deploy(world, V2)
    world.run.bad_images.add(V3)
    failed = deploy(world, V3)
    assert failed.status == "FAILED" and failed.error["code"] == "ROLLOUT_FAILED" and failed.targets[0].status == "UNHEALTHY"
    assert "startup probe failed" in failed.targets[0].error["message"] and "previous release runs again" in failed.targets[0].error["message"]
    assert world.run.serving() == revision(second.release_id)
    running = world.delivery.get(second.release_id, world.team)
    assert running.status == "RUNNING" and running.can_roll_back and world.delivery.get(first.release_id, world.team).status == "SUPERSEDED"


def test_a_first_release_that_fails_leaves_the_stand_in():
    world = World(service=True)
    world.run.bad_images.add(V1)
    failed = deploy(world)
    assert failed.status == "FAILED" and "nothing ran there before" in failed.targets[0].error["message"] and world.run.serving() == FIRST


def test_rollback_moves_the_traffic_and_remove_puts_the_stand_in_back():
    world = World(service=True)
    first = deploy(world)
    second = deploy(world, V2)
    rolled = world.delivery.rollback(second.release_id, world.team)
    assert rolled.status == "ROLLED_BACK" and world.run.serving() == revision(first.release_id) and world.run.image() == V1
    assert world.delivery.get(first.release_id, world.team).status == "RUNNING"
    removed = world.delivery.remove(first.release_id, world.team)
    assert removed.status == "REMOVED" and world.run.image() == STAND_IN
    assert world.delivery.running(world.infra.answers["dev"]["request_id"], world.team)["groups"] == []


def test_running_asks_cloud_run():
    world = World(service=True)
    done = deploy(world)
    [group] = world.delivery.running(world.infra.answers["dev"]["request_id"], world.team, live=True)["groups"]
    assert group["group"] == "service:service" and group["machines"][0]["running"] and group["machines"][0]["matches"] and group["release_id"] == done.release_id


def test_an_unreachable_service_fails_the_rollout_with_its_code():
    world = World(service=True)
    world.run.unreachable = True
    failed = deploy(world)
    assert failed.status == "FAILED" and failed.targets[0].error["code"] == "SERVICE_UNREACHABLE"


def test_no_secret_value_passes_through_this_service(tmp_path):
    world = World(database=str(tmp_path / "delivery.db"), service=True)
    deploy(world, env={"API_KEY": {"secret": "db-password"}})
    assert SECRET_VALUE not in json.dumps(world.run.patches)
    dump = "\n".join(sqlite3.connect(tmp_path / "delivery.db").iterdump())
    assert SECRET_VALUE not in dump and "dev-token" not in dump
