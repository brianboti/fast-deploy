"""Phase 23: the voice after a release (docs/ROADMAP.md D57 to D61). No test touches GitHub or a cloud."""
import datetime
import hashlib
import json
import time
import types
from pathlib import Path

import httpx
import pytest
from conftest import TARGET, FakeGitHub

from labdeploy import aftercare, chat
from labdeploy.cloud import Observer
from labdeploy.config import Config
from labdeploy.controller import Controller, Store
from labdeploy.status import diagnose

DAY = 86_400
NOW = datetime.datetime(2026, 10, 26, 14, 0, tzinfo=datetime.UTC).timestamp()  # a Monday, after the digest's hour


def config(tmp_path, **extra):
    base = {"live_dispatch": True, "database": str(tmp_path / "db"),
            "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
            "principals": [{"name": "m", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["*"], "can_deploy": True}]}
    base.update(extra)
    return Config.model_validate(base)


def stats(requests, errors=0, p95=100.0):
    return {"requests": requests, "errors_5xx": errors, "error_share": round(errors / requests, 4) if requests else None, "p95_ms": p95,
            "hours": 24, "truncated": False}


def release(plan_id, at, state="succeeded", sha="a" * 40, kind=None, app="demo", revision=None, previous=None, bake="passed", **extra):
    """A recorded release as the controller stores it: switched to 100 percent at `at`, recorded a few minutes later."""
    revision = revision or f"demo-r{plan_id[:8]}"
    receipt = {"state": "succeeded" if state == "succeeded" else extra.pop("receipt_state", "traffic_restored"), "revision": revision,
               "previous_traffic": previous or {"demo-rold": 100}, "bake": {"since": aftercare.iso(at), "result": bake} if bake else {}}
    receipt.update(extra.pop("receipt", {}))
    plan = {"id": plan_id.ljust(32, "0"), "application": app, "sha": sha, "commit": {"message": "Fix the login bug"}, "kind": kind,
            "expires_at": at - 600 + 900, "spec": {"health_path": "/health"}, "target": dict(TARGET), "receipt": receipt,
            "outcome": {"state": state, "completed_at": aftercare.iso(at + 300), "recorded_at": at + 300}}
    plan.update(extra)
    return {**plan, "state": state}


def store_with(tmp_path, *plans):
    store = Store(str(tmp_path / "db"))
    for plan in plans:
        record = {key: value for key, value in plan.items() if key != "state"}
        store.add("m", record, plan["state"])
    return store


class FakeObserver:
    """Reads by window: the new revision's day and the previous revisions' week, from canned numbers."""
    def __init__(self, new=(150, 0, 120.0), before=(900, 1, 110.0), uptime=1.0, errors_lines=0, server_errors=0, fail=False):
        self.new, self.before, self.uptime, self.fail = new, before, uptime, fail
        self.error_lines, self.server_errors = errors_lines, server_errors
        self.asked = []

    def revision_requests(self, target, revisions, start, end, health_path=None):
        if self.fail:
            raise httpx.ConnectError("down")
        self.asked.append((tuple(revisions), start, end, health_path))
        count, errors, latency = self.before if any(name.endswith("old") for name in revisions) else self.new
        return {"requests": [{"status": 500 if index < errors else 200, "latency_ms": latency} for index in range(count)], "truncated": False}

    def uptime_pass_rate(self, target, start, end):
        return self.uptime

    def memory_p95(self, target, revisions, start, end):
        return 0.4 if revisions else None

    def metrics(self, target, minutes=60):
        return {"window_minutes": minutes, "requests": 40, "server_errors": self.server_errors, "uptime_checks_passed": self.uptime,
                "request_latency_ms_p95": 120.0, "max_active_instances": 1, "memory_utilization_p95": 0.3}

    def logs(self, target, minutes, severity, limit):
        return [{"at": "t", "severity": "ERROR", "message": "boom"}] * min(self.error_lines, limit)

    def service(self, target):
        return {"url": "u", "ready": True, "ready_message": None, "ingress": "INGRESS_TRAFFIC_ALL", "latest_ready_revision": "demo-rnew",
                "updated_at": "t", "traffic": [{"revision": "demo-rnew", "percent": 100, "plan_id": None, "commit": None}]}

    def needs(self, target, needs):
        return []


# ---- D57: the verdict
@pytest.mark.parametrize(("new", "before", "uptime", "verdict"), [
    (stats(150, 0, 120), stats(900, 1, 110), 1.0, "same"),
    (stats(150, 6, 120), stats(900, 1, 110), 1.0, "worse"),        # 4% of errors, from 0.1%
    (stats(150, 1, 120), stats(900, 0, 110), 1.0, "same"),         # 0.7% is never called worse: under the 1% floor
    (stats(150, 0, 300), stats(900, 0, 110), 1.0, "worse"),        # p95 above twice the week before
    (stats(150, 0, 60), stats(900, 0, 110), 1.0, "better"),        # p95 down by more than a third, no more errors
    (stats(150, 1, 120), stats(900, 90, 110), 1.0, "better"),      # 10% of errors before, 0.7% now
    (stats(40, 3, 120), stats(900, 0, 110), 1.0, "too_little_traffic"),
    (stats(40, 3, 120), stats(900, 0, 110), 0.95, "worse"),        # the uptime check speaks even when users are few
    (stats(40, 0, 120), None, 1.0, "first"),
])
def test_the_verdict_follows_d57(new, before, uptime, verdict):
    assert aftercare.judge(new, before, uptime)["verdict"] == verdict


def test_the_switch_is_the_last_bake_then_the_last_move_then_the_run_end():
    at = NOW - 2 * DAY
    plan = release("p1", at)
    assert aftercare.switched_at(plan) == at
    del plan["receipt"]["bake"]
    plan["receipt"]["phases"] = [{"state": "promoting", "at": aftercare.iso(at - 600), "step": 10}, {"state": "promoting", "at": aftercare.iso(at), "step": 100}]
    assert aftercare.switched_at(plan) == at
    plan["receipt"]["phases"] = []
    assert aftercare.switched_at(plan) == at + 300
    assert aftercare.previous_revisions(release("p1", at, previous={"demo-rold": 90, "demo-rp1": 10}, revision="demo-rp1")) == ["demo-rold"]


def test_which_releases_are_due_a_report():
    old = release("old", NOW - 9 * DAY)                    # beyond the horizon: no report
    due = release("due", NOW - 2 * DAY)                    # a day has passed
    young = release("young", NOW - 3600)                   # not yet
    replaced = release("repl", NOW - 5 * 3600)             # replaced by `young` before its day was up
    done = release("done", NOW - 3 * DAY, release_report={"verdict": "same"})
    failed = release("fail", NOW - 2 * DAY, state="failed")
    production = release("prod", NOW - 2 * DAY, kind="promotion")
    found = {plan["id"][:4]: replaced_at for plan, replaced_at in aftercare.awaiting_report([old, due, young, replaced, done, failed, production], NOW)}
    assert set(found) == {"due0", "repl", "prod"} and found["repl"] == NOW - 3600 and found["due0"] == NOW - 5 * 3600


def test_a_report_compares_the_first_day_with_the_week_before_without_the_health_path():
    at = NOW - 2 * DAY
    observer = FakeObserver(new=(150, 6, 120.0))
    report = aftercare.release_report(observer, types.SimpleNamespace(**TARGET), release("p1", at), now=NOW)
    assert report["verdict"] == "worse" and report["reasons"][0]["rule"] == "errors" and report["environment"] == "staging"
    (new_names, start, end, health), (old_names, base_start, base_end, _) = observer.asked
    assert new_names == ("demo-rp1",) and health == "/health" and (end - start).total_seconds() == DAY
    assert old_names == ("demo-rold",) and (base_end - base_start).total_seconds() == 7 * DAY and base_end == start
    assert report["new"]["requests"] == 150 and report["baseline"]["requests"] == 900 and report["memory"] == {"new": 0.4, "before": 0.4}
    short = aftercare.release_report(FakeObserver(), types.SimpleNamespace(**TARGET), release("p2", NOW - 2 * DAY), replaced_at=NOW - 2 * DAY + 1200, now=NOW)
    assert short["verdict"] == "replaced"


# ---- the job: stored on the plan, announced at once when worse
def test_the_report_job_stores_the_verdict_and_announces_a_worse_one(tmp_path):
    cfg = config(tmp_path, notify_webhook_url="https://hooks.example/abc", platform={"repository": "lab/platform"})
    store_with(tmp_path, release("bad", NOW - 2 * DAY))
    gh = FakeGitHub()
    issues, comments = [], []
    gh.find_issue = lambda repository, title: ({"number": 7, "url": "u"} if issues else None)
    gh.create_issue = lambda repository, title, body: issues.append(title) or {"number": 7, "url": "https://github.com/lab/platform/issues/7"}
    gh.comment_issue = lambda repository, number, body: comments.append((number, body)) or {"url": f"https://github.com/lab/platform/issues/{number}#c"}
    observer = FakeObserver(new=(150, 6, 120.0))
    c = Controller(cfg, gh, Store(cfg.database), observer)
    posted = []
    c.post_webhook = lambda text: posted.append(text) or {"channel": "webhook", "ok": True}
    assert c.release_report_tick(now=NOW) == {"bad00000": "worse"}
    assert c.release_report_tick(now=NOW) == {}  # stored once
    stored = c.store.fetch("bad".ljust(32, "0"))[1]
    assert stored["release_report"]["verdict"] == "worse" and stored["release_report"]["announced"][0]["ok"]
    assert issues == ["Fast Deploy: today"] and comments[0][0] == 7 and "**`demo` did worse than the version before" in comments[0][1]
    assert posted and "roll demo back" in posted[0]
    assert [e["outcome"] for e in c.store.recent_events(5) if e["kind"] == "release_report"] == ["worse"]
    assert Controller(cfg, gh, Store(cfg.database)).release_report_tick() == {"skipped": "no observer identity: live views are off"}


def test_reads_that_keep_failing_end_in_could_not_be_read(tmp_path):
    cfg = config(tmp_path)
    store_with(tmp_path, release("p1", NOW - 2 * DAY))
    c = Controller(cfg, FakeGitHub(), Store(cfg.database), FakeObserver(fail=True))
    assert c.release_report_tick(now=NOW)["p1000000"].startswith("not read yet (1)")
    assert c.release_report_tick(now=NOW)["p1000000"].startswith("not read yet (2)")
    assert c.release_report_tick(now=NOW) == {"p1000000": "unreadable"}


# ---- D61: the trust numbers
def test_the_trust_numbers_follow_dora():
    t0 = NOW - 10 * DAY
    plans = [
        release("ok1", t0),
        release("bad", t0 + DAY, state="failed", receipt={"state": "traffic_restored", "failed_phase": "baking"}, bake="failed", sha="c" * 40),
        release("again", t0 + DAY + 7200, sha="c" * 40),     # the same commit passed within a day: a suspected false restore
        release("gate", t0 + 2 * DAY, state="failed", receipt={"state": "traffic_restored", "failed_phase": "deploying"}, bake=None),
        release("quiet", t0 + 3 * DAY, bake="inconclusive"),
        release("undone", t0 + 4 * DAY),
        release("fix", t0 + 4 * DAY + 3 * 3600),
        {**release("rb", t0 + 4 * DAY + 3600, kind="rollback"), "from_plan": "undone".ljust(32, "0"),
         "receipt": {"state": "rolled_back"}},
        release("ancient", NOW - 60 * DAY),
    ]
    numbers = aftercare.trust(plans, NOW, 30)["overall"]
    assert numbers["releases"] == 7 and numbers["reached_traffic"] == 6 and numbers["stopped_before_traffic"] == 1
    assert numbers["restored_automatically"] == 1 and numbers["rolled_back_by_a_person"] == 1 and numbers["change_failure_rate"] == 0.333
    assert numbers["recovery_hours_median"] == 2.0  # 2 h after the restore, 2 h after the rollback
    assert numbers["bakes"] == 6 and numbers["inconclusive_share"] == round(1 / 6, 3)
    assert numbers["suspected_false_restores"] == [{"application": "demo", "plan": "bad00000", "sha": "c" * 12, "passed_in": "again000"}]
    line = chat.trust_line(numbers)
    assert line.startswith("Last 30 days: 6 releases reached users, 1 went back on its own, 1 rolled back by a person; 33% needed undoing")
    assert "within 2 h (median)" in line and "1 attempt stopped before reaching anyone" in line


# ---- D60: the digest
def test_the_digest_waits_for_its_hour_writes_once_a_day_and_says_all_quiet(tmp_path):
    cfg = config(tmp_path, platform={"repository": "lab/platform"})
    gh = FakeGitHub()
    comments = []
    gh.find_issue = lambda repository, title: {"number": 3, "url": "u"}
    gh.create_issue = lambda *a: pytest.fail("the issue exists")
    gh.comment_issue = lambda repository, number, body: comments.append(body) or {"url": "https://github.com/lab/platform/issues/3#c"}
    c = Controller(cfg, gh, Store(cfg.database), FakeObserver())
    c.watchdog_problems = lambda now=None: []  # the fake repository pins no workflows; the watchdog's own checks are tested elsewhere
    morning = NOW - 4 * 3600  # 10:00 UTC
    assert c.digest_tick(now=morning) == {"waiting": "until 13:00 UTC"}
    first = c.digest_tick(now=NOW)
    assert first == {"day": "2026-10-26", "attention": False, "sent_by": ["issue"]} and c.digest_tick(now=NOW + 60) == {"done": "2026-10-26"}
    assert comments[0].startswith("**All quiet, 26 Oct 2026.**") and "2 services checked" not in comments[0]
    assert "1 service checked: no server errors, no error lines, every uptime check passed" in comments[0]
    assert "Last 30 days: no releases recorded" in comments[0]  # Monday: the trust numbers
    assert "**Next:** nothing to do." in comments[0] and "written by code" in comments[0]
    assert Controller(config(tmp_path, digest_hour_utc=None), gh, Store(cfg.database)).digest_tick() == {"off": "digest_hour_utc is null"}


def test_a_bad_day_names_each_thing_to_look_at(tmp_path):
    cfg = config(tmp_path, platform={"repository": "lab/platform"})
    restored = release("bad", NOW - 3 * 3600, state="failed", receipt={"state": "traffic_restored", "failed_phase": "baking"})
    reported = release("rep", NOW - 3 * DAY, release_report={"verdict": "worse", "generated_at": aftercare.iso(NOW - 3600), "environment": "staging",
                                                             "reasons": [{"rule": "errors", "new": 0.04, "before": 0.001}]})
    store_with(tmp_path, restored, reported)
    gh = FakeGitHub()
    c = Controller(cfg, gh, Store(cfg.database), FakeObserver(errors_lines=250, server_errors=12, uptime=0.96))
    record = c.build_digest(NOW)
    text = chat.digest(record)
    assert record["attention"] and text.startswith("**")
    assert "`demo` `aaaaaaa` (Fix the login bug) went back to the previous version on its own (it failed its watch with real traffic)" in text
    assert "a day later it did worse than the version before: 4% of its requests answered with a server error, against 0.1% before" in text
    assert "`demo`, last day: 12 server errors, 200 or more error lines in its log, the uptime check passed 96.0% of the time" in text
    assert "**Next:** ask me about any line above; nothing in it was changed by itself." in text
    assert "Google's own spend cap is not recorded for `test-project`" in text


def test_a_digest_that_reaches_no_one_becomes_a_watchdog_problem(tmp_path):
    cfg = config(tmp_path)  # no platform repository, no webhook: nowhere to send it
    c = Controller(cfg, FakeGitHub(), Store(cfg.database))
    result = c.digest_tick(now=NOW)
    assert result["sent_by"] == [] and c.store.digest("2026-10-26")["delivered"][0]["error"].startswith("platform.repository is not set")
    assert any("reached no one" in problem for problem in c.watchdog_problems(NOW))
    c.started = NOW - 4 * DAY
    assert any("was last written on 2026-10-26" in problem for problem in c.watchdog_problems(NOW + 3 * DAY))
    refused = FakeGitHub()
    request = httpx.Request("POST", "https://api.github.com/repos/lab/platform/issues")
    refused.find_issue = lambda repository, title: None

    def deny(*args):
        raise httpx.HTTPStatusError("403", request=request, response=httpx.Response(403, request=request))
    refused.create_issue = deny
    refused.comment_issue = deny
    sent = Controller(config(tmp_path, platform={"repository": "lab/platform"}), refused, Store(cfg.database)).post_issue("hello")
    assert not sent["ok"] and "Issues: read and write" in sent["error"]


def test_the_watchdog_leaves_its_findings_to_the_digest(tmp_path):
    cfg = config(tmp_path, notify_webhook_url="https://hooks.example/abc")
    c = Controller(cfg, FakeGitHub(), Store(cfg.database))
    c.post_webhook = lambda text: pytest.fail("the digest carries the watchdog's findings")
    c.watchdog_problems = lambda now=None: ["something"]
    assert c.watchdog_tick()["notified"] is False


def test_get_digest_builds_one_for_an_identity_that_sees_only_some_applications(tmp_path):
    cfg = config(tmp_path, applications={"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET},
                                         "other": {"repository": "lab/other", "service": "other", "target": {**TARGET, "service": "other"}}},
                 principals=[{"name": "m", "token_sha256": hashlib.sha256(b"t").hexdigest(), "applications": ["demo"], "can_deploy": False}])
    c = Controller(cfg, FakeGitHub(), Store(cfg.database), FakeObserver())
    view = c.digest_view(cfg.principals[0])
    assert view["kept"] is False and [day["application"] for day in view["services"]] == ["demo"] and view["problems"] == []
    assert chat.digest_answer(view).endswith("Built just now on request; the controller sends one a day at its hour.")


# ---- drills (23.6)
def test_drills_are_reminded_where_they_apply_and_quiet_once_recorded(tmp_path):
    plain = Controller(config(tmp_path), FakeGitHub(), Store(str(tmp_path / "db")))
    assert plain.drill_reminders(NOW) == []  # no cloud, no production: nothing to drill
    cfg = config(tmp_path, observer={"service_account": "lab-deploy-observer@test-project.iam.gserviceaccount.com"})
    c = Controller(cfg, FakeGitHub(), Store(cfg.database))
    lines = c.drill_reminders(NOW)
    assert len(lines) == 1 and lines[0].startswith("the monthly proof that alerts reach you is due (none recorded yet): scripts/alert-drill.sh")
    c.store.record_event("drill", name="alerts", outcome="ok")
    assert c.drill_reminders() == []
    import subprocess
    import sys
    (tmp_path / "c.yaml").write_text(json.dumps(cfg.model_dump(mode="json")))  # JSON is YAML
    done = subprocess.run([sys.executable, "-m", "labdeploy.cli", "record-drill", "cap_fire", "ok", "--detail", "closed 1, reopened"],
                          env={"LAB_CONFIG": str(tmp_path / "c.yaml"), "PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
                          cwd=Path(__file__).parent.parent)
    assert done.returncode == 0, done.stderr
    assert "next one is due in 183 days" in done.stdout and c.store.newest_event("drill", "ok", "cap_fire")["detail"] == "closed 1, reopened"


# ---- the observer's reads
def observer(handler):
    cfg = types.SimpleNamespace(observer=types.SimpleNamespace(service_account="lab-deploy-observer@test-project.iam.gserviceaccount.com", cache_seconds=300))
    found = Observer(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))
    found.token = lambda: "token"
    return found


def test_the_request_log_is_read_without_the_health_path_page_by_page():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        entries = [{"httpRequest": {"status": 200, "latency": "0.120s", "requestUrl": "https://demo.run.app/api?x=1"}},
                   {"httpRequest": {"status": 503, "latency": "1.5s", "requestUrl": "https://demo.run.app/"}},
                   {"httpRequest": {"status": 200, "latency": "0.001s", "requestUrl": "https://demo.run.app/health"}}]
        return httpx.Response(200, json={"entries": entries, **({} if body.get("pageToken") else {"nextPageToken": "next"})})
    start = datetime.datetime(2026, 10, 20, tzinfo=datetime.UTC)
    read = observer(handler).revision_requests(types.SimpleNamespace(**TARGET), ["demo-r1", "demo-r2"], start, start + datetime.timedelta(days=1), "/health")
    assert read == {"requests": [{"status": 200, "latency_ms": 120.0}, {"status": 503, "latency_ms": 1500.0}] * 2, "truncated": False}
    flt = bodies[0]["filter"]
    assert 'NOT httpRequest.requestUrl=~"^https?://[^/]+/health([?].*)?$"' in flt and "run.googleapis.com%2Frequests" in flt
    assert '(resource.labels.revision_name="demo-r1" OR resource.labels.revision_name="demo-r2")' in flt and 'timestamp<"2026-10-21T00:00:00Z"' in flt
    capped = observer(handler).revision_requests(types.SimpleNamespace(**TARGET), ["demo-r1"], start, start + datetime.timedelta(days=1), "/health", cap=3)
    assert capped["truncated"] and len(capped["requests"]) == 3
    with pytest.raises(ValueError, match="health path"):
        observer(handler).revision_requests(types.SimpleNamespace(**TARGET), ["demo-r1"], start, start, '/x" OR true')


def test_the_error_budget_says_when_its_errors_leave_the_window_and_counts_alert_slots():
    def handler(request):
        path = request.url.path
        if path.endswith("/alertPolicies"):
            return httpx.Response(200, json={"alertPolicies": [{"enabled": True}, {"enabled": False}]})
        if path.endswith("/uptimeCheckConfigs"):
            return httpx.Response(200, json={"uptimeCheckConfigs": [{"period": "300s"}, {"period": "60s", "selectedRegions": ["USA_OREGON", "USA_IOWA", "USA_VIRGINIA"]}]})
        if path.endswith("/serviceLevelObjectives/availability"):
            # The shape the Monitoring API returns, read from production's objective on 2026-10-21 (1.29.2).
            return httpx.Response(200, json={"goal": 0.995, "rollingPeriod": "2592000s", "serviceLevelIndicator": {"requestBased": {"goodTotalRatio": {
                "badServiceFilter": 'metric.type="logging.googleapis.com/user/lab-deploy/demo/server-errors"',
                "totalServiceFilter": 'metric.type="logging.googleapis.com/user/lab-deploy/demo/requests"'}}}})
        selector = request.url.params["filter"]
        if selector.startswith("select_slo"):
            return httpx.Response(200, json={"timeSeries": [{"points": [{"interval": {"endTime": "2026-10-18T22:00:00Z"}, "value": {"doubleValue": -0.2}}]}]})
        if "server-errors" in selector:
            return httpx.Response(200, json={"timeSeries": [{"points": [{"interval": {"endTime": "2026-10-18T00:00:00Z"}, "value": {"int64Value": "18"}},
                                                                         {"interval": {"endTime": "2026-10-19T00:00:00Z"}, "value": {"int64Value": "0"}}]}]})
        if "requests" in selector:
            return httpx.Response(200, json={"timeSeries": [{"points": [{"interval": {"endTime": "2026-10-18T00:00:00Z"}, "value": {"int64Value": "900"}}]}]})
        if path.endswith("/alertPolicies"):
            return httpx.Response(200, json={"alertPolicies": [{"enabled": True}, {"enabled": False}]})
        if path.endswith("/uptimeCheckConfigs"):
            return httpx.Response(200, json={"uptimeCheckConfigs": [{"period": "300s"}, {"period": "60s", "selectedRegions": ["USA_OREGON", "USA_IOWA", "USA_VIRGINIA"]}]})
        raise AssertionError(str(request.url))
    target = types.SimpleNamespace(**{**TARGET, "project_id": "lab-deploy-prod-abcd"})
    found = observer(handler)
    budget = found.error_budget(target, "demo-production")
    assert budget["requests_counted"] == 900 and budget["errors_counted"] == 18 and budget["clears_by"] == "2026-11-17"
    line = chat.budget_line(budget)
    assert line.startswith("Error budget: spent") and line.endswith("its errors leave the window by 17 Nov 2026 at the latest, if no new ones come")
    assert chat.budget_line({**budget, "requests_counted": 0}).startswith("Error budget: no user requests in the 30 days, so nothing to count yet")
    assert found.policies("lab-deploy-prod-abcd") == {"alert_policies": 2, "enabled_alert_policies": 1, "uptime_checks": 2,
                                                       "uptime_executions_month": 8640 * 6 + 43200 * 3}


# ---- the words
def test_the_words_of_a_report_in_the_release_the_list_and_the_health_view(tmp_path):
    report = {"verdict": "same", "environment": "staging", "new": stats(150, 0, 120)}
    assert chat.report_line(report) == ("behaves about the same as the version before: 150 requests besides the health checks in 24 hours, "
                                        "0 server errors, 95% within 120 ms")
    assert chat.report_line({"verdict": "replaced", "note": "a newer release replaced it within the hour"}).startswith("was replaced within the hour")
    assert chat.report_line(None) is None
    plan = {"application": "demo", "state": "succeeded", "sha": "a" * 40, "commit": {"message": "Fix"}, "receipt": {"state": "succeeded"},
            "release_report": report}
    assert "- A day later, it behaves about the same as the version before" in chat.release(plan)
    apps = {"demo": {"live_release": {"sha": "a" * 40, "completed_at": "2026-10-20T10:00:00Z", "report": {"verdict": "worse"}}}}
    assert "`demo`: its release of 20 Oct 2026 did worse than the version before; ask me about it" in chat.applications(apps)
    cfg = config(tmp_path)
    c = Controller(cfg, FakeGitHub(), store_with(tmp_path, release("live", NOW - 2 * DAY, revision="demo-rnew", release_report=report)),
                   FakeObserver(server_errors=3, errors_lines=1))
    c.observer.logs = lambda target, minutes, severity, limit: [{"message": "The request was aborted because there was no available instance."}]
    health = c.service_health(cfg.principals[0], "demo")
    assert health["release_report"]["verdict"] == "same" and health["diagnosis"]["contract_key"] == "scaling.maximum"
    text = chat.health(health)
    assert "Since its release of" in text and "behaves about the same as the version before" in text
    assert "Likely cause: every instance was busy and Cloud Run could not start another one (the instance limit)" in text


def test_running_services_failures_are_diagnosed():
    timeout = diagnose({"candidate_logs": [{"message": "The request has been terminated because it has reached the maximum request timeout."}]})
    assert timeout["contract_key"] == "timeout_seconds" and "3600" in timeout["operator"]


# ---- the workflows and the module
def test_promotions_say_how_they_ended_and_the_watch_checks_the_digest():
    promote = Path(".github/workflows/promote.yml").read_text()
    notify = promote[promote.index("  notify:"):]
    assert "needs: [describe, promote]" in notify and "secrets.NOTIFY_WEBHOOK_URL" in notify and "id-token" not in notify
    assert "production rolled back" in notify and "promotion stopped" in notify
    watch = Path(".github/workflows/controller-watch.yml").read_text()
    assert "- cron: '17 16 * * *'" in watch and "issues: read" in watch and '"Fast Deploy: today"' in watch and "180000" in watch
    assert "github.event.schedule != '17 16 * * *'" in watch  # the half-hourly ping does not run on the daily check
    scheduler = Path("labdeploy/scheduler.py").read_text()
    assert '"release_reports": (controller.release_report_tick, lambda: 900, 180)' in scheduler and '"digest": (controller.digest_tick' in scheduler


def test_the_digest_hour_is_a_utc_hour_or_null():
    assert Config.model_fields["digest_hour_utc"].default == 13
    with pytest.raises(ValueError):
        config(Path("/tmp"), digest_hour_utc=24)
    assert time.time() > 0
