"""The chat's answers (labdeploy/chat.py): the summary every main tool starts with, written by code from the result."""
import hashlib

from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient

from labdeploy import chat
from labdeploy.config import Config
from labdeploy.server import INSTRUCTIONS, build_app

SHA = "dede786786f5e38646cf5c221f9628fedfa08d41"
COMMIT = {"sha": SHA, "message": "Merge pull request #4 from brianboti/buildpack-trial\n\nbody"}
URL = "https://lab-bare-42n6s5wp5q-uc.a.run.app"
RUN = "https://github.com/brianboti/lab-bare/actions/runs/38852345252"


def plan(state, **extra):
    base = {"id": "8295ca7ef4604b2283d73383883e66d4", "application": "lab-bare", "sha": SHA, "commit": COMMIT, "state": state,
            "spec": {"exposure": "authenticated"}, "bake_seconds": 300, "checks": {}, "workflow": {"html_url": RUN},
            "next_action": "the controller's own sentence"}
    return {**base, **extra}


def test_every_answer_has_one_shape_a_bold_outcome_facts_and_one_next_line():
    text = chat.compose("**Done.**", ["one", None, "two"], "nothing to do.", "footer")
    assert text == "**Done.**\n\n- one\n- two\n\n**Next:** nothing to do.\n\nfooter"
    assert chat.money(0) == "$0" and chat.money(0.06) == "$0.06" and chat.money(1234) == "$1,234.00" and chat.money(None) == "unknown"
    assert chat.version(SHA, COMMIT["message"]) == "`dede786` (Merge pull request #4 from brianboti/buildpack-trial)"
    assert chat.clip("a `b`\nc", 50) == "a 'b' c" and chat.clip("x" * 300, 10).endswith("…") and len(chat.clip("x" * 300, 10)) == 10
    assert chat.duration("2026-10-16T04:46:30Z", "2026-10-16T04:56:07Z") == "10 min" and chat.duration("2026-10-16T04:46:30Z", None) is None


def test_a_live_release_names_the_version_the_address_the_checks_and_how_to_undo_it():
    done = plan("succeeded", checks={"tests": "passed", "dependency_scan": "passed", "health": "passed", "target": "passed"},
                phases={"state": "succeeded", "url": URL, "bake": {"result": "inconclusive", "window": {"requests": 0}}},
                workflow={"html_url": RUN, "created_at": "2026-10-16T04:46:30Z"}, outcome={"completed_at": "2026-10-16T04:56:07Z"},
                vulnerabilities={"analysis_status": "FINISHED_SUCCESS", "by_severity": {"CRITICAL": 2, "HIGH": 11},
                                 "fixable_by_severity": {"CRITICAL": 2, "HIGH": 11}, "blocking": False})
    text = chat.release(done)
    assert text.startswith("**`lab-bare` is live** with `dede786` (Merge pull request #4 from brianboti/buildpack-trial).")
    assert f"Address: {URL} (needs a Google sign-in token; not open in a browser)" in text
    assert "Checks: tests passed · library audit passed · health check passed" in text
    assert "too little traffic to judge, so only the health checks vouch for it" in text
    assert "Image scan: 2 critical and 11 high findings (all fixable); not blocking" in text and "Took 10 min" in text
    assert "**Next:** nothing to do. To undo it, say “roll lab-bare back”." in text
    assert text.endswith(f"Plan `8295ca7e` · [GitHub run]({RUN})")


def test_a_release_in_progress_says_what_it_is_doing_in_plain_words():
    tests = plan("running", timeline=[{"step": "Set up job", "status": "completed"}, {"step": "Run unit tests", "status": "in_progress"}])
    assert "Now: running the tests" in chat.release(tests) and chat.release(tests).startswith("**Release in progress for `lab-bare`**")
    baking = plan("running", checks={"tests": "passed", "dependency_scan": "passed"}, phases={"state": "baking"},
                  timeline=[{"step": "Deploy, probe, promote; restore traffic on failure", "status": "in_progress"}])
    text = chat.release(baking)
    assert "Now: watching the new version with real traffic" in text and "Passed so far: tests, library audit" in text
    assert "if it fails within 5 min, visitors go back to the previous version on their own" in text
    started = chat.release(plan("dispatched"))
    assert started.startswith("**Release started for `lab-bare`**") and "nothing is live until it finishes" in started
    ready = chat.release(plan("planned", cost={"idle_floor_usd_month": 0, "needs_floor_usd_month": 8.57, "compute_ceiling_usd_month": 66.357}))
    assert "**Ready to release `lab-bare`**" in ready and "Cost: $8.57 a month with no visitors; compute at most $66.36 a month" in ready


def test_a_stopped_release_says_the_previous_version_still_serves_and_why():
    migration = plan("failed", phases={"state": "traffic_restored", "failed_phase": "migrating", "error": "relation `x` does not exist"})
    text = chat.release(migration)
    assert text.startswith("**Release stopped for `lab-bare`: the previous version is still serving.**")
    assert "Why: the database migration failed, so nothing new was deployed" in text and "Error: `relation 'x' does not exist`" in text
    assert "fix the migration (check whether part of it ran), then release again." in text
    baked = chat.release(plan("failed", phases={"state": "traffic_restored", "failed_phase": "baking", "candidate_logs": ["x"]}))
    assert "the previous version is serving again" in baked and "failed its watch with real traffic" in baked and "ask me to show them" in baked
    tests = chat.release(plan("failed", failed_step={"step": "Run unit tests"}, checks={"tests": "failed"}))
    assert tests.startswith("**Release failed for `lab-bare` while running the tests.** Nothing new is serving.")
    assert "Checks: tests failed" in tests and "**Next:** the controller's own sentence" in tests
    stuck = chat.release(plan("failed", receipt={"state": "rollback_failed_manual_recovery_required", "restore_error": "quota"}))
    assert stuck.startswith("**Needs you now: `lab-bare` could not be put back on its previous version.**") and "docs/RUNBOOK.md" in stuck
    assert "GitHub never ran it" in chat.release(plan("dispatch_lost"))


def test_a_rollback_says_what_serves_now_and_what_it_does_not_undo():
    done = plan("succeeded", kind="rollback", receipt={"state": "rolled_back", "restored_traffic": {"lab-bare-r3a51": 100},
                                                       "rolled_back_from": "lab-bare-r8295"})
    text = chat.release(done)
    assert text.startswith("**`lab-bare` is back on its previous version.**") and "Now serving: `lab-bare-r3a51` 100%" in text
    assert "Database changes and anything the app already did are not undone" in text
    assert "within about a minute" in chat.release(plan("dispatched", kind="rollback"))


def test_onboarding_counts_its_steps_names_what_it_waits_for_and_who_acts():
    view = {"application": "api", "repository": "brianboti/lab-monorepo", "path": "services/api", "state": "planning",
            "steps": {"infrastructure": {"pull_request": 49, "url": "https://github.com/brianboti/lab-deploy/pull/49"},
                      "planning": {"waiting": "the infrastructure plan run"}}}
    text = chat.onboarding(view)
    assert text.startswith("**Onboarding `api`, step 4 of 9: plan the cloud setup and check the safety rules.**")
    assert "From `brianboti/lab-monorepo`, folder `services/api`" in text and "Waiting for the infrastructure plan run" in text
    assert "[#49](https://github.com/brianboti/lab-deploy/pull/49)" in text and "it continues on its own every minute" in text
    person = chat.onboarding({**view, "state": "merging_workflows", "steps": {"merging_workflows": {"waiting": "a person to merge https://x/1"}}})
    assert "a person reviews and merges that pull request" in person
    blocked = chat.onboarding({**view, "state": "blocked", "blocked_at": "validating", "error": "platform.yaml is missing"})
    assert blocked.startswith("**Onboarding `api` stopped at: check the repository.**") and "Nothing was created" in blocked
    done = chat.onboarding({**view, "state": "complete", "plan_id": "3a51a7036e6b", "measures": {"minutes": 4.0, "blocked": 1}})
    assert done.startswith("**`api` is onboarded** in 4 min.") and "First release started: plan `3a51a703`" in done
    assert "Stopped 1 time on the way" in done


def test_a_repository_check_lists_what_is_missing_why_and_what_the_platform_writes():
    result = {"repository": "lab/web", "path": "", "ready": False, "platform_can_draft": ["platform.yaml"],
              "checklist": [{"item": "Dockerfile", "ok": True}, {"item": "platform.yaml", "ok": False, "platform_writes": True, "why": "the application's contract"},
                            {"item": "requirements.txt", "ok": False, "platform_writes": False, "why": "pinned runtime dependencies"}],
              "recommended": []}
    text = chat.check(result)
    assert text.startswith("**Not ready yet: `lab/web`.** 2 required items missing:")
    assert "1. `platform.yaml`: the application's contract (the platform can draft it)" in text
    assert "2. `requirements.txt`: pinned runtime dependencies" in text and "sets `reviewed: true` and merges" in text
    ready = chat.check({**result, "ready": True, "checklist": [], "recommended": [{"item": "SECURITY.md", "ok": False, "platform_writes": True}]})
    assert ready.startswith("**Ready to deploy: `lab/web`.**") and "`SECURITY.md` (the platform can draft them)" in ready


def test_a_draft_names_its_pull_request_files_findings_and_the_review_a_person_does():
    view = {"repository": "lab/bare", "path": "", "files": {"Dockerfile": "x", "platform.yaml": "y"},
            "detected": {"framework": "fastapi", "application": "app:app", "python": "3.12", "port": 8080, "health_path": "/health", "health_route_found": True},
            "review": ["requirements.lock was not compiled"], "pull_request": {"number": 2, "url": "https://github.com/lab/bare/pull/2", "changed": ["Dockerfile"]}}
    text = chat.draft(view)
    assert text.startswith("**Draft pull request opened for `lab/bare`**: [#2](https://github.com/lab/bare/pull/2), 2 files.")
    assert "Found: FastAPI app `app:app`, Python 3.12, port 8080, health check at `/health`" in text and "To check: requirements.lock" in text
    stale = chat.draft({**view, "pull_request": {"number": 1, "url": "u", "changed": []}})
    assert stale.startswith("**A draft is already open for `lab/bare`**") and "close it and ask me to draft again" in stale
    assert chat.draft({**view, "pull_request": None}).startswith("**Nothing to draft")


def test_the_application_list_is_one_table_and_says_what_waits():
    apps = {"lab-bare": {"live_release": {"sha": SHA, "commit": COMMIT, "url": URL, "completed_at": "2026-10-16T04:56:07Z"},
                         "undeployed_commits": {"status": "identical", "ahead_by": 0}},
            "demo": {"live_release": {"sha": "839f28d" + "0" * 33, "commit": {"message": "Bake trial over"}, "url": "https://d"},
                     "undeployed_commits": {"status": "ahead", "ahead_by": 1}, "paused": True},
            "new": {"live_release": None, "undeployed_commits": None, "platform_pin": {"supported": False}}}
    text = chat.applications(apps)
    assert text.startswith("**3 applications**: 1 app has new commits waiting.")
    assert "| App | Live version | Released | Main |" in text and f"| [`lab-bare`]({URL}) | `dede786` (Merge pull request #4" in text
    assert "| 16 Oct 2026 | up to date |" in text and "| 1 new commit |" in text and "| `new` | not released yet | — | — |" in text
    assert "`demo` is paused" in text and "`new`: its release workflows are too old" in text
    assert "**Next:** say “deploy the latest demo” to release what is waiting." in text


def test_health_cost_compare_logs_and_controller_read_as_plain_sentences():
    healthy = chat.health({"application": "lab-bare", "service": {"ready": True, "traffic": [{"commit": SHA}]}, "agreement": True,
                           "metrics": {"window_minutes": 60, "requests": 73, "server_errors": 0, "uptime_checks_passed": 1.0,
                                       "request_latency_ms_p95": 9.5, "memory_utilization_p95": 0.1395},
                           "access": {"exposure": "lab-members"}, "console_url": "https://console"})
    assert healthy.startswith("**`lab-bare` is healthy.**") and "73 requests, 0 server errors, 95% answered within 9.5 ms" in healthy
    assert "Uptime checks: 100% passed" in healthy and "Access: lab members open it in a browser" in healthy and "Memory: 14%" in healthy
    errors = chat.health({"application": "x", "service": {"ready": True}, "metrics": {"window_minutes": 60, "requests": 9, "server_errors": 3}})
    assert errors.startswith("**`x` is serving, with 3 server errors in the last 60 min.**") and "roll x back" in errors
    cost = chat.estimate({"idle_floor_usd_month": 0.0, "needs_floor_usd_month": 8.57, "compute_ceiling_usd_month": 66.357, "pricing_verified": "2026-10-11",
                          "needs": [{"need": "postgres", "name": "mono-admin", "floor_usd_month": 8.57}],
                          "release_overheads": {"image_scan_usd_per_release": 0.26},
                          "findings": [{"id": "worst_case_bound", "message": "bound"}, {"id": "database_always_on", "severity": "warning", "message": "db"},
                                       {"id": "authenticated_only", "severity": "info", "message": "Only signed-in callers."}]}, application="admin")
    assert cost.startswith("**Cost of `admin`: about $8.57 a month with no visitors; compute at most $66.36 a month even if fully busy.**")
    assert "Postgres database `mono-admin`: $8.57 a month" in cost and "about $0.26 for the image scan" in cost and "Only signed-in callers." in cost
    assert "- bound" not in cost and "- db" not in cost and cost.endswith("not a bill.")
    waiting = chat.compare({"application": "demo", "live_release": {"sha": SHA, "commit": COMMIT}, "ahead_by": 2,
                            "commits": [{"sha": "a" * 40, "message": "Fix the form", "author": "Ana"}, {"sha": "b" * 40, "message": "Bump"}],
                            "changes": {"application_code": True, "dependencies": True}, "cost": None})
    assert waiting.startswith("**2 new commits waiting for `demo`.**") and "`aaaaaaa` (Fix the form) by Ana" in waiting
    assert "Changes: application code, dependencies" in waiting
    lines = chat.logs({"application": "x", "minutes": 30, "severity": "ERROR", "count": 7,
                       "lines": [{"at": "2026-10-16T04:50:00Z", "message": "Traceback ```x```"}] * 7})
    assert lines.startswith("**7 error lines for `x`** in the last 30 minutes (the newest 5 shown):") and "04:50:00 Traceback '''x'''" in lines
    assert chat.logs({"application": "x", "count": 0}).startswith("**No error lines for `x`**")
    status = chat.controller({"version": "1.13.0", "live_dispatch": True, "paused": False, "github_credential": {"days_left": 87},
                              "infrastructure_drift": {"status": "clean", "age_days": 1.4}, "spending_guards": {"missing": []},
                              "background": {"watchdog": {"last_result": {"problems": ["api: rightsize-gcp.yml: no scheduled run recorded yet",
                                                                                       "demo: the newest release attempt is 'failed'"]}}}})
    assert status.startswith("**The controller is running** version 1.13.0; releases are on.")
    assert "Weekly cost report not run yet for: `api`" in status and "Watchdog: demo: the newest release attempt" in status


def test_a_summary_never_breaks_a_tool_and_never_replaces_one_already_there():
    def broken(result):
        raise KeyError("x")
    assert chat.attach(broken, {"a": 1}) == {"a": 1}
    assert chat.attach(chat.release, {"summary": "kept"}) == {"summary": "kept"}
    assert chat.attach(chat.release, ["not", "a", "dict"]) == ["not", "a", "dict"]
    assert list(chat.attach(chat.release, plan("dispatched")))[0] == "summary"


def test_the_tools_start_with_the_summary_and_the_instructions_say_how_to_answer(tmp_path):
    config = Config.model_validate({"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
                                    "database": str(tmp_path / "db"),
                                    "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True}]})
    headers = {"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"}
    with TestClient(build_app(config, FakeGitHub())) as client:
        def call(name, **arguments):
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
            return client.post("/mcp", headers=headers, json=body).json()["result"]["structuredContent"]
        apps = call("list_applications")
        assert list(apps)[0] == "summary" and apps["summary"].startswith("**1 application**") and apps["demo"]["service"] == "demo"
        planned = call("create_deployment_plan", application="demo")
        assert planned["summary"].startswith("**Ready to release `demo`**") and planned["state"] == "planned"
        assert call("get_controller_status")["summary"].startswith("**Releases are switched off**")  # this test's configuration
    assert "summary" in INSTRUCTIONS and "**Next:**" in INSTRUCTIONS and "no raw JSON" in INSTRUCTIONS
