"""The chat's answers (labdeploy/chat.py): the summary every main tool starts with, written by code from the result."""
import hashlib

from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient

from labdeploy import chat
from labdeploy.config import Config
from labdeploy.controller import HIDDEN_COSTS, HIDDEN_STATUS
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
    assert "Checks: tests failed" in tests and "**Next:** the tests failed on main; the developer fixes them, then release again." in tests
    assert "the controller's own sentence" not in tests  # the pipeline's sentence stays in next_action for the detail
    stuck = chat.release(plan("failed", receipt={"state": "rollback_failed_manual_recovery_required", "restore_error": "quota"}))
    assert stuck.startswith("**Needs you now: `lab-bare` could not be put back on its previous version.**") and "docs/RUNBOOK.md" in stuck
    assert "GitHub never ran it" in chat.release(plan("dispatch_lost"))


def test_a_rollback_says_what_serves_now_and_what_it_does_not_undo():
    done = plan("succeeded", kind="rollback", receipt={"state": "rolled_back", "restored_traffic": {"lab-bare-r3a51": 100},
                                                       "rolled_back_from": "lab-bare-r8295"})
    text = chat.release(done)
    assert text.startswith("**`lab-bare` is back on its previous version.**") and "Now serving: the previous version" in text
    assert "No longer serving: `dede786` (Merge pull request #4 from brianboti/buildpack-trial)" in text and "lab-bare-r" not in text
    assert "Database changes and anything the app already did are not undone" in text
    assert "visitors move to the previous version within about a minute" in chat.release(plan("dispatched", kind="rollback"))


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


# ---- 1.17.1: clearer answers, found live on 2026-10-17
def test_a_failed_release_says_what_to_do_in_plain_words_not_the_pipelines_sentence():
    scan = plan("failed", failed_step={"step": "Summarize image vulnerabilities"},
                next_action="The vulnerability policy blocked the release; see vulnerability-summary.json, fix or accept the findings, then deploy again.")
    text = chat.release(scan)
    assert text.startswith("**Release failed for `lab-bare` while scanning the image.**")
    assert "**Next:** the image scan found serious problems with fixes available; the developer updates the libraries or the base image, then release again." in text
    assert "vulnerability-summary.json" not in text and f"[GitHub run]({RUN})" in text and scan["next_action"].startswith("The vulnerability policy")
    cancelled = plan("cancelled", recovery_run={"conclusion": "success", "receipt": {"state": "no_recovery_needed"}},
                     next_action="The run was cancelled before the release step changed anything, or no receipt was uploaded. The recovery run that "
                                 "followed concluded 'success': no_recovery_needed.")
    text = chat.release(cancelled)
    assert ("**Next:** it was cancelled before anything changed, so the previous version kept serving. The check that followed confirmed nothing "
            "needed restoring. Release again when ready.") in text
    assert "no_recovery_needed" not in text and "receipt" not in text
    interrupted = chat.release(plan("cancelled", receipt={"state": "traffic_restored", "failed_phase": "deploying", "error": "KeyboardInterrupt: "}))
    assert "Why: it was cancelled" in interrupted and "KeyboardInterrupt" not in interrupted and "**Next:** release again when ready." in interrupted
    waiting = chat.release(plan("failed", recovery_run={"status": "in_progress", "conclusion": None}))
    assert "**Next:** it failed; the GitHub run has the detail. The check that runs after a stopped release is still running." in waiting
    assert "put visitors back on the previous version" in chat.release(plan("timed_out", recovery_run={"conclusion": "success", "receipt": {"state": "traffic_restored"}}))


def test_a_rollback_names_commits_not_revision_names():
    done = plan("succeeded", kind="rollback", previous={"revision": "lab-bare-r3a51", "sha": "3a51" + "0" * 36, "message": "Add the form"},
                receipt={"state": "rolled_back", "restored_traffic": {"lab-bare-r3a51": 100}, "rolled_back_from": "lab-bare-r8295"})
    text = chat.release(done)
    assert "Now serving: `3a51000` (Add the form)" in text and "No longer serving: `dede786` (Merge pull request #4 from brianboti/buildpack-trial)" in text
    assert "lab-bare-r3a51" not in text and "lab-bare-r8295" not in text
    assert "visitors move to `3a51000` (Add the form) within about a minute" in chat.release(plan("dispatched", kind="rollback", previous=done["previous"]))
    assert "visitors move to the previous version within" in chat.release(plan("dispatched", kind="rollback", previous={"revision": "lab-bare-r3a51"}))
    split = chat.release({**done, "receipt": {**done["receipt"], "restored_traffic": {"lab-bare-r3a51": 60, "lab-bare-r1111": 40}}})
    assert "Now serving: `3a51000` (Add the form) (traffic split `lab-bare-r3a51` 60%, `lab-bare-r1111` 40%)" in split


def test_the_controller_resolves_the_previous_versions_commit_for_a_rollback(tmp_path):
    from test_phase2_release import IMAGE, make, release
    c, p, gh = make(tmp_path)
    release(c, p, gh, "1" * 40, "demo-r1")
    gh.sha = "2" * 40
    second = c.plan(p, "demo")
    c.deploy(p, second["id"])
    gh.receipt = {"release": {"plan_id": second["id"], "state": "succeeded", "revision": "demo-r2", "image": IMAGE, "previous_traffic": {"demo-r1": 100}},
                  "vulnerabilities": None}
    assert c.status(p, second["id"])["state"] == "succeeded"
    record = c.rollback(p, "demo")
    assert record["previous"] == {"revision": "demo-r1", "sha": "1" * 40, "message": "Fix the login bug"}
    assert "visitors move to `1111111` (Fix the login bug) within about a minute" in chat.release(record)
    assert c.status(p, record["id"])["previous"] == record["previous"]  # kept with the plan, so a later status answer has it too
    assert c.previous_release("demo", {"demo-r9": 100}) == {"revision": "demo-r9"} and c.previous_release("demo", None) is None


def test_logs_suggest_a_rollback_only_for_warnings_and_worse():
    info = chat.logs({"application": "x", "minutes": 30, "severity": "INFO", "count": 3, "lines": [{"at": "2026-10-16T04:50:00Z", "message": "GET /health 200"}] * 3})
    assert info.startswith("**3 info lines for `x`**") and "roll x back" not in info
    assert "**Next:** nothing to do: these are ordinary lines, not problems. Ask me for `x`'s ERROR lines to see only what went wrong." in info
    assert "roll x back" in chat.logs({"application": "x", "severity": "WARNING", "count": 1, "lines": [{"message": "slow"}]})
    assert "roll x back" in chat.logs({"application": "x", "severity": "CRITICAL", "count": 1, "lines": [{"message": "down"}]})
    assert chat.logs({"application": "x", "severity": "DEFAULT", "count": 0}).startswith("**No log lines for `x`**")


def test_the_estimate_prices_a_traffic_scenario_and_keeps_findings_whole():
    long_finding = "A" * 100 + ". " + "B" * 200 + "."
    result = {"idle_floor_usd_month": 0.0, "needs_floor_usd_month": 0.0, "compute_ceiling_usd_month": 66.357, "pricing_verified": "2026-10-11",
              "scenario": {"requests_per_month": 100000, "average_request_seconds": 0.2, "usd_month": {"best_case": 0.12, "worst_case": 4.56}},
              "findings": [{"id": "low_concurrency", "severity": "info", "message": long_finding}]}
    text = chat.estimate(result, application="demo")
    assert "- With 100,000 requests a month of 0.2 s each: between $0.12 and $4.56 a month, depending on how many overlap" in text
    assert "- " + "A" * 100 + ".\n" in text and "B" not in text.split("**Next")[0].split("- " + "A" * 100)[1][:3]
    assert chat.sentence_clip("One. Two. " + "x" * 300, 20) == "One. Two."
    assert chat.sentence_clip("x" * 300, 10) == chat.clip("x" * 300, 10)  # no sentence end to cut at: the plain clip
    assert chat.sentence_clip("Short.", 240) == "Short."
    assert "With" not in chat.estimate({**result, "scenario": None}, application="demo")


def test_an_old_onboarding_points_at_health_and_releases_and_a_hand_registered_app_says_so():
    view = {"application": "api", "repository": "brianboti/lab-monorepo", "state": "complete", "plan_id": "3a51a7036e6b",
            "completed_at": chat.moment("2026-10-13T12:00:00Z").timestamp(), "measures": {"minutes": 4.0}}
    fresh = chat.onboarding(view, now="2026-10-13T12:10:00Z")
    assert fresh.startswith("**`api` is onboarded** in 4 min.") and "First release started: plan `3a51a703`" in fresh and "I'll follow the first release" in fresh
    old = chat.onboarding(view, now="2026-10-16T12:00:00Z")
    assert old.startswith("**`api` was onboarded** on 13 Oct 2026 in 4 min.") and "Its first release was plan `3a51a703`" in old
    assert "**Next:** ask me about `api`'s health or releases." in old and "I'll follow" not in old
    by_hand = chat.onboarding({"application": "demo", "state": "registered_by_hand", "repository": "lab/demo",
                               "live_release": {"sha": SHA, "commit": COMMIT, "completed_at": "2026-10-15T10:00:00Z"}})
    assert by_hand.startswith("**`demo` was registered by hand**, not onboarded from chat, so there is no onboarding to follow.")
    assert "Live: `dede786` (Merge pull request #4 from brianboti/buildpack-trial), released 15 Oct 2026" in by_hand
    assert "onboard_application" not in by_hand and "**Next:** ask me how `demo` is doing (its health) or which apps are live." in by_hand
    assert "Not released by this controller yet" in chat.onboarding({"application": "new", "state": "registered_by_hand", "repository": "lab/new"})


def test_refusals_of_a_repository_taken_as_it_is_are_a_numbered_list():
    result = {"repository": "lab/x", "checklist": [{"item": "Dockerfile", "ok": False}], "as_it_is": {"ready": False, "refusals": [
        {"file": ".env", "problem": "is committed", "fix": "remove it and rotate the values"},
        {"file": "Dockerfile", "problem": "pipes a download into a shell", "fix": "download, check and then run"},
        {"file": "pyproject.toml", "problem": "needs Python 3.9.", "fix": "move to 3.11 or newer."}]}}
    text = chat.check(result)
    assert ("As it is, it is refused too:\n1. `.env` is committed. Fix: remove it and rotate the values.\n"
            "2. `Dockerfile` pipes a download into a shell. Fix: download, check and then run.\n"
            "3. `pyproject.toml` needs Python 3.9. Fix: move to 3.11 or newer.\n\n**Next:**") in text
    many = chat.check({**result, "as_it_is": {"ready": False, "refusals": [{"file": f"f{n}", "problem": f"p{n}", "fix": f"x{n}"} for n in range(7)]}})
    assert "5. `f4` p4. Fix: x4.\n…and 2 more in the details" in many and "f5" not in many


def test_the_controller_status_says_the_weekly_report_runs_on_its_own():
    status = chat.controller({"version": "1.17.1", "live_dispatch": True, "paused": False, "github_credential": {"kind": "github_app"},
                              "background": {"watchdog": {"last_result": {"problems": ["api: rightsize-gcp.yml: no scheduled run recorded yet"]}}}})
    assert "GitHub access through the GitHub App" in status and "Weekly cost report not run yet for: `api`; it runs on its own every week" in status
    assert "**Next:** nothing to do; the weekly cost report runs on its own. Tell the operator only if it is still missing after a week." in status
    assert chat.controller({"version": "1.17.1", "live_dispatch": True}).endswith("**Next:** nothing to do.")


def test_the_controller_status_of_a_limited_identity_says_what_it_leaves_out():
    """An identity that sees only some applications gets each job's times, not its result, and the digest without the verdict
    judged across every application (labdeploy/controller.py, HIDDEN_STATUS); the answer says so and still reads whole."""
    limited = {"version": "1.29.2", "live_dispatch": True, "paused": False, "github_credential": {"kind": "github_app"},
               "background": {"backup": {"last_run": "2026-10-21T03:00:00+00:00", "next_run": "2026-10-22T03:00:00+00:00"},
                              "watchdog": {"last_run": "2026-10-21T09:00:00+00:00", "next_run": "2026-10-22T09:00:00+00:00"}},
               "infrastructure_drift": {"status": "clean", "age_days": 1.4, "drifted": {}}, "spending_guards": {"missing": []},
               "digest": {"day": "2026-10-21", "delivered": [{"channel": "email", "ok": True}], "generated_at": "2026-10-21T13:00:00+00:00"},
               "hidden": HIDDEN_STATUS}
    text = chat.controller(limited)
    assert "- Last backup: 21 Oct 2026\n" in text and "- The cloud setup matches the code (checked 1.4 days ago)\n" in text
    assert "- Daily digest of 21 Oct 2026: sent by email\n" in text and "all quiet" not in text and "something to look at" not in text
    assert f"- {HIDDEN_STATUS.rstrip('.')}\n" in text and "Watchdog" not in text and text.endswith("**Next:** nothing to do.")
    whole = chat.controller({**limited, "digest": {**limited["digest"], "attention": True}, "hidden": None})
    assert "- Daily digest of 21 Oct 2026: something to look at, sent by email" in whole and "shown to an identity" not in whole


def test_the_cost_report_of_a_limited_identity_gives_its_own_spend_and_says_who_sees_the_rest():
    """Of the bill, an identity that sees only some applications gets the lines labelled with them (Controller.cost_report_for):
    the answer gives the application's own spend beside its window before, and one line on the whole project's bill."""
    report = {"generated_at": "2026-10-12T06:23:00+00:00", "service": "demo", "window_days": 14, "rightsizing": {"observed": None, "recommendations": []},
              "actual_spend": {"window_days": 30, "by_app": [{"app": "demo", "net_cost_usd": 1.5}], "previous": {"window_days": 30, "by_app": []},
                               "app": {"name": "demo", "net_cost_usd": 1.5, "by_environment": {"staging": 1.0, "production": 0.5}}}}
    text = chat.cost_report({"report": report, "hidden": HIDDEN_COSTS}, application="demo")
    assert ("- Actual spend, last 30 days: $1.50 for `demo`'s own resources, net of credits (staging $1.00, production $0.50); $0 in the 30 days before\n"
            f"- {chat.WHOLE_BILL}\n") in text
    assert "whole project, net" not in text and "This month in" not in text and "**Next:** nothing to do; the report runs again next week." in text
    older = {**report, "actual_spend": {"window_days": 30, "by_app": [{"app": "other", "net_cost_usd": 4.0}, {"app": "demo", "net_cost_usd": 2.25}]}}
    assert "- Actual spend, last 30 days: $2.25 for `demo`'s own resources, net of credits\n" in chat.cost_report({"report": older, "hidden": HIDDEN_COSTS})
    nothing = {**report, "actual_spend": {"window_days": 30, "by_app": []}}
    assert "- Actual spend, last 30 days: $0 for `demo`'s own resources, net of credits\n" in chat.cost_report({"report": nothing, "hidden": HIDDEN_COSTS})
    stale = chat.cost_report({"report": {**report, "actual_spend": {"window_days": 30, "warning": "The billing export is empty"}}, "hidden": HIDDEN_COSTS})
    assert "- Actual spend unknown: The billing export is empty" in stale and chat.WHOLE_BILL not in stale


def test_the_plan_list_is_a_short_table_with_plain_outcomes_and_readable_times():
    made = chat.moment("2026-10-16T04:46:30Z").timestamp()
    items = [{"id": "8295ca7ef4604b2283d73383883e66d4", "application": "lab-bare", "sha": SHA, "expires_at": made + 900, "state": "succeeded", "kind": "release",
              "summary": COMMIT["message"], "completed_at": "2026-10-16T04:56:07Z", "revision": "lab-bare-r8295"},
             {"id": "b" * 32, "application": "demo", "sha": "b" * 40, "expires_at": made + 900, "state": "succeeded", "kind": "rollback", "summary": "Bump",
              "completed_at": "2026-10-16T05:00:00Z"},
             {"id": "c" * 32, "application": "demo", "sha": "c" * 40, "expires_at": made + 900, "state": "failed", "kind": "release", "summary": "Break", "completed_at": None}]
    items += [{"id": str(n) * 32, "application": "demo", "sha": "d" * 40, "expires_at": made + 900, "state": "planned", "kind": "release", "summary": "x"} for n in range(7)]
    text = chat.plans({"plans": items})
    assert text.startswith("**10 plans for 2 apps**: 7 planned but not released, 1 released, 1 rolled back, 1 failed. The newest 8:")
    assert "| App | Outcome | Version | When (UTC) |" in text
    assert "| `lab-bare` | released | `dede786` (Merge pull request #4 from brianboti/bu…) | 16 Oct 04:56 |" in text
    assert "| `demo` | rolled back | `bbbbbbb` (Bump) | 16 Oct 05:00 |" in text and "| `demo` | failed | `ccccccc` (Break) | 16 Oct 04:46 |" in text
    assert "8295ca7ef4604b2283d73383883e66d4" not in text and text.count("\n| `") == 8 and "1760" not in text
    assert "**Next:** ask me about any of these" in text
    assert chat.plans({"plans": []}).startswith("**No plans yet**")
    running = chat.plans({"plans": [{**items[0], "state": "running"}]})
    assert running.startswith("**1 plan for 1 app**: 1 in progress.") and "**Next:** the newest release (`lab-bare`) is still on its way" in running


def test_the_event_list_is_a_short_table_with_readable_times_and_counts_errors():
    at = chat.moment("2026-10-17T14:03:00Z").timestamp()
    items = [{"at": at, "identity": "brian", "client": "1.2.3.4", "kind": "tool", "name": "list_plans", "application": None, "outcome": "ok", "duration_ms": 12, "detail": None},
             {"at": at - 60, "identity": "brian", "client": "1.2.3.4", "kind": "tool", "name": "deploy_latest", "application": "demo", "outcome": "error", "duration_ms": 30,
              "detail": "Deployments of demo are paused by the operator"},
             {"at": at - 120, "identity": None, "client": "5.6.7.8", "kind": "oauth_sign_in", "name": None, "application": None, "outcome": "ok", "duration_ms": None, "detail": None}]
    text = chat.events({"events": items})
    assert text.startswith("**3 events**, 1 error, from 2 identities:")
    assert "| When (UTC) | Who | What | App | Outcome |" in text and "| 17 Oct 14:03 | brian | `list_plans` | — | ok |" in text
    assert "| 17 Oct 14:02 | brian | `deploy_latest` | `demo` | error: Deployments of demo are paused by the operator |" in text
    assert "| 17 Oct 14:01 | 5.6.7.8 | oauth sign in | — | ok |" in text and "**Next:** ask me what went wrong in the errors." in text
    assert str(int(at)) not in text
    quiet = chat.events({"events": items[:1] * 12})
    assert quiet.startswith("**12 events**, 0 errors, from 1 identity; the newest 8:") and "**Next:** nothing to do" in quiet
    assert chat.events({"events": []}).startswith("**No events recorded**")


def test_the_cost_report_gives_measured_use_proposals_and_spend():
    report = {"generated_at": "2026-10-12T06:23:00+00:00", "service": "demo", "window_days": 14,
              "estimate": {"compute_ceiling_usd_month": 132.71, "idle_floor_usd_month": 0.0},
              "rightsizing": {"observed": {"usd_month_at_list_price": 0.0034, "requests": 6},
                              "recommendations": [{"setting": "resources.memory_mib", "current": 1024, "recommended": 512, "reason": "p95 memory 14%",
                                                   "estimated_monthly_saving_usd": 0.5}]},
              "drift": [{"setting": "cpu", "platform_yaml": 1, "live": 2}],
              "actual_spend": {"window_days": 30, "total_net_cost_usd": 1.75, "by_service": [{"service": "Cloud Run", "net_cost_usd": 1.5}],
                               "app": {"name": "demo", "net_cost_usd": 1.5}},
              "release_tolls": {"month": "2026-10", "fresh_images": 3, "image_scans_usd": 0.78}}
    result = {"workflow_run_id": 1, "run_url": "https://example/run", "generated_at": "2026-10-12T06:23:00Z", "report": report,
              "spending_guards": {"project_id": "test-project", "cloud_run_spend_cap": {"status": "not_recorded"}}}
    text = chat.cost_report(result, application="demo")
    assert text.startswith("**Measured costs for `demo`**, the 14 days to 12 Oct 2026.")
    assert "Actual spend, last 30 days: $1.75 for the whole project, net of credits; `demo`'s share $1.50" in text
    assert "Measured use: 6 requests in the window, worth under $0.01 a month at list prices" in text and chat.plural(2, "identity") == "2 identities"
    assert "Bounds: $0 a month with no visitors; compute at most $132.71 a month" in text
    assert "Proposals: memory (MiB) 1024 to 512 (p95 memory 14%; saves about $0.50 a month)" in text
    assert "Live settings differ from `platform.yaml`: cpu is 2 live but 1 reviewed" in text and "This month: 3 new images scanned, about $0.78" in text
    assert "Google's own spend cap is not recorded for this project" in text
    assert "**Next:** say “make demo cheaper” and I'll open a pull request with these proposals; a person merges it." in text
    assert text.endswith("[GitHub run](https://example/run)")
    none = chat.cost_report({"report": None, "note": "No successful rightsizing run with a cost-report artifact yet; the workflow runs weekly or on manual dispatch."},
                            application="demo")
    assert none.startswith("**No measured cost report yet for `demo`.**") and "it runs on its own every week" in none
    quiet = chat.cost_report({"report": {**report, "rightsizing": {"observed": None, "recommendations": []}, "actual_spend": {"warning": "The billing export is empty"}}})
    assert "No usage was measured in the window" in quiet and "Proposals: none" in quiet and "Actual spend unknown: The billing export is empty" in quiet
    assert "**Next:** nothing to do; the report runs again next week." in quiet


def test_the_repository_analysis_gives_the_facts_and_the_questions():
    result = {"commit_sha": SHA, "path": "", "supported_candidate": True,
              "evidence": ["Python dependency manifest found", "fastapi appears in dependencies",
                           "redis appears in dependencies: the platform does not provide it", "3 test files found; they run before every build"],
              "draft_spec": {"name": "demo", "port": 8080, "python": "3.12", "concurrency": 80, "resources": {"cpu": 1, "memory_mib": 512},
                             "scaling": {"minimum": 0, "maximum": 3}, "dependencies": ["redis"]},
              "suggested_needs": ["database: postgres (psycopg in the dependencies)"],
              "review_required": ["Confirm stateless HTTP service; source files cannot prove this.", "Confirm port and health endpoint; EXPOSE is only a hint.",
                                  "Q3.", "Q4.", "Q5.", "Q6."]}
    text = chat.analysis(result, application="demo")
    assert text.startswith("**`demo` read at `dede786`: a Python web service the platform can run.**")
    assert "- Draft settings: port 8080, Python 3.12, 80 requests in parallel per instance, 1 CPU and 512 MiB, up to 3 instances" in text
    assert "- Found: Python dependency manifest found" in text and "- Services it uses that the platform does not provide: redis" in text
    assert "- Needs the dependencies suggest: database: postgres (psycopg in the dependencies)" in text
    assert "To confirm before deploying:\n1. Confirm stateless HTTP service; source files cannot prove this.\n2. Confirm port and health endpoint" in text
    assert "5. Q5.\n…and 1 more in the details" in text and text.index("- Draft settings") < text.index("To confirm") < text.index("**Next:**")
    assert "**Next:** nothing changed; a person confirms these and reviews `platform.yaml`" in text
    unknown = chat.analysis({"commit_sha": SHA, "path": "backend", "supported_candidate": False, "evidence": [], "draft_spec": {}, "review_required": []}, application="x")
    assert unknown.startswith("**`x`, folder `backend` read at `dede786`: no Python or Node.js dependency list was found**")


def test_a_rightsizing_proposal_names_its_pull_request_or_says_there_is_nothing():
    result = {"application": "demo", "changes": [{"setting": "resources.memory_mib", "value": 512}], "estimated_monthly_saving_usd": 0.5,
              "cost": {"worst_case_before_usd_month": 132.71, "worst_case_after_usd_month": 66.36, "idle_floor_before_usd_month": 0.0, "idle_floor_after_usd_month": 0.0},
              "pull_request": {"number": 11, "url": "https://github.com/lab/demo/pull/11", "branch": "b", "changed": ["platform.yaml"]}}
    text = chat.rightsizing(result)
    assert text.startswith("**Right-sizing pull request opened for `demo`**: [#11](https://github.com/lab/demo/pull/11), saving about $0.50 a month.")
    assert "- Changes: memory (MiB) to 512" in text and "- Compute at most $132.71 a month now, $66.36 after; with no visitors $0 now, $0 after" in text
    assert "**Next:** a person reviews and merges it; the next release applies it. Nothing changes until then." in text
    nothing = chat.rightsizing({"application": "demo", "pull_request": None, "changes": [], "note": "The latest report proposes no concrete setting change."})
    assert nothing.startswith("**Nothing to propose for `demo`**: the latest report proposes no concrete setting change.") and "next week" in nothing
    assert chat.rightsizing({**result, "pull_request": {"number": 11, "url": "u", "changed": []}}).startswith("**A right-sizing pull request is already open for `demo`**")


def test_every_tool_tells_claude_to_answer_from_the_summary_and_every_parameter_says_what_it_is():
    from labdeploy import toolpin
    from labdeploy.server import SUMMARY_RULE
    tools = toolpin.listed_tools()
    assert len(tools) == 28 and SUMMARY_RULE.startswith("Answer from `summary` as written")
    for tool in tools:
        assert tool["description"].endswith(" " + SUMMARY_RULE), tool["name"]
        assert len(tool["description"]) <= 500, (tool["name"], len(tool["description"]))  # claude.ai shows the description, not the server's instructions
        for name, prop in tool["inputSchema"].get("properties", {}).items():
            assert prop.get("description"), (tool["name"], name)
    logs = next(tool for tool in tools if tool["name"] == "get_recent_logs")["inputSchema"]["properties"]
    assert logs["severity"]["description"].startswith("The lowest level to include") and logs["minutes"]["default"] == 30 and logs["limit"]["default"] == 50
    status = next(tool for tool in tools if tool["name"] == "get_deployment_status")["inputSchema"]["properties"]
    assert "45" in status["wait_seconds"]["description"] and "list_plans" in status["plan_id"]["description"]


def test_the_five_tools_that_answered_with_raw_json_now_start_with_a_summary(tmp_path):
    config = Config.model_validate({"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
                                    "database": str(tmp_path / "db"),
                                    "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True}]})
    headers = {"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"}
    with TestClient(build_app(config, FakeGitHub())) as client:
        def call(name, **arguments):
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
            return client.post("/mcp", headers=headers, json=body).json()["result"]["structuredContent"]
        assert call("list_plans")["summary"].startswith("**No plans yet**")
        call("create_deployment_plan", application="demo")
        listed = call("list_plans")
        assert list(listed)[0] == "summary" and listed["summary"].startswith("**1 plan for 1 app**: 1 planned but not released.")
        assert "| `demo` | planned but not released | `aaaaaaa` (Fix the login bug) |" in listed["summary"] and len(listed["plans"]) == 1
        events = call("list_events")
        assert list(events)[0] == "summary" and "| When (UTC) |" in events["summary"] and "| manager | `create_deployment_plan` | `demo` | ok |" in events["summary"]
        report = call("get_cost_report", application="demo")
        assert report["summary"].startswith("**No measured cost report yet for `demo`.**") and report["report"] is None
        analysis = call("analyze_repository", application="demo")
        assert analysis["summary"].startswith("**`demo` read at `aaaaaaa`") and "To confirm before deploying:" in analysis["summary"]
        by_hand = call("get_onboarding_status", application="demo")
        assert by_hand["summary"].startswith("**`demo` was registered by hand**") and by_hand["state"] == "registered_by_hand"
        assert "registered by hand" in by_hand["next_action"] and "onboard_application" not in by_hand["next_action"]
