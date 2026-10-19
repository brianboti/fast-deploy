"""Phase 16, part seven: operations (docs/ROADMAP.md item 16.7): release tags per promotion, production's spend in the weekly
report, the cap-fire drill and the incident procedure."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest
import yaml
from test_promotion import COMMIT, DIGEST, RUN, STAGING_IMAGE, STAGING_PLAN, ready_controller, staging_release

from labdeploy import chat
from labdeploy.cost import billing_actuals, issue_body, summary_lines, usd
from labdeploy.github import GitHub
from labdeploy.infra import check_plan

STEPS = [{"name": name, "status": "completed", "conclusion": "success"} for name in (
    "Refuse to run without a reviewer's approval", "Verify the staging image's provenance", "Copy the image into production by digest",
    "Deploy, probe, promote; restore traffic on failure")]


def finished(c, brian, gh, plan_id, conclusion="success", state="succeeded"):
    gh.run = {"id": 77, "status": "completed", "conclusion": conclusion, "html_url": RUN, "created_at": "2026-10-17T11:00:00Z",
              "updated_at": "2026-10-17T11:30:00Z", "jobs": [{"name": "describe", "steps": []}, {"name": "promote", "steps": STEPS}]}
    gh.receipt = {"release": {"state": state, "revision": "demo-r1234", "url": "https://demo-prod.a.run.app",
                              "promotion": {"source_plan": STAGING_PLAN, "source_image": STAGING_IMAGE, "approved_by": ["brianboti"]}}}
    return c.status(brian, plan_id)


# ---- release tags
def test_a_promotion_that_succeeded_tags_the_promoted_commit_once(tmp_path):
    c, brian, gh = ready_controller(tmp_path)
    staging_release(c)
    tags = []
    gh.create_tag = lambda repository, name, sha, message: tags.append((repository, name, sha, message)) or {
        "name": name, "url": f"https://github.com/{repository}/releases/tag/{name}", "existed": False}
    plan_id = c.promote(brian, "demo")["id"]
    done = finished(c, brian, gh, plan_id)
    repository, name, sha, message = tags[0]
    assert (repository, sha, name) == ("lab/demo", COMMIT, f"production/demo/2026-10-17-{plan_id[:8]}")  # the day the run finished
    assert message.startswith("demo promoted to production on 2026-10-17 by Fast Deploy, approved by brianboti.")
    assert f"Plan: {plan_id}" in message and f"Staging release: {STAGING_PLAN}" in message and f"Image: {DIGEST}" in message and f"Run: {RUN}" in message
    assert done["release_tag"] == {"name": name, "repository": "lab/demo", "sha": COMMIT, "created": True,
                                   "url": f"https://github.com/lab/demo/releases/tag/{name}"}
    assert f"Tagged [`{name}`](https://github.com/lab/demo/releases/tag/{name}) in `lab/demo`" in chat.release(done)
    c.status(brian, plan_id)
    assert len(tags) == 1  # a finished plan is read from the store; its tag is never written twice
    assert [e["outcome"] for e in c.store.recent_events(40) if e["kind"] == "release_tag"] == ["created"]


def test_a_tag_that_cannot_be_written_is_reported_and_the_release_stands(tmp_path):
    c, brian, gh = ready_controller(tmp_path)
    staging_release(c)

    def refuse(repository, name, sha, message):
        raise ValueError(f"The Fast Deploy GitHub App is not installed on {repository}. Install it there and try again.")
    gh.create_tag = refuse
    done = finished(c, brian, gh, c.promote(brian, "demo")["id"])
    assert done["state"] == "succeeded" and done["release_tag"]["created"] is False and "not installed on lab/demo" in done["release_tag"]["note"]
    assert "**`demo` is live in production**" in chat.release(done) and "No release tag in `lab/demo`: The Fast Deploy GitHub App" in chat.release(done)
    assert [e["outcome"] for e in c.store.recent_events(40) if e["kind"] == "release_tag"] == ["failed"]


def test_only_a_promotion_that_succeeded_is_tagged(tmp_path):
    c, brian, gh = ready_controller(tmp_path)
    staging_release(c)
    tags = []
    gh.create_tag = lambda *args: tags.append(args) or {"name": args[1], "url": None, "existed": False}
    stopped = finished(c, brian, gh, c.promote(brian, "demo")["id"], conclusion="failure", state="traffic_restored")
    assert stopped["state"] == "failed" and "release_tag" not in stopped and tags == []


def test_the_github_adapter_writes_an_annotated_tag_and_never_moves_one(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-credential")
    seen, refs = [], set()

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path == "/repos/lab/demo/git/tags":
            return httpx.Response(201, json={"sha": "f" * 40})
        if request.url.path == "/repos/lab/demo/git/refs":
            ref = json.loads(request.content)["ref"]
            if ref in refs or ref.endswith("/refused"):
                return httpx.Response(422, json={"message": "Reference already exists" if ref in refs else "Repository rule violations found"})
            refs.add(ref)
            return httpx.Response(201, json={"ref": ref})
        if request.url.path.startswith("/repos/lab/demo/git/ref/tags/"):
            ref = "refs/tags/" + request.url.path.removeprefix("/repos/lab/demo/git/ref/tags/")
            return httpx.Response(200, json={"ref": ref}) if ref in refs else httpx.Response(404, json={})
        return httpx.Response(404, json={})
    gh = GitHub(httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)))
    name = "production/demo/2026-10-17-12345678"
    assert gh.create_tag("lab/demo", name, COMMIT, "promoted") == {"name": name, "url": f"https://github.com/lab/demo/releases/tag/{name}", "existed": False}
    assert seen[0][2] == {"tag": name, "message": "promoted", "object": COMMIT, "type": "commit"}  # annotated: the tag object first
    assert seen[1][2] == {"ref": f"refs/tags/{name}", "sha": "f" * 40}
    assert gh.create_tag("lab/demo", name, COMMIT, "again")["existed"] is True  # asked twice: left where it is
    with pytest.raises(httpx.HTTPStatusError):  # refused for another reason: raised, so the plan records why
        gh.create_tag("lab/demo", "production/demo/refused", COMMIT, "promoted")


# ---- production's spend in the weekly report
def export(rows):
    captured = {}

    def handler(request):
        body = json.loads(request.content)
        if "MAX(usage_end_time)" in body["query"]:  # the export is fresh
            return httpx.Response(200, json={"jobComplete": True, "rows": [{"f": [{"v": str(int(time.time()) - 3600)}]}]})
        captured["body"] = body
        return httpx.Response(200, json={"jobComplete": True, "rows": [{"f": [{"v": value} for value in row]} for row in rows]})
    return httpx.Client(transport=httpx.MockTransport(handler)), captured


TABLE = "test-project.billing_export.gcp_billing_export_v1_ABC"


def test_the_weekly_report_reads_production_s_spend_from_the_same_export():
    client, captured = export([("test-project", "Cloud Run", "demo", "1.5"), ("lab-deploy-prod-abcd", "Cloud Run", "demo", "2.0"),
                               ("lab-deploy-prod-abcd", "Cloud Monitoring", None, "0.25"), ("test-project", "Artifact Registry", "other", "0.5")])
    result = billing_actuals(TABLE, "test-project", "t", client=client, app="demo", production="lab-deploy-prod-abcd")
    assert "project.id IN UNNEST(@projects)" in captured["body"]["query"]
    assert captured["body"]["queryParameters"][0]["parameterValue"]["arrayValues"] == [{"value": "test-project"}, {"value": "lab-deploy-prod-abcd"}]
    assert result["total_net_cost_usd"] == 4.25 and result["by_project"] == [
        {"project": "test-project", "environment": "staging", "net_cost_usd": 2.0},
        {"project": "lab-deploy-prod-abcd", "environment": "production", "net_cost_usd": 2.25}]
    own = result["app"]
    assert own["net_cost_usd"] == 3.5 and own["by_environment"] == {"staging": 1.5, "production": 2.0}
    assert own["by_service"] == [{"service": "Cloud Run", "net_cost_usd": 1.5}] and own["production_by_service"] == [{"service": "Cloud Run", "net_cost_usd": 2.0}]
    report = {"service": "demo", "window_days": 14, "generated_at": "2026-10-19T06:30:00Z", "actual_spend": result,
              "estimate": {"compute_ceiling_usd_month": 12.5, "idle_floor_usd_month": 0.0}, "rightsizing": {"observed": {}, "recommendations": []}}
    lines = summary_lines(report)
    assert any("$4.25 for staging and production together (staging $2.00, production $2.25;" in line for line in lines)
    assert "Of which labelled demo: $3.50 (staging $1.50, production $2.00)" in lines
    body = issue_body(report, "https://github.com/lab/demo/actions/runs/1")
    assert "- staging (`test-project`): $2.00\n- production (`lab-deploy-prod-abcd`): $2.25" in body and "By Google service, both projects:" in body
    text = chat.cost_report({"report": report}, "demo")
    assert "$4.25 for staging and production together (staging $2.00, production $2.25)" in text
    assert "`demo`'s share $3.50 (staging $1.50, production $2.00)" in text


def test_a_production_without_spend_shows_zero_and_staging_alone_reads_as_before():
    client, _ = export([("test-project", "Cloud Run", "demo", "1.5")])
    quiet = billing_actuals(TABLE, "test-project", "t", client=client, app="demo", production="lab-deploy-prod-abcd")
    assert quiet["by_project"][1] == {"project": "lab-deploy-prod-abcd", "environment": "production", "net_cost_usd": 0.0}
    assert quiet["app"]["by_environment"] == {"staging": 1.5, "production": 0.0}
    client, captured = export([("test-project", "Cloud Run", "demo", "1.5")])
    alone = billing_actuals(TABLE, "test-project", "t", client=client, app="demo")
    assert captured["body"]["queryParameters"][0]["parameterValue"]["arrayValues"] == [{"value": "test-project"}]
    assert "by_environment" not in alone["app"] and "together" not in "\n".join(summary_lines(
        {"service": "demo", "window_days": 14, "actual_spend": alone, "estimate": {"compute_ceiling_usd_month": 1, "idle_floor_usd_month": 0},
         "rightsizing": {"observed": {}, "recommendations": []}}))
    for wrong in ("test-project", "Not A Project"):
        with pytest.raises(ValueError, match="another project's id"):
            billing_actuals(TABLE, "test-project", "t", production=wrong)


def test_a_net_spend_a_fraction_of_a_cent_below_zero_reads_as_nothing():
    """Found in the first weekly report that covered production (2026-10-19): credits a fraction of a cent above the cost
    printed "$-0.00", and parts below a cent read "$0.00" beside a total of "$0.01". The figures are that report's."""
    assert (usd(-0.0003), usd(-0.0049), usd(0), usd(0.0012), usd(2.0), usd(-1.234)) == ("$0.00", "$0.00", "$0.00", "under $0.01", "$2.00", "-$1.23")
    assert (chat.money(-0.0003), chat.money(-1.5), chat.money(0.004)) == ("$0", "-$1.50", "under $0.01")
    actual = {"window_days": 30, "total_net_cost_usd": -0.0003, "by_service": [{"service": "Container Registry Vulnerability Scanning", "net_cost_usd": 0.0208}],
              "by_app": [{"app": "lab-demo", "net_cost_usd": 0.0054}],
              "by_project": [{"project": "lab-deploy-staging-f29a", "environment": "staging", "net_cost_usd": -0.0015},
                             {"project": "fast-deploy-prod-c602", "environment": "production", "net_cost_usd": 0.0012}],
              "app": {"name": "lab-demo", "net_cost_usd": 0.0054, "by_environment": {"staging": 0.0042, "production": 0.0012}}}
    report = {"service": "lab-demo", "window_days": 14, "actual_spend": actual, "estimate": {"compute_ceiling_usd_month": 1, "idle_floor_usd_month": 0},
              "rightsizing": {"observed": {}, "recommendations": []}}
    lines = summary_lines(report)
    assert any("net of credits: $0.00 for staging and production together (staging $0.00, production under $0.01;" in line for line in lines)
    assert "Of which labelled lab-demo: $0.01 (staging under $0.01, production under $0.01)" in lines
    body = issue_body(report, "https://github.com/lab/demo/actions/runs/1")
    assert "**$0.00**" in body and "- production (`fast-deploy-prod-c602`): under $0.01" in body and "$-" not in body
    text = chat.cost_report({"report": report}, "demo")
    assert "$0 for staging and production together (staging $0, production under $0.01)" in text and "$-" not in text


def test_add_production_names_production_to_the_weekly_report_in_staging(tmp_path):
    c, brian, gh = ready_controller(tmp_path)
    assert gh.variables[("lab/demo", "staging")] == {"PRODUCTION_PROJECT_ID": "lab-deploy-prod-abcd"}  # the app's own staging environment
    for name in (".github/workflows/rightsize.yml", "templates/rightsize-gcp.yml"):
        workflow = yaml.safe_load(Path(name).read_text())
        measure = next(step for step in workflow["jobs"]["rightsize"]["steps"] if step.get("run") == "python -m labdeploy.cost")
        assert measure["env"]["PRODUCTION_PROJECT_ID"] == "${{ vars.PRODUCTION_PROJECT_ID }}", name


# ---- the cap-fire drill
CAP = Path("infra/gcp/modules/project/cap-workflow.yaml")

FAKE = r'''#!/usr/bin/env python3
"""gcloud and curl for the drill: a project with services, the cap and its executions, in a JSON file."""
import base64, json, os, pathlib, sys, urllib.parse
path = pathlib.Path(os.environ["FAKE_STATE"])
state = json.loads(path.read_text())
tool, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]


def fire(message):
    data = json.loads(message)
    if data["costAmount"] / data["budgetAmount"] >= 1.0:
        for service in state["services"]:
            if service["labels"].get("lab-deploy-managed") == "true":
                service["ingress"] = "INGRESS_TRAFFIC_INTERNAL_ONLY"
        result = "cap enforced at 1.0001 of the budget: 1 services closed, 0 databases stopped"
    event = {"data": {"message": {"data": base64.b64encode(message.encode()).decode()}}}
    state["executions"].insert(0, {"state": "SUCCEEDED", "argument": json.dumps(event), "result": json.dumps(result)})


if tool == "gcloud":
    if args[:2] == ["workflows", "describe"]:
        print(state["source"])
    elif args[:2] == ["auth", "print-access-token"]:
        print("token")
    elif args[:3] == ["sql", "instances", "list"]:
        print("[]")
    elif args[:3] == ["pubsub", "topics", "publish"]:
        state["published"] = args[args.index("--message") + 1]
        fire(state["published"])
elif "-w" in args:
    print("404" if state["services"][0]["ingress"] == "INGRESS_TRAFFIC_INTERNAL_ONLY" else "403", end="")
elif "PATCH" in args:
    state["services"][0]["ingress"] = json.loads(args[args.index("-d") + 1])["ingress"]
    state["patches"] = state.get("patches", 0) + 1
elif "/executions" in args[-1]:
    print(json.dumps({"executions": state["executions"]}))
elif os.environ.get("FAKE_FAIL_LISTING") and "published" in state:
    sys.exit(56)  # the Cloud Run API answering 503 from the moment the drill has published
else:
    print(json.dumps({"services": state["services"]}))
path.write_text(json.dumps(state))
'''


def drill(tmp_path, detach="false", ingress="INGRESS_TRAFFIC_ALL", **extra):
    folder = tmp_path / "bin"
    folder.mkdir(parents=True)
    (folder / "fake.py").write_text(FAKE)
    (folder / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for name in ("fake.py", "sleep"):
        (folder / name).chmod(0o755)
    for name in ("gcloud", "curl"):
        (folder / name).symlink_to(folder / "fake.py")
    source = CAP.read_text().replace("__THRESHOLD__", "1").replace("__DETACH__", detach).replace("__DETACH_THRESHOLD__", "1.2")
    service = {"name": "projects/p/locations/us-central1/services/demo", "labels": {"lab-deploy-managed": "true"}, "ingress": ingress,
               "uri": "https://demo-abc.a.run.app"}
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"source": source, "executions": [], "services": [service]}))
    environment = {**os.environ, "PATH": f"{folder}:{os.environ['PATH']}", "FAKE_STATE": str(state), **extra}
    ran = subprocess.run(["zsh", "scripts/cap-drill.sh", "fast-deploy-prod-abcd"], input="yes\n", capture_output=True, text=True, env=environment, timeout=60)
    return ran, json.loads(state.read_text())


def test_the_cap_drill_never_reaches_the_detach_line_and_reopens_what_it_closed():
    script = Path("scripts/cap-drill.sh")
    text = script.read_text()
    assert script.stat().st_mode & 0o111 and "gcloud pubsub topics publish lab-deploy-budget-alerts" in text  # the path a real notification takes
    assert """[[ "$SOURCE" == *'${"false" == "true" and exceeded >='* ]]""" in text  # detach off, as rendered, or refused
    assert '${"__DETACH__" == "true" and exceeded >= __DETACH_THRESHOLD__}' in CAP.read_text() and "${exceeded < __THRESHOLD__}" in CAP.read_text()
    assert "?updateMask=ingress" in text and "trap finish EXIT" in text  # the cap's own call (no new revision), also after a failure
    assert "lab-deploy-cap-lifted" in text and "a second delivery" in text
    # Found in the first drill on production (2026-10-19): zsh skips the EXIT trap when errexit fires inside a function, so
    # after publishing nothing may end the drill early; an interrupt still goes through the trap.
    published = text.split("trap finish EXIT", 1)[1]
    assert "set +e" in published and "trap 'exit 130' INT TERM HUP" in published and "services || continue" in published
    assert "left=${#OPEN}" in published and "for attempt in 1 2 3 4 5; do" in text  # never "all closed" unseen; five tries to reopen


@pytest.mark.skipif(shutil.which("zsh") is None, reason="the operator's scripts run in zsh")
def test_the_cap_drill_closes_then_reopens_and_refuses_what_it_must(tmp_path):
    ran, state = drill(tmp_path / "armed")
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "demo answers 404 from outside" in ran.stdout and "SUCCEEDED | cap enforced at 1.0001 of the budget" in ran.stdout
    assert json.loads(state["published"]) == {"budgetDisplayName": "Fast Deploy cap drill", "alertThresholdExceeded": 1.0, "costAmount": 100.01,
                                              "budgetAmount": 100, "currencyCode": "USD"}
    assert state["services"][0]["ingress"] == "INGRESS_TRAFFIC_ALL" and "PASSED" in ran.stdout
    detach, state = drill(tmp_path / "detach", detach="true")
    assert detach.returncode == 1 and "REFUSED" in detach.stdout and "published" not in state
    enforced, state = drill(tmp_path / "enforced", ingress="INGRESS_TRAFFIC_INTERNAL_ONLY")
    assert enforced.returncode == 1 and "the cap may be in force now" in enforced.stdout and "published" not in state
    bad = subprocess.run(["zsh", "scripts/cap-drill.sh", "Not A Project"], capture_output=True, text=True)
    assert bad.returncode == 1 and "project id" in bad.stdout
    unread, state = drill(tmp_path / "unread", FAKE_FAIL_LISTING="1")  # the API fails once the services are closed
    assert unread.returncode == 2 and "not seen internal-only" in unread.stdout and "Reopened demo (INGRESS_TRAFFIC_ALL)" in unread.stdout
    assert state["services"][0]["ingress"] == "INGRESS_TRAFFIC_ALL"  # reopened all the same


def test_the_cap_may_act_as_an_application_s_runtime_identity_and_nothing_else():
    """Found by the first cap-fire drill on production (2026-10-19): Cloud Run refused the cap's ingress change with
    "Permission 'iam.serviceaccounts.actAs' denied" on the service's runtime identity, so the cap had never closed anything."""
    module = Path("infra/gcp/modules/service/main.tf").read_text()
    grant = module.split('resource "google_service_account_iam_member" "cap_runtime"', 1)[1].split("}", 1)[0]
    assert "service_account_id = google_service_account.runtime.name" in grant and 'role               = "roles/iam.serviceAccountUser"' in grant
    cap = "serviceAccount:lab-deploy-spending-cap@lab-deploy-prod-abcd.iam.gserviceaccount.com"
    runtime = "projects/lab-deploy-prod-abcd/serviceAccounts/demo-runtime@lab-deploy-prod-abcd.iam.gserviceaccount.com"

    def plan(after, unknown=None):
        return {"resource_changes": [{"address": "module.service.google_service_account_iam_member.cap_runtime[0]", "type": "google_service_account_iam_member",
                                      "change": {"actions": ["create"], "before": None, "after": after, "after_unknown": unknown or {}}}]}
    assert check_plan(plan({"service_account_id": runtime, "role": "roles/iam.serviceAccountUser", "member": cap})) == []
    assert check_plan(plan({"role": "roles/iam.serviceAccountUser", "member": cap}, {"service_account_id": True})) == []  # a new application
    deployer = runtime.replace("demo-runtime@", "demo-deploy@")
    assert [v["rule"] for v in check_plan(plan({"service_account_id": deployer, "role": "roles/iam.serviceAccountUser", "member": cap}))] == [
        "platform_identities_untouched"]
    assert [v["rule"] for v in check_plan(plan({"service_account_id": runtime, "role": "roles/iam.serviceAccountTokenCreator", "member": cap}))] == [
        "platform_identities_untouched", "role_not_allowed"]


# ---- the incident procedure
def test_the_runbook_has_an_incident_procedure_for_one_operator_and_both_drills():
    runbook = Path("docs/RUNBOOK.md").read_text()
    incident = runbook.split("## An incident\n", 1)[1].split("\n## ", 1)[0]
    for step in ("**Detect and decide.**", "**Mitigate first**", "**Communicate.**", "**Review**"):
        assert step in incident
    assert "NIST SP 800-61 Rev. 3" in incident and "rollback_production" in incident and "never in the public platform" in incident
    assert "### The cap-fire drill" in runbook and "scripts/cap-drill.sh PROD_PROJECT" in runbook and "### Release tags" in runbook
