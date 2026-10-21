"""Phase 3: actual spend from the billing export, the cost-report issue, right-sizing pull requests, the schedule watchdog."""
import hashlib
import json
import re
import time
from pathlib import Path

import httpx
import pytest
import yaml
from conftest import TARGET, FakeGitHub

from labdeploy.config import Config
from labdeploy.controller import Controller, Store
from labdeploy.cost import billing_actuals, issue_body
from labdeploy.rightsize import apply_settings

DEMO_YAML = Path("examples/demo-app/platform.yaml").read_text()


def test_apply_settings_edits_values_in_place_and_keeps_comments():
    text, applied, regenerated = apply_settings(DEMO_YAML, {"resources.memory_mib": 1024, "concurrency": 160, "scaling.minimum": 0})
    assert not regenerated and {a["setting"] for a in applied} == {"resources.memory_mib", "concurrency", "scaling.minimum"}
    assert "  memory_mib: 1024\n" in text and "concurrency: 160\n" in text and "# Requests served in parallel" in text and "reviewed: true" in text
    sparse = "api_version: lab/v1\nname: demo\nreviewed: true\n"
    text, applied, regenerated = apply_settings(sparse, {"resources.cpu": 2})
    assert regenerated and applied == [{"setting": "resources.cpu", "value": 2}] and "cpu: 2" in text


def test_billing_actuals_queries_the_export_and_sums_net_cost():
    captured = {}

    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/bigquery/v2/projects/test-project/queries" and request.headers["Authorization"] == "Bearer t"
        if "MAX(usage_end_time)" in body["query"]:  # the export is fresh
            return httpx.Response(200, json={"jobComplete": True, "rows": [{"f": [{"v": str(int(time.time()) - 3600)}]}]})
        captured["body"] = body
        here = {"v": "test-project"}
        return httpx.Response(200, json={"jobComplete": True, "rows": [{"f": [here, {"v": "Cloud Run"}, {"v": "demo"}, {"v": "1.5"}]},
                                                                      {"f": [here, {"v": "Artifact Registry"}, {"v": "demo"}, {"v": "0.25"}]},
                                                                      {"f": [here, {"v": "Cloud Run"}, {"v": None}, {"v": "0.5"}]}]})
    result = billing_actuals("test-project.billing_export.gcp_billing_export_v1_ABC", "test-project", "t", client=httpx.Client(transport=httpx.MockTransport(handler)), app="demo")
    assert result["total_net_cost_usd"] == 2.25 and result["by_service"][0] == {"service": "Cloud Run", "net_cost_usd": 2.0}
    assert result["by_app"] == [{"app": "demo", "net_cost_usd": 1.75}, {"app": "(unlabelled)", "net_cost_usd": 0.5}]
    assert result["app"] == {"name": "demo", "net_cost_usd": 1.75, "by_service": [{"service": "Cloud Run", "net_cost_usd": 1.5}, {"service": "Artifact Registry", "net_cost_usd": 0.25}]}
    assert "lab-deploy-app" in captured["body"]["query"] and "`test-project.billing_export.gcp_billing_export_v1_ABC`" in captured["body"]["query"]
    assert captured["body"]["queryParameters"][0]["parameterValue"]["arrayValues"] == [{"value": "test-project"}]  # staging alone
    assert result["by_project"] == [{"project": "test-project", "environment": "staging", "net_cost_usd": 2.25}] and "by_environment" not in result["app"]
    with pytest.raises(ValueError, match="project.dataset.table"):
        billing_actuals("not a table", "test-project", "t")


def test_issue_body_lists_summary_proposals_drift_and_actual_spend():
    report = {"service": "demo", "window_days": 14, "generated_at": "2026-10-12T06:23:00+00:00",
              "estimate": {"compute_ceiling_usd_month": 132.71, "idle_floor_usd_month": 0.0},
              "rightsizing": {"observed": {"usd_month_at_list_price": 0.0034, "requests": 6},
                              "recommendations": [{"setting": "resources.memory_mib", "current": 1024, "recommended": 512, "reason": "p95 memory 14%", "estimated_monthly_saving_usd": 0.5}]},
              "drift": [{"setting": "cpu", "platform_yaml": 1, "live": 2}],
              "actual_spend": {"window_days": 30, "total_net_cost_usd": 1.75, "by_service": [{"service": "Cloud Run", "net_cost_usd": 1.5}]}}
    body = issue_body(report, "https://example/run")
    assert "| `resources.memory_mib` | 1024 | 512 |" in body and "cpu` is 2 live but 1" in body and "**$1.75**" in body and "propose_rightsizing" in body


class Reporting(FakeGitHub):
    def __init__(self, recommendations):
        self.recommendations, self.prs = recommendations, []

    def cost_report(self, app, workflow="rightsize-gcp.yml"):
        return {"workflow_run_id": 1, "run_url": "https://example/run", "generated_at": "2026-10-12T06:23:00Z",
                "report": {"generated_at": "2026-10-12T06:23:00+00:00", "rightsizing": {"recommendations": self.recommendations}}}

    def file(self, app, sha, filename):
        return DEMO_YAML.replace("memory_mib: 512", "memory_mib: 1024")

    def open_pull_request(self, app, branch, files, title, body, message):
        self.prs.append((branch, files, title, body))
        return {"number": 11, "url": "https://github.com/lab/demo/pull/11", "branch": branch, "changed": list(files)}


def make(tmp_path, github, **extra):
    config = Config.model_validate({"live_dispatch": True, "database": str(tmp_path / "db"),
                                    "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET, **extra}},
                                    "principals": [{"name": "m", "token_sha256": hashlib.sha256(b"t").hexdigest(), "applications": ["demo"], "can_deploy": True}]})
    return Controller(config, github, Store(config.database)), config.principals[0]


def test_propose_rightsizing_opens_a_pull_request_with_savings_and_bounds(tmp_path):
    proposals = [{"setting": "resources.memory_mib", "current": 1024, "recommended": 512, "reason": "p95 memory 14%", "estimated_monthly_saving_usd": 0.5},
                 {"setting": "billing_model", "current": "request-based", "recommended": "instance-based (not offered)", "reason": "busy", "estimated_monthly_saving_usd": None}]
    gh = Reporting(proposals)
    c, p = make(tmp_path, gh)
    result = c.propose_rightsizing(p, "demo")
    assert result["pull_request"]["number"] == 11 and result["changes"] == [{"setting": "resources.memory_mib", "value": 512}] and result["estimated_monthly_saving_usd"] == 0.5
    assert result["cost"]["worst_case_after_usd_month"] < result["cost"]["worst_case_before_usd_month"]
    branch, files, title, body = gh.prs[0]
    assert branch == "lab-deploy/rightsize-20261012" and "memory_mib: 512" in files["platform.yaml"] and "# Requests served in parallel" in files["platform.yaml"]
    assert "Nothing is merged" in body and "$0.50 per month" in body
    assert "no concrete setting change" in make(tmp_path, Reporting([]))[0].propose_rightsizing(p, "demo")["note"]
    with pytest.raises(ValueError, match="cost ceiling"):
        bigger = Reporting([{"setting": "resources.cpu", "current": 1, "recommended": 2, "reason": "busy", "estimated_monthly_saving_usd": -1}])
        make(tmp_path, bigger, cost_ceiling_usd=150)[0].propose_rightsizing(p, "demo")
    with pytest.raises(ValueError, match="Staging policy"):
        outside = Reporting([{"setting": "resources.cpu", "current": 1, "recommended": 4, "reason": "busy", "estimated_monthly_saving_usd": -3}])
        make(tmp_path, outside)[0].propose_rightsizing(p, "demo")
    with pytest.raises(ValueError, match="No cost report yet"):
        make(tmp_path, FakeGitHub())[0].propose_rightsizing(p, "demo")


def test_schedule_watchdog_flags_paused_and_stale_schedules(tmp_path):
    class Scheduled(FakeGitHub):
        def schedule_health(self, app, workflow):
            if workflow == "recover-gcp.yml":
                return {"workflow": workflow, "state": "active", "last_scheduled_run": "2026-10-01T06:00:00Z", "last_conclusion": "success"}  # days ago: stale
            return {"workflow": workflow, "state": "disabled_inactivity", "last_scheduled_run": "2026-07-01T06:00:00Z", "last_conclusion": "failure"}
    c, p = make(tmp_path, Scheduled())
    schedules = c.applications(p)["demo"]["schedules"]
    assert schedules["recover-gcp.yml"]["ok"] is False and "hours ago" in schedules["recover-gcp.yml"]["problems"][0]
    stale = schedules["rightsize-gcp.yml"]
    assert stale["ok"] is False and any("disabled_inactivity" in s for s in stale["problems"]) and any("failure" in s for s in stale["problems"])
    assert make(tmp_path, FakeGitHub())[0].applications(p)["demo"]["schedules"] is None  # adapters without the check report nothing


def test_cap_workflow_renders_to_valid_yaml_with_complete_expressions():
    # Cloud Workflows rejected the first live deploy with "Unterminated expression": a colon followed by a space
    # inside an unquoted ${...} reads as a nested YAML key. Render the file the way Terraform does and parse it.
    source = Path("infra/gcp/modules/project/cap-workflow.yaml").read_text()
    placeholders = {
        "__THRESHOLD__": "1.0",
        "__PROJECT__": "proj",
        "__REGION__": "us-central1",
        "__DETACH__": "true",
        "__DETACH_THRESHOLD__": "1.5",
    }
    rendered = source
    for key, value in placeholders.items():
        assert key in source, key
        rendered = rendered.replace(key, value)
    assert not re.search(r"__[A-Z_]+__", rendered)
    document = yaml.safe_load(rendered)

    def scalars(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from scalars(value)
        elif isinstance(node, list):
            for value in node:
                yield from scalars(value)
        elif isinstance(node, str):
            yield node

    expressions = [text for text in scalars(document) if "${" in text]
    assert len(expressions) >= 10
    for text in expressions:
        assert text.startswith("${") and text.endswith("}") and text.count("{") == text.count("}"), text
    steps = [next(iter(step)) for step in document["main"]["steps"]]
    assert steps == ["decode", "ratio", "below_cap", "list_services", "nothing_to_close", "close_ingress", "list_instances", "nothing_to_stop",
                     "stop_instances", "list_jobs", "no_jobs", "cancel_runs", "compose", "log_message", "maybe_detach", "fail_if_needed", "done",
                     "report_below"]
    assert max(len(text) for text in expressions) <= 400  # Cloud Workflows refuses longer expressions
    assert "activationPolicy: NEVER" in rendered and "settings.userLabels.lab-deploy-managed:true" in rendered
    assert "{}" not in "".join(expressions) and "[]" not in "".join(expressions)  # Workflows expressions take no map or list literals
    # Services are discovered by label at run time rather than baked in, so adding an app needs no re-apply.
    assert "lab-deploy-managed" in rendered and "__" not in document["main"]["steps"][3]["list_services"]["try"]["args"]["url"]
    # Review H6: every call retries, each resource is handled in its own try so one failure never leaves the others
    # open, failures end the execution (the alert policy watches), the ratio comes from the amounts rather than from
    # the budget's own alert lines, and a lifted resource stays open until the operator removes the label.
    assert rendered.count("retry: ${http.default_retry}") >= 5 and rendered.count("except:") == 5  # services, databases; the jobs' listing, each job's runs, each cancel (27.7)
    assert "costAmount" in rendered and "budgetAmount" in rendered and "cost / budget" in rendered
    assert "lab-deploy-cap-lifted" in rendered and "raise:" in rendered
