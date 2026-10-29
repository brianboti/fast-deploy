"""Releases from start to end on a fake fleet: rollout one machine at a time, undo on failure, approval, rollback,
remove, reading, and recovery after a restart (docs/DELIVERY-SPEC.md sections 5 and 7)."""
import json
import logging
import sqlite3

import pytest
from delivery_fakes import Runner, principal
from fleet_fakes import SECRET_VALUE, V1, V2, V3, World

from appdelivery.errors import DeliveryError, ErrorCode
from appdelivery.remote import Completed
from appdelivery.state import ProcessLock

WEB = ("shop-web-1", "shop-web-2")


def statuses(response):
    return [target.status for target in response.targets]


def test_a_release_rolls_out_to_every_machine_one_at_a_time():
    world = World()
    response = world.deploy()
    assert response.status == "RUNNING" and statuses(response) == ["HEALTHY", "HEALTHY"]
    assert response.previous is None and response.can_roll_back is False and response.requested_by == "shop-team"
    assert [world.fleet.app[name] for name in WEB] == [V1, V1]
    deploys = [call for call in world.fleet.calls if call["action"] == "deploy"]
    assert [call["machine"] for call in deploys] == list(WEB) and {call["environment"] for call in deploys} == {"dev"}
    assert deploys[0]["env_text"] == f"LOG_LEVEL=info\nDB_HOST=10.20.0.5\nDB_PASSWORD={SECRET_VALUE}\n" and deploys[0]["token"] == "dev-token"
    assert response.env == {"LOG_LEVEL": "value", "DB_HOST": "endpoint database_host", "DB_PASSWORD": "secret db-password"}


def test_no_secret_value_reaches_the_database_the_log_or_an_answer(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="appdelivery")
    world = World(database=str(tmp_path / "delivery.db"))
    world.fleet.unhealthy.add(V2)
    first = world.deploy()
    failed = world.deploy(V2)
    assert failed.status == "FAILED" and "[redacted]" in failed.targets[0].log and SECRET_VALUE not in failed.targets[0].log
    dump = "\n".join(sqlite3.connect(tmp_path / "delivery.db").iterdump())
    for text in (dump, caplog.text, first.model_dump_json(), failed.model_dump_json(), json.dumps(world.delivery.running(first.request_id, world.team))):
        assert SECRET_VALUE not in text
    assert "dev-token" not in dump and "dev-token" not in caplog.text


def test_a_second_release_replaces_the_first_which_it_can_roll_back_to():
    world = World()
    first = world.deploy()
    assert world.delivery.plan(world.release(V2), world.team).replaces == first.release_id
    second = world.deploy(V2)
    assert second.status == "RUNNING" and second.previous == first.release_id and second.can_roll_back
    assert world.delivery.get(first.release_id, world.team).status == "SUPERSEDED"
    assert [world.fleet.previous[name] for name in WEB] == [V1, V1]


def test_a_machine_that_fails_its_health_check_stops_the_rollout_and_the_done_ones_go_back():
    world = World()
    first = world.deploy()

    def second_machine_unhealthy():
        original = world.fleet.session

        def session(environment):
            fake = original(environment)
            deploy = fake.deploy

            def flaky(machine, release_id, release, env_text, token):
                if machine.name == "shop-web-2":
                    world.fleet.unhealthy.add(release.image)
                return deploy(machine, release_id, release, env_text, token)
            fake.deploy = flaky
            return fake
        world.delivery.machines = session
    second_machine_unhealthy()
    failed = world.deploy(V2)
    assert failed.status == "FAILED" and statuses(failed) == ["RESTORED", "UNHEALTHY"]
    assert failed.error["code"] == "ROLLOUT_FAILED" and failed.error["details"]["machine"] == "shop-web-2" and failed.error["details"]["undone"] == ["shop-web-1"]
    assert failed.targets[1].error["code"] == "UNHEALTHY" and "previous container runs again" in failed.targets[1].error["message"]
    assert [world.fleet.app[name] for name in WEB] == [V1, V1], "the group is back on one version"
    assert world.delivery.get(first.release_id, world.team).status == "RUNNING"


def test_a_groups_first_release_that_fails_leaves_the_group_empty():
    world = World()
    world.fleet.unreachable.add("shop-web-2")
    failed = world.deploy()
    assert failed.status == "FAILED" and statuses(failed) == ["REMOVED", "FAILED"]
    assert failed.targets[1].error["code"] == "MACHINE_UNREACHABLE" and world.fleet.app["shop-web-1"] is None


def test_an_unreachable_first_machine_touches_nothing_else():
    world = World()
    world.fleet.unreachable.add("shop-web-1")
    failed = world.deploy()
    assert statuses(failed) == ["FAILED", "SKIPPED"] and [call["machine"] for call in world.fleet.calls] == ["shop-web-1"]


def test_a_failed_pull_changes_nothing_and_says_so():
    world = World()
    world.deploy()
    world.fleet.pull_fails.add(V2)
    failed = world.deploy(V2)
    assert statuses(failed) == ["FAILED", "SKIPPED"] and failed.targets[0].error["code"] == "PULL_FAILED"
    assert "nothing changed there" in failed.targets[0].error["message"] and world.fleet.app["shop-web-1"] == V1


def test_production_waits_for_another_principal_and_verifies_the_image_first():
    world = World()
    world.infra.answers["prod"]["access"][0]["member"] = f"serviceAccount:{world.config.environments['prod'].service_account}"
    waiting = world.deploy(environment="prod")
    assert waiting.status == "AWAITING_APPROVAL" and statuses(waiting) == ["PENDING", "PENDING"] and world.fleet.calls == []
    with pytest.raises(DeliveryError) as caught:
        world.delivery.approve(waiting.release_id, world.team)
    assert caught.value.code == ErrorCode.PERMISSION_DENIED
    approved = world.delivery.approve(waiting.release_id, world.oncall)
    assert approved.status == "RUNNING" and approved.approved_by == "shop-oncall"
    assert world.cosign.calls[0]["argv"][:2] == ["cosign", "verify-attestation"] and world.cosign.calls[0]["docker_config"]["auths"]
    assert {call["environment"] for call in world.fleet.calls} == {"prod"}


def test_a_release_waiting_for_approval_blocks_another_on_the_same_group():
    world = World()
    world.deploy(environment="prod")
    with pytest.raises(DeliveryError) as caught:
        world.deploy(V2, environment="prod")
    assert caught.value.code == ErrorCode.INVALID_STATE and "one release moves at a time" in caught.value.message


def test_an_image_without_a_trusted_attestation_never_reaches_a_machine():
    world = World(cosign=Runner(Completed(1, "", "no matching attestations")))
    waiting = world.deploy(environment="prod")
    failed = world.delivery.approve(waiting.release_id, world.oncall)
    assert failed.status == "FAILED" and failed.error["code"] == "ATTESTATION_FAILED" and statuses(failed) == ["SKIPPED", "SKIPPED"] and world.fleet.calls == []


def test_everything_is_checked_again_when_the_rollout_starts():
    world = World()
    waiting = world.deploy(environment="prod")
    world.infra.answers["prod"]["access"] = []  # the team withdrew the grant while the release waited
    failed = world.delivery.approve(waiting.release_id, world.oncall)
    assert failed.status == "FAILED" and failed.error["code"] == "NOT_GRANTED" and world.fleet.calls == []


def test_machines_replaced_since_the_release_was_recorded_are_not_deployed_to():
    world = World()
    waiting = world.deploy(environment="prod")
    world.infra.answers["prod"]["resources"]["compute"][1]["id"] = "cmp-999999999999"
    world.infra.answers["prod"]["access"][0]["resources"].append("cmp-999999999999")
    failed = world.delivery.approve(waiting.release_id, world.oncall)
    assert failed.error["code"] == "INVALID_STATE" and "changed since" in failed.error["message"] and world.fleet.calls == []


def test_rollback_puts_the_previous_release_back_and_cannot_be_repeated():
    world = World()
    first = world.deploy()
    second = world.deploy(V2)
    rolled = world.delivery.rollback(second.release_id, world.team)
    assert rolled.status == "ROLLED_BACK" and statuses(rolled) == ["ROLLED_BACK", "ROLLED_BACK"] and not rolled.can_roll_back
    restored = world.delivery.get(first.release_id, world.team)
    assert restored.status == "RUNNING" and not restored.can_roll_back
    assert [world.fleet.app[name] for name in WEB] == [V1, V1]
    for release_id in (first.release_id, second.release_id):
        with pytest.raises(DeliveryError) as caught:
            world.delivery.rollback(release_id, world.team)
        assert caught.value.code == ErrorCode.INVALID_STATE


def test_a_groups_first_release_has_nothing_to_roll_back_to():
    world = World()
    first = world.deploy()
    with pytest.raises(DeliveryError) as caught:
        world.delivery.rollback(first.release_id, world.team)
    assert "deploy the version you want" in caught.value.message


def test_a_rollback_that_a_machine_refuses_is_reported_and_leaves_the_release_marked():
    world = World()
    world.deploy()
    second = world.deploy(V2)
    world.fleet.previous["shop-web-2"] = None  # someone removed it by hand
    failed = world.delivery.rollback(second.release_id, world.team)
    assert failed.status == "ROLLBACK_FAILED" and statuses(failed) == ["ROLLED_BACK", "UNDO_FAILED"] and "shop-web-2" in failed.error["message"]


def test_remove_empties_the_machines():
    world = World()
    first = world.deploy()
    removed = world.delivery.remove(first.release_id, world.team)
    assert removed.status == "REMOVED" and statuses(removed) == ["REMOVED", "REMOVED"] and all(world.fleet.app[name] is None for name in WEB)
    assert world.delivery.running(first.request_id, world.team)["groups"] == []


def test_another_team_sees_nothing_of_these_releases():
    world = World()
    first = world.deploy()
    data = world.config.model_dump()
    data["principals"].append({"name": "pay-team", "token_sha256": "2" * 64, "infra_owner": "pay-team", "environments": ["dev"], "can_deploy": True})
    other = principal(type(world.config).model_validate(data), "pay-team")
    for call in (lambda: world.delivery.get(first.release_id, other), lambda: world.delivery.rollback(first.release_id, other),
                 lambda: world.delivery.remove(first.release_id, other)):
        with pytest.raises(DeliveryError) as caught:
            call()
        assert caught.value.code == ErrorCode.NOT_FOUND
    assert world.delivery.list_releases(other) == [] and world.delivery.running(first.request_id, other)["groups"] == []


def test_list_and_running_show_what_runs_where():
    world = World()
    first = world.deploy()
    second = world.deploy(V2)
    assert [item.release_id for item in world.delivery.list_releases(world.team)] == [second.release_id, first.release_id]
    running = world.delivery.running(first.request_id, world.team, live=True)
    group = running["groups"][0]
    assert group["release_id"] == second.release_id and group["image"] == V2
    assert [(item["name"], item["running"], item["matches"]) for item in group["machines"]] == [("shop-web-1", True, True), ("shop-web-2", True, True)]
    world.fleet.app["shop-web-2"] = V3  # changed by hand
    assert [item["matches"] for item in world.delivery.running(first.request_id, world.team, live=True)["groups"][0]["machines"]] == [True, False]


def test_a_restart_marks_moving_releases_as_interrupted():
    world = World()
    first = world.deploy()
    second = world.deploy(V2)
    world.state.transition(second.release_id, ("RUNNING",), "ROLLING_BACK")
    world.state.set_target(second.release_id, second.targets[0].resource, "DEPLOYING")
    found = world.delivery.recover()
    assert found == [{"release_id": second.release_id, "was": "ROLLING_BACK", "now": "ROLLBACK_FAILED"}]
    after = world.delivery.get(second.release_id, world.team)
    assert after.error["details"]["cause"] == "INTERRUPTED" and after.targets[0].status == "FAILED"
    assert world.delivery.get(first.release_id, world.team).status == "SUPERSEDED"


def test_an_unexpected_failure_in_a_worker_becomes_a_failed_release():
    world = World()

    def broken(environment):
        raise RuntimeError("boom")
    world.delivery.machines = broken
    failed = world.deploy()
    assert failed.status == "FAILED" and failed.error["code"] == "INTERNAL_ERROR" and "RuntimeError" in failed.error["message"]


def test_the_planner_refusals_reach_the_caller_before_anything_is_recorded():
    world = World()
    world.infra.answers["dev"]["status"] = "STOPPED"
    with pytest.raises(DeliveryError) as caught:
        world.deploy()
    assert caught.value.code == ErrorCode.INVALID_STATE and world.delivery.list_releases(world.team) == []


def test_one_process_per_state_file(tmp_path):
    first, second = ProcessLock(str(tmp_path / "delivery.db")), ProcessLock(str(tmp_path / "delivery.db"))
    first.acquire()
    with pytest.raises(RuntimeError, match="one process per state file"):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


def test_a_failed_rollout_that_touched_a_machine_takes_away_the_running_releases_rollback():
    """Found on a real machine on 2026-10-29: putting a machine back starts its previous container, so the machine keeps
    nothing older, and a rollback of the release running again would fail there."""
    world = World()
    world.deploy()
    second = world.deploy(V2)
    assert second.can_roll_back
    world.fleet.unhealthy.add(V3)
    failed = world.deploy(V3)
    assert failed.status == "FAILED" and world.fleet.previous["shop-web-1"] is None, "the fake spends the previous container as the script does"
    running = world.delivery.get(second.release_id, world.team)
    assert running.status == "RUNNING" and running.can_roll_back is False
    with pytest.raises(DeliveryError) as caught:
        world.delivery.rollback(second.release_id, world.team)
    assert caught.value.code == ErrorCode.INVALID_STATE, "refused up front, not half done on the machines"


def test_a_rollout_that_changed_no_machine_keeps_the_running_releases_rollback():
    world = World()
    world.deploy()
    second = world.deploy(V2)
    world.fleet.pull_fails.add(V3)
    world.deploy(V3)
    assert world.delivery.get(second.release_id, world.team).can_roll_back is True
    world.fleet.unreachable.add("shop-web-1")
    world.deploy(V3)
    assert world.delivery.get(second.release_id, world.team).can_roll_back is True


def test_machines_on_one_network_warn_once():
    world = World()
    world.infra.answers["dev"]["resources"]["network"][0]["firewall_rules"].pop()
    warnings = world.delivery.plan(world.release(), world.team).warnings
    assert len([item for item in warnings if "open only inside" in item]) == 1
