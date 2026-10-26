import copy
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from labdeploy import operations
from labdeploy.controller import Store
from labdeploy.scheduler import Scheduler


def test_migration_preserves_history_without_replaying_old_actions(tmp_path):
    path = tmp_path / "old.db"
    payload = {"id": "a" * 32, "application": "demo", "sha": "b" * 40}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE plans(id TEXT PRIMARY KEY, owner TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL)")
        db.execute("INSERT INTO plans VALUES (?,?,?,?)", (payload["id"], "owner", "dispatching", json.dumps(payload)))
    store = Store(path)
    assert store.fetch(payload["id"])[1]["sha"] == payload["sha"]
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    Store(path)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM operation_events").fetchone()[0] == 1


def test_stale_writer_cannot_reverse_a_completed_plan(setup):
    c, p, _ = setup
    plan = c.plan(p, "demo")
    first, stale = c.store.get(p.name, plan["id"]), c.store.get(p.name, plan["id"])
    assert c.store.finish(p.name, first, "succeeded")
    assert not c.store.finish(p.name, stale, "dispatching")
    assert c.store.get(p.name, plan["id"])["state"] == "succeeded"


def test_only_one_sender_can_claim_even_after_restart(setup):
    c, p, _ = setup
    plan = c.plan(p, "demo")
    c.store.claim(p.name, plan["id"])
    def claim(_):
        try:
            c.store.start_action(plan["id"])
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=16) as pool:
        assert sum(pool.map(claim, range(100))) == 1
    with pytest.raises(ValueError, match="uncertain"):
        Store(c.config.database).start_action(plan["id"])


def test_intent_collision_never_authorizes_mutation():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(412) if request.method == "POST" else httpx.Response(200, json={"different": True})
    intents = operations.Intents("test-bucket", lambda: "test", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError, match="different request"):
        intents.write("a" * 32, {"request": "one"})
    assert calls[0].url.params["ifGenerationMatch"] == "0"


def test_restore_of_missing_dispatch_is_unknown_and_admission_stays_fenced(setup):
    c, p, _ = setup
    request = {"id": "f" * 32, "kind": "deployment", "application": "demo", "environment": "staging", "sha": "a" * 40}
    witness = {"schema": 1, "kind": "release", "owner": p.name, "request": request, "request_hash": operations.digest(request)}
    operations.restore_intents(c.store, [witness])
    assert c.store.recovery_mode()
    assert c.store.get(p.name, request["id"])["state"] == "dispatch_unknown"
    operations.restore_intents(c.store, [witness])
    with c.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM operations WHERE id=?", (request["id"],)).fetchone()[0] == 1
    with pytest.raises(ValueError):
        c.store.start_action(request["id"])
    corrupt = copy.deepcopy(witness)
    corrupt["request"]["sha"] = "b" * 40
    with pytest.raises(ValueError, match="hash mismatch"):
        operations.restore_intents(c.store, [corrupt])
    assert c.store.recovery_mode()


def test_stalled_reporting_does_not_block_follow_and_schedules_survive_restart(setup):
    c, _, _ = setup
    started, release, followed = threading.Event(), threading.Event(), threading.Event()
    scheduler = Scheduler(c, c.config)
    def slow():
        started.set()
        assert release.wait(5)
    scheduler.jobs["digest"] = (slow, lambda: 300, 0)
    scheduler.jobs["follow"] = (followed.set, lambda: 60, 0)
    try:
        assert scheduler.submit("digest")
        assert started.wait(2)
        assert not scheduler.submit("digest")
        assert scheduler.submit("follow")
        assert followed.wait(2)
    finally:
        release.set()
        scheduler.stop()
    restored = Scheduler(c, c.config)
    assert restored.state["follow"]["last_run"] is not None
    assert restored.state["follow"]["next_run"] == scheduler.state["follow"]["next_run"]


def test_follow_batches_rotate_instead_of_starving_old_unknown_operations(setup):
    c, p, _ = setup
    for index in range(25):
        c.store.add(p.name, {"id": f"{index:032x}", "application": "demo"}, "dispatching")
    batches = [set(item[0] for item in c.store.in_flight(limit=10, now=100 + index)) for index in range(3)]
    assert len(set.union(*batches)) == 25


def queue(c, plan_id):
    with c.store.connect() as db:
        return [row[0] for row in db.execute("SELECT state FROM outbox WHERE operation_id=?", (plan_id,))]


def test_a_follow_up_pass_during_sending_does_not_lose_the_dispatch(setup):
    import time
    c, p, gh = setup
    plan = c.plan(p, "demo")
    c.store.mark(plan["id"], expires_at=time.time() + 600)  # planned five minutes ago
    def dispatch(app, sent):
        gh.status = lambda app, plan: {"status": "not_visible_yet"}
        assert c.refresh(p, sent["id"])["state"] != "dispatch_lost"  # the follow-up thread runs while GitHub answers
        return {"workflow_run_id": 123}
    gh.dispatch = dispatch
    c.deploy(p, plan["id"])
    assert c.store.get(p.name, plan["id"])["state"] == "dispatched"


def test_a_confirmed_dispatch_beats_an_earlier_loss(setup):
    c, p, gh = setup
    plan = c.plan(p, "demo")
    def dispatch(app, sent):
        owner, current = c.store.fetch(sent["id"])
        c.store.finish(owner, current, "dispatch_lost")
        return {"workflow_run_id": 123}
    gh.dispatch = dispatch
    c.deploy(p, plan["id"])
    assert c.store.get(p.name, plan["id"])["state"] == "dispatched"


def test_a_failure_before_sending_gives_the_plan_back(setup):
    c, p, gh = setup
    plan = c.plan(p, "demo")
    c.config.durable_intents = True  # no intent bucket configured: prepare_dispatch refuses before anything is sent
    with pytest.raises(ValueError, match="intent bucket"):
        c.deploy(p, plan["id"])
    assert c.store.get(p.name, plan["id"])["state"] == "planned" and queue(c, plan["id"]) == []
    c.config.durable_intents = False
    assert c.deploy(p, plan["id"])["state"] == "dispatched"


def test_every_finished_state_closes_the_queue(setup):
    c, p, _ = setup
    plan = c.plan(p, "demo")
    c.deploy(p, plan["id"])
    owner, current = c.store.fetch(plan["id"])
    c.store.finish(owner, current, "completed_unverified")
    assert queue(c, plan["id"]) == ["completed"]


def test_a_lost_cancellation_answer_can_be_asked_again(setup):
    c, p, gh = setup
    plan = c.plan(p, "demo")
    c.deploy(p, plan["id"])
    gh.status = lambda app, plan: {"id": 123, "status": "in_progress", "conclusion": None, "html_url": "u", "jobs": []}
    calls = []
    def cancel_run(app, run_id):
        calls.append(run_id)
        if len(calls) == 1:
            raise TimeoutError
        return {"cancel_requested": True}
    gh.cancel_run = cancel_run
    with pytest.raises(ValueError, match="harmless"):
        c.cancel(p, plan["id"])
    assert c.cancel(p, plan["id"])["cancel"] == {"cancel_requested": True}
    assert c.cancel(p, plan["id"])["cancel_state"] == "succeeded" and len(calls) == 2  # an accepted one is not sent again
