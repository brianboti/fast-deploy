"""What the run of releases S1 and S2 on Google Cloud found on 2026-11-02 (docs/DELIVERY-SPIKE.md section 16), delivery side,
each held by a test that fails without its fix."""
import base64

import pytest
from fleet_fakes import PULL_DENIED, V1, V2, World
from host_fakes import FakeHost
from test_delivery_cluster import deploy as deploy_on_cluster
from test_delivery_host_script import OLD, deploy


@pytest.fixture
def host(tmp_path):
    return FakeHost(tmp_path)


@pytest.mark.parametrize(("mark", "token", "words"), [("fail_pull", None, "downloadArtifacts"), ("fail_pull", "ya29.short", "downloadArtifacts"),
                                                      ("fail_login", "expired", "401 Unauthorized")])
def test_a_failed_login_or_pull_sends_dockers_own_words(host, mark, token, words):
    # The machine's pull account could not read the registry and the release said only "could not pull the image": a
    # refused account and an unreachable registry read alike. docker's last lines now come back, as the health check's do.
    deploy(host, image=OLD, release="rel-000000000001")
    host.mark(mark)
    report = deploy(host, token=token)
    assert report.result == "pull_failed" and words in report.log()
    assert list(host.run_dir.iterdir()) == [], "the error file must be gone"


def test_the_release_shows_why_the_pull_failed():
    world = World()
    world.deploy()
    world.fleet.pull_fails.add(V2)
    failed = world.deploy(V2)
    assert "downloadArtifacts" in (failed.targets[0].log or "")
    assert "log" not in failed.targets[0].error["details"] and world.fleet.app["shop-web-1"] == V1
    assert base64.b64encode(PULL_DENIED.encode()).decode() not in str(failed.targets[0].error)


def test_a_rollout_with_no_room_on_any_node_says_so():
    # Two replicas of 1 GiB on a one-node cluster: one ran, the other found no node, and the release said only that the
    # ReplicaSet "has timed out progressing". The scheduler's reason now comes with it.
    world = World(cluster=True)
    deploy_on_cluster(world)
    world.kube.unschedulable_images.add(V2)
    failed = deploy_on_cluster(world, V2)
    message = failed.targets[0].error["message"]
    assert failed.status == "FAILED" and "timed out progressing" in message and "found no node: 0/1 nodes are available: 1 Insufficient memory" in message
    assert world.kube.image() == V1


@pytest.mark.parametrize(("purpose", "short"), [("database_password", "db-password"), ("cache_auth", "cache-auth")])
def test_the_requests_own_credential_is_bound_by_its_short_name(purpose, short):
    # cloudinfra names them <name>-db-password-<6 hex> and <name>-cache-auth-<6 hex> since 1.37.0; a release binding
    # {secret: db-password} was refused on the run ("the request has no secret 'db-password'; its secrets: api-key,
    # db-password-10ae23"), though DELIVERY-SPEC.md shows exactly that binding.
    from delivery_fakes import infra_response

    from appdelivery.infra import InfraRequest
    data = infra_response()
    data["resources"]["secret"] = [{**data["resources"]["secret"][0], "name": f"shop-{short}-10ae23", "purpose": purpose},
                                   {**data["resources"]["secret"][0], "id": "sec-444444444444", "name": "shop-api-key", "purpose": None}]
    infra = InfraRequest.model_validate(data)
    assert infra.secret(short).name == f"shop-{short}-10ae23" and infra.secret("api-key").name == "shop-api-key"
    assert infra.secret_names() == sorted([short, "api-key"])
