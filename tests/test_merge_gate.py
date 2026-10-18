"""The controller merges its own pull request only after its checks finished and passed (found live, 2026-10-18).

lab-bare has no required checks, so GitHub called the onboarding's workflows pull request (lab-bare#6) mergeable while its
tests were still running, and the controller merged it twelve seconds before they ended.
"""
from test_onboarding import PLATFORM, make

from labdeploy.github import CHECKS_GRACE_SECONDS, checks_verdict, parse_expiration

RUNNING = {"name": "tests", "status": "in_progress", "conclusion": None, "url": "https://github.com/lab/newapp/actions/runs/7"}


def test_the_verdict_waits_for_running_checks_and_stops_at_a_failed_one():
    assert checks_verdict([RUNNING]) == ("wait", "its checks to finish (tests)")
    failed = {**RUNNING, "status": "completed", "conclusion": "failure"}
    assert checks_verdict([failed]) == ("failed", "tests ended failure (https://github.com/lab/newapp/actions/runs/7)")
    assert checks_verdict([{**failed, "conclusion": "cancelled"}])[0] == "failed"
    fine = [{**failed, "conclusion": conclusion} for conclusion in ("success", "skipped", "neutral")]
    assert checks_verdict(fine) == ("passed", None)


def test_with_no_run_yet_it_gives_github_two_minutes_to_start_them():
    opened = "2026-10-18T02:53:17Z"
    at = parse_expiration(opened).timestamp()
    assert checks_verdict([], opened, now=at + 12) == ("wait", "GitHub to start its checks")
    assert checks_verdict([], opened, now=at + CHECKS_GRACE_SECONDS + 1) == ("passed", None)  # a repository without checks
    assert checks_verdict([]) == ("passed", None)


def onboard_to_the_workflows_merge(tmp_path):
    c, brian, gh = make(tmp_path)
    c.onboard(brian, "https://github.com/lab/newapp")
    gh.runs[(PLATFORM, "infra-plan.yml", "d" * 40)] = [{"id": 1, "status": "completed", "conclusion": "success", "url": "u1", "head_sha": "d" * 40}]
    gh.comments[(PLATFORM, 10)] = ["<!-- lab-deploy-infra:newapp -->\n### Infrastructure plan for `newapp`\n\n12 to add.\n\n**Policy: passed.** Merging applies exactly this plan."]
    c.advance_onboarding("newapp")
    gh.runs[(PLATFORM, "infra-apply.yml", "9" * 40)] = [{"id": 2, "status": "completed", "conclusion": "success", "url": "u2", "head_sha": "9" * 40}]
    assert c.advance_onboarding("newapp")["state"] == "merging_workflows"
    return c, brian, gh


def test_onboarding_does_not_merge_its_workflows_while_their_checks_run(tmp_path):
    c, brian, gh = onboard_to_the_workflows_merge(tmp_path)
    gh.prs[("lab/newapp", 11)]["mergeable_state"] = "unstable"  # what GitHub says while a check that is not required runs
    gh.head_checks = lambda repository, sha: [RUNNING]
    status = c.advance_onboarding("newapp")
    assert status["state"] == "merging_workflows" and ("lab/newapp", 11) not in gh.merges
    assert status["steps"]["merging_workflows"]["waiting"].startswith("its checks to finish (tests)")
    gh.head_checks = lambda repository, sha: [{**RUNNING, "status": "completed", "conclusion": "failure"}]
    blocked = c.advance_onboarding("newapp")
    assert blocked["state"] == "blocked" and "checks failed: tests ended failure" in blocked["error"] and ("lab/newapp", 11) not in gh.merges
    gh.head_checks = lambda repository, sha: [{**RUNNING, "status": "completed", "conclusion": "success"}]
    done = c.onboard(brian, "lab/newapp")  # asked again after the fix: it continues from the merge
    assert done["state"] == "complete" and ("lab/newapp", 11) in gh.merges
