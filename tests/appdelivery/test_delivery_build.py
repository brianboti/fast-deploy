"""Builds in the trusted project (docs/research/RESEARCH-2026-10-30-trusted-build.md): the build request's contract, the
Cloud Build request this service composes, following a build to its end, and the rule that a release names only an image
a build of this service produced."""
import json
import logging
import sqlite3

import httpx
import pydantic
import pytest
from build_fakes import DIGEST_BUILT, FakeCloudBuild
from delivery_fakes import COMMIT, DEV_BUILD_SA, PROD_BUILD_SA, SOURCE_REPOSITORY, TEAM_REGISTRY, build_dict, config_dict, make_config, principal
from fleet_fakes import V1, World

from appdelivery import cloudbuild
from appdelivery.building import Builder
from appdelivery.config import Config
from appdelivery.errors import DeliveryError, ErrorCode, problems_from
from appdelivery.models import BuildRequest
from appdelivery.releases import InlineExecutor
from appdelivery.state import StateManager

BUILT = f"{TEAM_REGISTRY}/web@{DIGEST_BUILT}"


def request(**overrides):
    return BuildRequest.model_validate(build_dict(**overrides))


def refused(data):
    with pytest.raises(pydantic.ValidationError) as caught:
        BuildRequest.model_validate(data)
    return problems_from(caught.value, "build")


# ---- the contract
def test_a_build_names_a_source_a_full_commit_an_image_and_paths_in_the_source():
    build = request(dockerfile="services/web/Dockerfile", context="services/web", image="shop/web")
    assert (build.source, build.revision, build.image, build.dockerfile, build.context) == ("shop-web", COMMIT, "shop/web", "services/web/Dockerfile", "services/web")
    assert (request().dockerfile, request().context) == ("Dockerfile", ".")


@pytest.mark.parametrize("revision", ["main", "v1.4.2", COMMIT[:12], COMMIT.upper(), COMMIT + "0"])
def test_only_a_full_commit_is_built_because_a_branch_or_tag_moves(revision):
    problems = refused(build_dict(revision=revision))
    assert problems[0].code == ErrorCode.INVALID_BUILD and "40" in problems[0].message


@pytest.mark.parametrize("field,value", [("dockerfile", "../Dockerfile"), ("dockerfile", "/etc/passwd"), ("context", "-f"), ("context", "--network=host"),
                                         ("context", "a/../../b"), ("dockerfile", "a//b"), ("context", ""), ("dockerfile", "a b")])
def test_a_path_outside_the_source_or_one_docker_would_read_as_a_flag_is_refused(field, value):
    assert refused(build_dict(**{field: value}))[0].code == ErrorCode.INVALID_BUILD


@pytest.mark.parametrize("image", ["Web", "web:latest", "web@sha256:" + "a" * 64, "us-central1-docker.pkg.dev/p/r/web", "-web", "a/b/c/d/e"])
def test_the_image_is_a_name_under_the_teams_registry_and_nothing_else(image):
    assert refused(build_dict(image=image))[0].code == ErrorCode.INVALID_BUILD


@pytest.mark.parametrize("field", ["steps", "command", "args", "entrypoint", "build_args", "substitutions", "secrets", "service_account", "env", "script",
                                   "repository", "registry", "machine_type"])
def test_a_field_that_could_change_what_runs_in_the_build_is_not_part_of_a_build(field):
    problems = refused(build_dict(**{field: "anything"}))
    assert problems[0].code == ErrorCode.INVALID_BUILD and "not a field of a build" in problems[0].message


def test_the_build_contract_has_only_its_seven_fields():
    assert set(BuildRequest.model_fields) == {"environment", "source", "revision", "image", "dockerfile", "context", "note"}
    assert BuildRequest.model_config.get("extra") == "forbid"


# ---- the configuration
def test_a_build_account_is_never_the_account_that_runs_releases():
    data = config_dict()
    data["environments"]["dev"]["build"]["service_account"] = data["environments"]["dev"]["service_account"]
    with pytest.raises(pydantic.ValidationError, match="must differ"):
        Config.model_validate(data)


@pytest.mark.parametrize("field,value", [("repository", "projects/someone-else/locations/us-central1/connections/github/repositories/x"),
                                         ("registry", "us-central1-docker.pkg.dev/someone-else/images")])
def test_a_source_and_its_registry_live_in_the_trusted_project(field, value):
    data = config_dict()
    data["sources"]["shop-web"][field] = value
    with pytest.raises(pydantic.ValidationError, match="trusted project"):
        Config.model_validate(data)


def test_a_source_is_a_linked_repository_not_a_url():
    data = config_dict()
    data["sources"]["shop-web"]["repository"] = "https://github.com/acme/shop.git"
    with pytest.raises(pydantic.ValidationError):
        Config.model_validate(data)


# ---- the Cloud Build request
def test_the_build_is_one_docker_step_pushed_by_cloud_build_as_the_build_account_with_provenance_required():
    config = make_config()
    source = config.sources["shop-web"]
    body = cloudbuild.compose(request(dockerfile="web/Dockerfile", context="web", note="fix rounding"), source, config.environments["dev"].build,
                              "bld-0123456789ab", "shop-team")
    tag = f"{TEAM_REGISTRY}/web:bld-0123456789ab"
    assert body == {
        "source": {"connectedRepository": {"repository": SOURCE_REPOSITORY, "revision": COMMIT}},
        "steps": [{"name": "gcr.io/cloud-builders/docker", "args": ["build", "--build-arg", "TARGETARCH=amd64", "-t", tag, "-f", "web/Dockerfile", "web"]}],
        "images": [tag],
        "serviceAccount": f"projects/infra-project/serviceAccounts/{DEV_BUILD_SA}",
        "options": {"logging": "CLOUD_LOGGING_ONLY", "requestedVerifyOption": "VERIFIED", "machineType": "E2_STANDARD_2"},
        "timeout": "1200s",
        "tags": ["appdelivery", "shop-team", "dev", "bld-0123456789ab"],
    }
    assert "fix rounding" not in json.dumps(body), "the note is the service's record, never part of the build"
    assert (source.project, source.region) == ("infra-project", "us-central1")


@pytest.mark.parametrize("status,expected", [("QUEUED", "QUEUED"), ("PENDING", "QUEUED"), ("WORKING", "WORKING"), ("FAILURE", "FAILED"), ("TIMEOUT", "FAILED"),
                                             ("CANCELLED", "FAILED"), ("INTERNAL_ERROR", "FAILED"), ("EXPIRED", "FAILED")])
def test_cloud_builds_statuses_as_this_service_reports_them(status, expected):
    found, digest, error = cloudbuild.outcome({"status": status}, "t")
    assert found == expected and digest is None and (error is not None) == (expected == "FAILED")
    if error:
        assert error["code"] == "BUILD_FAILED"


def test_a_success_counts_only_with_a_digest_for_the_image_asked_for():
    tag = f"{TEAM_REGISTRY}/web:bld-0123456789ab"
    assert cloudbuild.outcome({"status": "SUCCESS", "results": {"images": [{"name": tag, "digest": DIGEST_BUILT}]}}, tag) == ("SUCCEEDED", DIGEST_BUILT, None)
    for results in ({}, {"images": [{"name": f"{TEAM_REGISTRY}/other:x", "digest": DIGEST_BUILT}]}, {"images": [{"name": tag, "digest": "sha256:abc"}]}):
        status, digest, error = cloudbuild.outcome({"status": "SUCCESS", "results": results}, tag)
        assert status == "FAILED" and digest is None and error["code"] == "BUILD_FAILED"


def cloud_build_client(handler, seen):
    def handle(request):
        seen.append(request)
        return handler(request)
    return cloudbuild.CloudBuildClient(lambda: "env-token", httpx.Client(transport=httpx.MockTransport(handle)))


def test_the_client_creates_and_reads_regional_builds_as_the_environment():
    seen = []
    client = cloud_build_client(lambda request: httpx.Response(200, json={"name": "operations/x", "metadata": {"build": {"id": "cb-1", "status": "QUEUED"}}})
                                if request.method == "POST" else httpx.Response(200, json={"id": "cb-1", "status": "WORKING"}), seen)
    assert client.create("infra-project", "us-central1", {"steps": []})["id"] == "cb-1"
    assert client.get("infra-project", "us-central1", "cb-1")["status"] == "WORKING"
    post, get = seen
    assert str(post.url) == "https://cloudbuild.googleapis.com/v1/projects/infra-project/locations/us-central1/builds?projectId=infra-project"
    assert str(get.url) == "https://cloudbuild.googleapis.com/v1/projects/infra-project/locations/us-central1/builds/cb-1"
    assert post.headers["Authorization"] == "Bearer env-token" and json.loads(post.content) == {"steps": []}


@pytest.mark.parametrize("status,code", [(403, ErrorCode.BUILD_UNAVAILABLE), (401, ErrorCode.BUILD_UNAVAILABLE), (400, ErrorCode.BUILD_FAILED),
                                         (404, ErrorCode.BUILD_FAILED)])
def test_the_clients_refusals_have_codes(status, code):
    client = cloud_build_client(lambda request: httpx.Response(status, json={"error": {"message": "no"}}), [])
    with pytest.raises(DeliveryError) as caught:
        client.create("infra-project", "us-central1", {})
    assert caught.value.code == code
    if code == ErrorCode.BUILD_UNAVAILABLE:
        assert "roles/cloudbuild.builds.editor" in caught.value.message and "serviceAccountUser" in caught.value.message


def test_an_operation_without_a_build_id_is_not_taken_for_a_build():
    client = cloud_build_client(lambda request: httpx.Response(200, json={"name": "operations/x", "metadata": {}}), [])
    with pytest.raises(DeliveryError) as caught:
        client.create("infra-project", "us-central1", {})
    assert caught.value.code == ErrorCode.BUILD_UNAVAILABLE


def test_an_unreachable_cloud_build_is_unavailable():
    def handle(request):
        raise httpx.ConnectError("no route")
    client = cloudbuild.CloudBuildClient(lambda: "t", httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(DeliveryError) as caught:
        client.get("infra-project", "us-central1", "cb-1")
    assert caught.value.code == ErrorCode.BUILD_UNAVAILABLE


# ---- building, end to end
def test_a_build_is_ordered_followed_to_its_end_and_its_digest_recorded():
    world = World()
    build = world.delivery.build(request(), world.team)
    assert build.status == "SUCCEEDED" and build.built == BUILT and build.image == f"{TEAM_REGISTRY}/web:{build.build_id}"
    assert build.cloud_build_id == "cb-0001" and build.log_url.endswith("/cb-0001") and build.requested_by == "shop-team" and build.error is None
    [created] = world.cloud_build.created
    assert (created["project"], created["region"], created["environment"]) == ("infra-project", "us-central1", "dev")
    assert created["body"]["serviceAccount"].endswith(DEV_BUILD_SA)
    assert [item.status for item in world.delivery.list_builds(world.team, source="shop-web")][0] == "SUCCEEDED"


def test_each_environment_builds_as_its_own_account_with_its_own_settings():
    world = World()
    world.delivery.build(request(environment="prod"), world.team)
    body = world.cloud_build.created[0]["body"]
    assert body["serviceAccount"].endswith(PROD_BUILD_SA) and body["options"]["machineType"] == "E2_HIGHCPU_8" and body["timeout"] == "1800s"


def test_what_a_build_produced_is_released_and_the_release_names_its_build():
    world = World()
    build = world.delivery.build(request(), world.team)
    planned = world.delivery.plan(world.release(build.built), world.team)
    assert planned.build_id == build.build_id
    released = world.deploy(build.built)
    assert released.status == "RUNNING" and released.build_id == build.build_id and world.fleet.app["shop-web-1"] == BUILT


def test_an_image_pushed_into_the_teams_own_registry_from_outside_is_never_released():
    world = World()
    world.delivery.build(request(), world.team)
    pushed = f"{TEAM_REGISTRY}/web@sha256:" + "e" * 64  # same registry, same name, a digest no build of this service reported
    for attempt in (lambda: world.delivery.plan(world.release(pushed), world.team), lambda: world.deploy(pushed)):
        with pytest.raises(DeliveryError) as caught:
            attempt()
        assert caught.value.code == ErrorCode.NOT_BUILT_HERE and "build_image" in caught.value.message
    assert world.fleet.calls == [] and world.infra.reads == 0, "refused before the infrastructure layer is even read"


def test_another_teams_build_is_not_this_teams_to_release():
    world = World()
    theirs = f"{TEAM_REGISTRY}/web@sha256:" + "f" * 64
    world.built(theirs, infra_owner="other-team")
    with pytest.raises(DeliveryError) as caught:
        world.deploy(theirs)
    assert caught.value.code == ErrorCode.NOT_BUILT_HERE


def test_a_failed_build_records_why_and_leaves_nothing_to_release():
    world = World(cloud_build=FakeCloudBuild(script=("WORKING", "FAILURE")))
    build = world.delivery.build(request(), world.team)
    assert build.status == "FAILED" and build.built is None and build.error["code"] == "BUILD_FAILED" and "non-zero status" in build.error["message"]
    with pytest.raises(DeliveryError) as caught:
        world.deploy(f"{TEAM_REGISTRY}/web@{DIGEST_BUILT}")
    assert caught.value.code == ErrorCode.NOT_BUILT_HERE


def test_a_success_without_the_images_digest_is_a_failed_build():
    world = World(cloud_build=FakeCloudBuild(no_digest=True))
    build = world.delivery.build(request(), world.team)
    assert build.status == "FAILED" and build.built is None and "no digest" in build.error["message"]


def test_who_may_build_what():
    world = World()
    with pytest.raises(DeliveryError) as caught:
        world.delivery.build(request(environment="prod"), world.oncall)
    assert caught.value.code == ErrorCode.PERMISSION_DENIED, "approving is not building"
    with pytest.raises(DeliveryError) as caught:
        world.delivery.build(request(source="other-app"), world.team)
    assert caught.value.code == ErrorCode.NOT_FOUND and "other-team" not in caught.value.message
    with pytest.raises(DeliveryError) as caught:
        world.delivery.build(request(source="nothing-here"), world.team)
    assert caught.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(DeliveryError) as caught:
        world.delivery.build(request(environment="qa"), world.team)
    assert caught.value.code == ErrorCode.UNKNOWN_ENVIRONMENT
    world.config.environments["dev"].build = None
    with pytest.raises(DeliveryError) as caught:
        world.delivery.build(request(), world.team)
    assert caught.value.code == ErrorCode.INVALID_BUILD
    assert world.cloud_build.created == [], "nothing refused reached Cloud Build"


def test_a_source_whose_registry_the_environment_does_not_allow_is_refused():
    world = World()
    world.config.environments["dev"].allowed_registries = ["us-central1-docker.pkg.dev/acme-apps"]
    with pytest.raises(DeliveryError) as caught:
        world.delivery.build(request(), world.team)
    assert caught.value.code == ErrorCode.REGISTRY_NOT_ALLOWED and world.cloud_build.created == []


def test_a_refused_build_request_at_cloud_build_is_passed_on_with_its_code():
    world = World(cloud_build=FakeCloudBuild(fail_create=DeliveryError(ErrorCode.BUILD_UNAVAILABLE, "refused")))
    before = len(world.state.list_builds("shop-team", limit=200))
    with pytest.raises(DeliveryError) as caught:
        world.delivery.build(request(), world.team)
    assert caught.value.code == ErrorCode.BUILD_UNAVAILABLE and len(world.state.list_builds("shop-team", limit=200)) == before, "nothing recorded"


def test_another_teams_build_is_not_found():
    world = World()
    build = world.delivery.build(request(), world.team)
    other = world.team.model_copy(update={"name": "other", "infra_owner": "other-team"})
    with pytest.raises(DeliveryError) as caught:
        world.delivery.get_build(build.build_id, other)
    assert caught.value.code == ErrorCode.NOT_FOUND
    assert world.delivery.list_builds(other) == []


# ---- following, restarts
class Held:
    """An executor that keeps what was submitted, as a pool would before it runs."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args):
        self.submitted.append((fn, args))

    def shutdown(self, wait=True, cancel_futures=False):
        pass


def builder(state, cloud, executor):
    config = make_config()
    return Builder(config, state, cloud, executor, sleep=lambda seconds: None), principal(config)


def test_a_build_left_unfinished_by_a_restart_is_followed_again():
    state, cloud = StateManager(":memory:"), FakeCloudBuild(script=("WORKING", "WORKING", "SUCCESS"))
    first, team = builder(state, cloud, Held())
    build = first.build(request(), team)
    assert build.status == "QUEUED" and len(first.executor.submitted) == 1, "the follower was never run: the service stopped"
    second, _ = builder(state, cloud, InlineExecutor())
    assert second.resume() == [build.build_id]
    assert state.get_build(build.build_id)["status"] == "SUCCEEDED" and state.get_build(build.build_id)["built"] == BUILT


def test_reading_a_build_asks_cloud_build_when_nobody_follows_it():
    state, cloud = StateManager(":memory:"), FakeCloudBuild(script=("WORKING", "SUCCESS"))
    held, team = builder(state, cloud, Held())
    build = held.build(request(), team)
    assert held.get(build.build_id, team).status == "WORKING"
    assert held.get(build.build_id, team).built == BUILT
    calls = len(cloud.gets)
    assert held.get(build.build_id, team).status == "SUCCEEDED" and len(cloud.gets) == calls, "a final status is not asked for again"


def test_a_follower_gives_up_on_a_silent_cloud_build_and_a_read_picks_it_up(caplog):
    caplog.set_level(logging.INFO, logger="appdelivery")
    state, cloud = StateManager(":memory:"), FakeCloudBuild(script=("SUCCESS",), fail_get=6)
    follower, team = builder(state, cloud, InlineExecutor())
    build = follower.build(request(), team)
    assert build.status == "QUEUED" and "build_follow_stopped" in caplog.text
    assert follower.get(build.build_id, team).status == "SUCCEEDED"


def test_a_stopping_service_stops_following():
    state, cloud = StateManager(":memory:"), FakeCloudBuild(script=("WORKING",))
    stopped, team = builder(state, cloud, InlineExecutor())
    stopped.stopping.set()
    assert stopped.build(request(), team).status == "QUEUED" and cloud.gets == []


def test_no_token_reaches_the_build_the_record_or_the_log(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="appdelivery")
    world = World(database=str(tmp_path / "delivery.db"))
    world.delivery.build(request(), world.team)
    dump = "\n".join(sqlite3.connect(tmp_path / "delivery.db").iterdump())
    for text in (dump, caplog.text, json.dumps(world.cloud_build.created)):
        assert "dev-token" not in text and "prod-token" not in text


def test_the_service_status_names_each_environments_build_account():
    world = World()
    status = world.delivery.service_status("x")
    assert status["environments"]["dev"]["build"] == {"service_account": DEV_BUILD_SA, "machine_type": "E2_STANDARD_2"} and status["building"] == 0
    assert V1 in {record["built"] for record in world.state.list_builds("shop-team", limit=200)}
