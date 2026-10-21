"""Phase 16, part four: production's objective, its burn-rate alerts and the error budget (docs/ROADMAP.md D39, item 16.4)."""
import json
import types
from pathlib import Path

import httpx
import pytest
from conftest import TARGET
from test_promotion import ROOT, SPEC, ready_controller, staging_release

from labdeploy import chat
from labdeploy.cloud import Observer
from labdeploy.infra import PRODUCTION_SLO_GOAL, PRODUCTION_UPTIME_REGIONS, app_root
from labdeploy.target import Target


# ---- the module: one objective, two burn-rate alerts, the minute uptime check, production only
def test_production_has_one_objective_on_user_requests_with_three_burn_alerts():
    slo = Path("infra/gcp/modules/service/slo.tf").read_text()
    assert 'resource "google_monitoring_custom_service" "slo"' in slo and 'resource "google_monitoring_slo" "availability"' in slo
    assert "rolling_period_days = 30" in slo and "request_based_sli" in slo
    # Since 1.26.0 (D58): two log-based counters of the users' requests, the health path left out, so the uptime check's
    # calls neither dilute nor spend the objective.
    assert 'resource "google_logging_metric" "user_requests"' in slo and 'resource "google_logging_metric" "user_errors"' in slo
    assert 'var.health_probe ? "NOT httpRequest.requestUrl=~\\"^https?://[^/]+${var.health_path}([?].*)?$\\"" : ""' in slo
    assert 'filter      = "${local.user_requests_filter} AND httpRequest.status>=500"' in slo
    assert 'bad_service_filter   = "metric.type=\\"logging.googleapis.com/user/${google_logging_metric.user_errors[0].name}\\" AND resource.type=\\"cloud_run_revision\\""' in slo
    assert "run.googleapis.com/request_count" not in slo
    # Google's multiwindow pattern: a long and a short window (a twelfth), both burning, and at least 100 user requests.
    for name, long, short, threshold, severity in (("fast", "60m", "5m", 10, "CRITICAL"), ("slow", "360m", "30m", 2, "ERROR"),
                                                   ("steady", "1440m", "120m", 1, "WARNING")):
        assert f'long = "{long}", short = "{short}"' in slo and f"threshold = {threshold}, severity = \"{severity}\"" in slo, name
    assert 'combiner     = "AND"' in slo and "threshold_value = 99" in slo and 'alignment_period     = "${each.value.seconds}s"' in slo
    assert 'select_slo_burn_rate(\\"${google_monitoring_slo.availability[0].name}\\", \\"${each.value.long}\\")' in slo
    assert 'select_slo_burn_rate(\\"${google_monitoring_slo.availability[0].name}\\", \\"${each.value.short}\\")' in slo
    assert 'subject   = "[${local.environment_name}] ${var.service}: ${each.value.title}"' in slo
    main = Path("infra/gcp/modules/service/main.tf").read_text()
    assert "period           = var.uptime_period" in main and "selected_regions = length(var.uptime_regions) > 0 ? var.uptime_regions : null" in main
    variables = Path("infra/gcp/modules/service/variables.tf").read_text()
    assert 'variable "slo_goal"' in variables and "var.slo_goal == 0 ||" in variables and 'default     = "300s"' in variables


def test_every_alert_has_a_severity_labels_and_words_and_the_log_line_alert_is_gone():
    """D59: a severity on every policy, labels that name the application and environment, a subject and a documentation
    block that says what it means and what to ask; any ERROR log line is no longer an alert but a count in the digest."""
    import re
    main = Path("infra/gcp/modules/service/main.tf").read_text()
    assert 'resource "google_monitoring_alert_policy" "log_errors"' not in main and "condition_matched_log" not in main
    expected = {"health": "CRITICAL", "errors": "ERROR", "latency": "WARNING", "saturation": "WARNING", "memory": "WARNING"}
    for name, severity in expected.items():
        block = main[main.index(f'resource "google_monitoring_alert_policy" "{name}"'):]
        block = block[:block.index("\n}\n")]
        assert f'severity              = "{severity}"' in block and "lab-deploy-environment = local.environment_name" in block, name
        assert 'subject   = "[${local.environment_name}] ${var.service}:' in block and "ask claude" in block.lower(), name
    assert len(re.findall(r'resource "google_monitoring_alert_policy"', main)) == len(expected)
    project = Path("infra/gcp/modules/project/main.tf").read_text() + Path("infra/gcp/modules/project/controller.tf").read_text()
    assert project.count('severity              = "CRITICAL"') + project.count('severity    = "CRITICAL"') == 2
    assert 'subject   = "[${var.environment}] the spending cap could not act"' in project and 'subject   = "Fast Deploy controller: not answering"' in project


def test_only_a_production_root_renders_the_objective_and_the_minute_check():
    production = app_root("demo", project_id="lab-deploy-prod-abcd", environment="production", github_environment="production-demo",
                          workflow_files=["promote.yml"], pipeline="platform", **{**ROOT, "state_bucket": "lab-deploy-prod-abcd-tfstate"})["main.tf"]
    assert f"slo_goal                     = {PRODUCTION_SLO_GOAL}" in production and 'uptime_period                = "60s"' in production
    assert 'uptime_regions               = ["USA_OREGON", "USA_IOWA", "USA_VIRGINIA"]' in production and len(PRODUCTION_UPTIME_REGIONS) == 3
    staging = app_root("demo", project_id="test-project", **ROOT)["main.tf"]
    assert "slo_goal" not in staging and "uptime_period" not in staging  # staging's roots plan no change


# ---- reading the error budget
def observer(handler):
    config = types.SimpleNamespace(observer=types.SimpleNamespace(service_account="lab-deploy-observer@test-project.iam.gserviceaccount.com", cache_seconds=300))
    found = Observer(config, client=httpx.Client(transport=httpx.MockTransport(handler)))
    found.token = lambda: "token"
    return found


def test_the_error_budget_is_read_from_the_objective_itself():
    asked = []

    def handler(request):
        asked.append(request.url)
        if request.url.path.endswith("/serviceLevelObjectives/availability"):
            return httpx.Response(200, json={"goal": 0.995, "rollingPeriod": "2592000s"})
        selector = request.url.params["filter"]
        value = 0.9998 if selector.startswith("select_slo_compliance") else 0.96
        return httpx.Response(200, json={"timeSeries": [{"points": [{"interval": {"endTime": "2026-10-18T22:00:00Z"}, "value": {"doubleValue": 0.5}},
                                                                     {"interval": {"endTime": "2026-10-18T22:05:00Z"}, "value": {"doubleValue": value}}]}]})
    target = Target.model_validate({**TARGET, "project_id": "lab-deploy-prod-abcd", "deploy_service_account": "demo-deploy@lab-deploy-prod-abcd.iam.gserviceaccount.com",
                                    "probe_service_account": "demo-probe@lab-deploy-prod-abcd.iam.gserviceaccount.com"})
    budget = observer(handler).error_budget(target, "demo-production")
    assert budget == {"goal": 0.995, "days": 30, "compliance": 0.9998, "budget_remaining": 0.96,
                      "name": "projects/lab-deploy-prod-abcd/services/demo-production/serviceLevelObjectives/availability"}
    assert asked[1].params["filter"] == 'select_slo_compliance("projects/lab-deploy-prod-abcd/services/demo-production/serviceLevelObjectives/availability")'
    assert observer(lambda request: httpx.Response(404, json={})).error_budget(target, "demo-production") is None  # no objective yet


class Seen:
    """An observer that answers the health view's reads, for staging or for production."""

    def __init__(self, budget):
        self.budget, self.targets = budget, []

    def service(self, target):
        self.targets.append(target.project_id)
        return {"ready": True, "ingress": "INGRESS_TRAFFIC_ALL", "traffic": [{"revision": "demo-r1", "percent": 100, "commit": "a" * 40}]}

    def metrics(self, target, minutes):
        return {"window_minutes": minutes, "requests": 1200, "server_errors": 0, "uptime_checks_passed": 1.0}

    def needs(self, target, declared):
        return []

    def error_budget(self, target, service_id):
        self.asked = service_id
        return self.budget


def test_production_health_reads_production_and_names_what_is_left_of_the_budget(tmp_path):
    c, brian, gh = ready_controller(tmp_path)
    c.observer = Seen({"goal": 0.995, "days": 30, "compliance": 0.9998, "budget_remaining": 0.96, "name": "x"})
    view = c.service_health(brian, "demo", environment="production")
    assert c.observer.targets == ["lab-deploy-prod-abcd"] and c.observer.asked == "demo-production" and view["environment"] == "production"
    text = chat.health(view)
    assert text.startswith("**`demo` in production is healthy.**") and "Error budget: 96% left, 99.98% of requests fine so far" in text
    c.observer.budget = {"goal": 0.995, "days": 30, "compliance": 0.99, "budget_remaining": -0.1, "name": "x"}
    low = c.service_health(brian, "demo", environment="production")
    assert any("The 30-day error budget is spent, 10% beyond it" in note for note in low["notes"]) and "Error budget: spent" in chat.health(low)
    c.observer.budget = {"goal": 0.995, "days": 30, "compliance": 0.999, "budget_remaining": 0.2, "name": "x"}
    assert any("The 30-day error budget has only 20% left" in note for note in c.service_health(brian, "demo", environment="production")["notes"])
    c.observer.budget = None
    assert any("No objective yet in production" in note for note in c.service_health(brian, "demo", environment="production")["notes"])
    staging = c.service_health(brian, "demo")
    assert c.observer.targets[-1] == "test-project" and staging["error_budget"] is None and "Error budget" not in chat.health(staging)


def test_production_health_needs_a_production(tmp_path):
    from test_phase16 import controller
    c, brian, gh = controller(tmp_path)
    c.observer = Seen(None)
    with pytest.raises(ValueError, match="no production yet"):
        c.service_health(brian, "demo", environment="production")
    with pytest.raises(ValueError, match="staging or production"):
        c.service_health(brian, "demo", environment="qa")


# ---- the readiness checklist at the first promotion
def test_the_first_promotion_carries_the_readiness_checklist_and_the_next_does_not(tmp_path):
    c, brian, gh = ready_controller(tmp_path)
    staging_release(c)
    first = c.promote(brian, "demo")
    items = {item["item"]: item for item in first["readiness"]}
    assert list(items) == ["probes", "alerts", "runbook", "backup", "capacity", "rollback"]
    assert items["alerts"]["ok"] and items["probes"]["ok"] and first["approval"]["readiness"] == first["readiness"]
    record = c.store.fetch(first["id"])[1]
    record["receipt"] = {"state": "succeeded", "revision": "demo-r2"}
    c.store.finish("brian", record, "succeeded")
    assert "readiness" not in c.promote(brian, "demo")
    # a production root rendered before the objective: the checklist says what to do
    gh.on_main["infra/gcp/apps-production/demo/main.tf"] = "module \"service\" {}\n"
    assert not {item["item"]: item for item in c.readiness(c.config.applications["demo"], SPEC)}["alerts"]["ok"]


def test_the_get_service_health_tool_takes_the_environment():
    tools = json.loads(Path("schema/tools.sha256.json").read_text())
    assert "get_service_health" in tools
    source = Path("labdeploy/server.py").read_text()
    assert 'environment: Annotated[Literal["staging", "production"]' in source
