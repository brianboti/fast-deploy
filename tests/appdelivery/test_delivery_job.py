"""Releases on a managed job, Cloud Run jobs (docs/DELIVERY-SPEC.md, phase 4, step 4): a release becomes the container
the job runs, replaced whole with the version read and nothing else of the job changed; rollback makes the previous
release's container again; remove puts the stand-in back; run_release starts a run and get_release_run says how it went.
Secrets are pinned references, never values."""
import json
import sqlite3

import pydantic
import pytest
from delivery_fakes import DIGEST, JOB_ID, job_response, make_config, principal, release_dict
from fleet_fakes import SECRET_VALUE, V1, V2, World

from appdelivery import planner
from appdelivery.errors import DeliveryError, ErrorCode
from appdelivery.infra import InfraRequest
from appdelivery.models import ReleaseRequest
from appdelivery.run import JOB_STAND_IN, RELEASE_LABEL

JOB = {"job": True}
ENV = {"MODE": {"value": "nightly"}, "API_KEY": {"secret": "db-password"}}


def deploy(world, image=V1, env=ENV):
    return world.delivery.deploy(ReleaseRequest.model_validate(release_dict(environment="dev", image=image, target=JOB, env=env)), world.team)


@pytest.mark.parametrize("target", [{"job": True, "workload": "w"}, {"job": True, "service": True}, {"job": "Night_Job"}, {"job": True, "replicas": 2}])
def test_a_job_target_names_only_its_job(target):
    with pytest.raises((DeliveryError, pydantic.ValidationError)):
        ReleaseRequest.model_validate(release_dict(target=target))


def test_a_job_release_plans_the_job():
    config = make_config()
    release = ReleaseRequest.model_validate(release_dict(environment="prod", image=f"us-central1-docker.pkg.dev/acme-apps/shop/web@sha256:{DIGEST}", target=JOB))
    planned = planner.plan(release, principal(config), config, InfraRequest.model_validate(job_response()))
    assert planned.target == "job" and [item.resource for item in planned.machines] == [JOB_ID]
    assert release.target.key() == "job:job"


def test_a_release_becomes_what_the_job_runs_and_nothing_else_of_the_job_changes():
    world = World(job=True)
    done = deploy(world)
    assert done.status == "RUNNING" and done.targets[0].status == "HEALTHY"
    container = world.run_job.job["template"]["template"]["containers"][0]
    assert world.run_job.image() == V1 and "ports" not in container and "startupProbe" not in container, "a job serves nothing"
    assert container["resources"] == {"limits": {"cpu": "1", "memory": "512Mi"}}
    assert world.run_job.env() == {"MODE": "nightly", "API_KEY": {"secretKeyRef": {"secret": "projects/infra-project/secrets/shop-db-password", "version": "7"}}}
    assert world.run_job.job["template"]["labels"][RELEASE_LABEL] == done.release_id


def test_run_starts_the_job_and_get_run_says_how_it_went():
    world = World(job=True)
    done = deploy(world)
    run = world.delivery.start_run(done.release_id, world.team, wait_seconds=5)
    assert run.status == "SUCCEEDED" and run.succeeded == 2 and run.tasks == 2 and run.log_url
    assert world.delivery.get_run(done.release_id, run.execution, world.team) == run
    world.run_job.failing_images.add(V1)
    failed = world.delivery.start_run(done.release_id, world.team)
    assert failed.status == "FAILED" and failed.failed == 2


def test_a_run_still_going_is_reported_running():
    world = World(job=True)
    done = deploy(world)
    world.run_job.still_running = True
    assert world.delivery.start_run(done.release_id, world.team).status == "RUNNING"


def test_only_the_release_a_job_holds_runs_and_only_a_deployer_starts_it():
    world = World(job=True)
    first = deploy(world)
    deploy(world, V2)
    with pytest.raises(DeliveryError) as caught:
        world.delivery.start_run(first.release_id, world.team)
    assert caught.value.code == ErrorCode.INVALID_STATE
    second = world.delivery.list_releases(world.team)[0]
    with pytest.raises(DeliveryError) as caught:
        world.delivery.start_run(second.release_id, world.oncall)
    assert caught.value.code == ErrorCode.PERMISSION_DENIED


def test_a_run_of_another_job_is_not_found():
    world = World(job=True)
    done = deploy(world)
    with pytest.raises(DeliveryError) as caught:
        world.delivery.get_run(done.release_id, "other-job-00001", world.team)
    assert caught.value.code == ErrorCode.NOT_FOUND


def test_rollback_makes_the_previous_container_again_and_remove_puts_the_stand_in_back():
    world = World(job=True)
    first = deploy(world)
    second = deploy(world, V2)
    assert world.run_job.image() == V2 and world.delivery.get(second.release_id, world.team).can_roll_back
    rolled = world.delivery.rollback(second.release_id, world.team)
    assert rolled.status == "ROLLED_BACK" and world.run_job.image() == V1 and world.run_job.job["template"]["labels"][RELEASE_LABEL] == first.release_id
    assert world.run_job.env()["API_KEY"]["secretKeyRef"]["version"] == "7", "its secrets pinned again"
    removed = world.delivery.remove(first.release_id, world.team)
    assert removed.status == "REMOVED" and world.run_job.image() == JOB_STAND_IN and RELEASE_LABEL not in world.run_job.job["template"]["labels"]


def test_running_says_what_the_job_holds():
    world = World(job=True)
    done = deploy(world)
    [group] = world.delivery.running(world.infra.answers["dev"]["request_id"], world.team, live=True)["groups"]
    assert group["group"] == "job:job" and group["machines"][0]["matches"] and group["release_id"] == done.release_id


def test_no_secret_value_passes_through_this_service(tmp_path):
    world = World(database=str(tmp_path / "delivery.db"), job=True)
    deploy(world)
    assert SECRET_VALUE not in json.dumps(world.run_job.writes)
    assert SECRET_VALUE not in "\n".join(sqlite3.connect(tmp_path / "delivery.db").iterdump())
