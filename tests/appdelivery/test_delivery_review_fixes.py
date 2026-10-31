"""The code review of 2026-10-31 over releases S1 and S2 of the security assessment, delivery side: provenance held to the
step a build actually sent, and one team's build order never waiting on another's."""
import threading

import pytest
from fleet_fakes import World
from test_delivery_build import request
from test_delivery_provenance import BUILD, EXPECTED, IMAGE, OWN_PEM, refused, rewritten, world_requiring_provenance

from appdelivery import cloudbuild, provenance


# ---- provenance is held to what was composed then, not to what this code would compose now
def test_a_build_keeps_the_step_it_sent_on_its_record():
    world = World()
    build = world.delivery.build(request(dockerfile="services/web/Dockerfile", context="services/web"), world.team)
    [created] = world.cloud_build.created
    kept = world.state.get_build(build.build_id)["request"]["composed"]
    assert kept == cloudbuild.composed(created["body"]) and kept["args"][-1] == "./services/web"
    assert world.delivery.get_build(build.build_id, world.team).context == "services/web", "the build's view is unchanged"


def test_the_kept_step_is_what_provenance_is_held_to_whatever_the_builder_is_now():
    step = {"builder": "gcr.io/cloud-builders/docker@sha256:" + "e" * 64, "args": ["build", "."], "tags": ["appdelivery"]}
    record = {"build_id": "bld-1", "infra_owner": "shop-team", "environment": "dev", "image": "x:bld-1", "request": {"context": "app", "composed": step}}
    assert cloudbuild.expected(record, "gcr.io/cloud-builders/docker@sha256:" + "f" * 64) == step


LEGACY = {"build_id": BUILD["build_id"], "infra_owner": "shop-team", "environment": "dev", "image": IMAGE.split("@")[0] + ":" + BUILD["build_id"],
          "request": {"dockerfile": "Dockerfile.appdelivery", "context": "app"}}


@pytest.mark.parametrize("context", ["app", "./app"])
def test_a_build_recorded_before_its_step_was_kept_verifies_with_the_context_as_sent_then_or_anchored(context):
    expected = cloudbuild.expected(LEGACY, EXPECTED["builder"])
    found = rewritten(lambda config: config["steps"][0]["args"].__setitem__(-1, context))
    assert provenance.verify(IMAGE, BUILD, found, keys={"own": OWN_PEM}, expected=expected)["commit"] == BUILD["revision"]


def test_a_build_recorded_before_its_step_was_kept_still_refuses_another_context():
    expected = cloudbuild.expected(LEGACY, EXPECTED["builder"])
    found = rewritten(lambda config: config["steps"][0]["args"].__setitem__(-1, "other"))
    assert "build arguments" in refused(found=found, keys={"own": OWN_PEM}, expected=expected).message


def test_a_build_of_an_environment_since_removed_fails_its_provenance_not_the_service():
    from test_delivery_provenance import FOUND
    world = world_requiring_provenance(FOUND)
    world.state.create_build(BUILD["build_id"], "retired", "shop-team", "shop-team", "shop-web", BUILD["revision"], IMAGE.split("@")[0] + ":" + BUILD["build_id"],
                             "WORKING", BUILD["cloud_build_id"], None, {"environment": "retired", "source": "shop-web", "revision": BUILD["revision"], "image": "web",
                                                                       "dockerfile": "Dockerfile.appdelivery", "context": "."})
    world.state.update_build(BUILD["build_id"], ("WORKING",), "SUCCEEDED", built=IMAGE)
    released = world.deploy(IMAGE)
    assert released.status == "FAILED" and released.error["code"] == "PROVENANCE_FAILED", "tags name an environment that is not the record's"


# ---- one team's build order never waits on another's
def test_each_team_and_environment_orders_its_builds_under_its_own_lock():
    world = World()
    builder = world.delivery.builder
    assert builder._ordering("shop-team", "dev") is builder._ordering("shop-team", "dev")
    assert builder._ordering("shop-team", "dev") is not builder._ordering("other-team", "dev")
    assert builder._ordering("shop-team", "dev") is not builder._ordering("shop-team", "prod")


def test_a_build_is_ordered_while_another_teams_order_is_still_waiting_on_cloud_build():
    world = World()
    other = world.delivery.builder._ordering("other-team", "dev")
    with other:  # another team's order, held up by a slow answer
        done = []
        thread = threading.Thread(target=lambda: done.append(world.delivery.build(request(), world.team)))
        thread.start()
        thread.join(timeout=10)
    assert done and done[0].status == "SUCCEEDED"
