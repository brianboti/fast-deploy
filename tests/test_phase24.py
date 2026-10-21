"""Phase 24: cost between Mondays (docs/ROADMAP.md 24.1 to 24.4, D62 and D63). No test touches GitHub, BigQuery or a cloud."""
import datetime
import hashlib
import time
from pathlib import Path

import pytest
from conftest import TARGET, FakeGitHub
from test_phase23 import DAY, NOW, FakeObserver, release, store_with

from labdeploy import chat, cost
from labdeploy.config import Config
from labdeploy.controller import Controller, Store
from labdeploy.settings import change_platform

TABLE = "lab-deploy-staging-f29a.billing_export.gcp_billing_export_v1_X"


def config(tmp_path, **extra):
    base = {"live_dispatch": True, "database": str(tmp_path / "db"),
            "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
            "principals": [{"name": "m", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["*"], "can_deploy": True}],
            "onboarding": {"project_id": "test-project", "project_number": "123456789", "billing_export_table": TABLE}}
    base.update(extra)
    return Config.model_validate(base)


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self.data


def cell(service, app, amount, project="test-project"):
    return {"f": [{"v": project}, {"v": service}, {"v": app}, {"v": str(amount)}]}


# ---- 24.1 (D62): the window before, and what moved
def test_movers_are_the_lines_that_moved_by_a_fifth_and_a_dollar():
    now = [{"service": "Cloud Run", "net_cost_usd": 6.0}, {"service": "Artifact Analysis", "net_cost_usd": 2.6},
           {"service": "Cloud SQL", "net_cost_usd": 8.6}, {"service": "BigQuery", "net_cost_usd": 0.4}]
    before = [{"service": "Cloud Run", "net_cost_usd": 4.0}, {"service": "Artifact Analysis", "net_cost_usd": 2.4},
              {"service": "BigQuery", "net_cost_usd": 0.1}, {"service": "Logging", "net_cost_usd": 3.0}]
    found = cost.movers(now, before, "service")
    assert [item["service"] for item in found] == ["Cloud SQL", "Logging", "Cloud Run"]  # largest change first
    assert found[0]["change_share"] is None and found[1]["change_usd"] == -3.0 and found[2]["change_share"] == 0.5
    # Artifact Analysis moved 8 percent, BigQuery by 30 cents: neither is named


def test_the_weekly_report_reads_the_window_before_and_names_the_movers():
    class Client:
        bodies = []

        def post(self, url, json, headers, timeout):
            Client.bodies.append(json)
            if "MAX(usage_end_time)" in json["query"]:
                return Response({"jobComplete": True, "rows": [{"f": [{"v": str(time.time())}]}]})
            since = next(p["parameterValue"]["value"] for p in json["queryParameters"] if p["name"] == "days")
            rows = [cell("Cloud Run", "demo", 6.0), cell("Cloud SQL", "demo", 8.6)] if since == "30" else [cell("Cloud Run", "demo", 4.0)]
            return Response({"jobComplete": True, "rows": rows})
    result = cost.billing_actuals(TABLE, "test-project", "token", client=Client())
    windows = [{p["name"]: p["parameterValue"].get("value") for p in body["queryParameters"]} for body in Client.bodies[1:]]
    assert [(w["days"], w["until_days"]) for w in windows] == [("30", "0"), ("60", "30")]
    assert result["total_net_cost_usd"] == 14.6 and result["previous"]["total_net_cost_usd"] == 4.0
    assert [item["service"] for item in result["movers"]["by_service"]] == ["Cloud SQL", "Cloud Run"]
    assert [item["app"] for item in result["movers"]["by_app"]] == ["demo"]
    lines = cost.comparison_lines({"actual_spend": result})
    assert lines[0] == "The 30 days before: $4.00, against $14.60 now"
    assert lines[1].startswith("Moved most: Cloud SQL $0.00 to $8.60, new; Cloud Run $4.00 to $6.00, +50%")


def test_the_month_so_far_and_its_forecast():
    class Client:
        def post(self, url, json, headers, timeout):
            assert url.endswith("/projects/lab-deploy-staging-f29a/queries") and "invoice.month = @month" in json["query"]
            month = next(p["parameterValue"]["value"] for p in json["queryParameters"] if p["name"] == "month")
            assert month == "202610"
            return Response({"jobComplete": True, "totalBytesProcessed": "2048",
                             "rows": [{"f": [{"v": "lab-deploy-staging-f29a"}, {"v": "3.1"}]}]})
    middle = datetime.datetime(2026, 10, 16, tzinfo=datetime.UTC).timestamp()  # 15 of 31 days
    month = cost.month_to_date(TABLE, ["lab-deploy-staging-f29a", "fast-deploy-prod-c602"], "token", Client(), middle)
    assert month["month"] == "2026-10" and month["days_elapsed"] == 15 and month["days_in_month"] == 31 and not month["rough"]
    assert month["projects"] == [{"project": "lab-deploy-staging-f29a", "net_cost_usd": 3.1, "forecast_usd": 6.41},
                                 {"project": "fast-deploy-prod-c602", "net_cost_usd": 0.0, "forecast_usd": 0.0}]
    early = cost.month_to_date(TABLE, ["lab-deploy-staging-f29a"], "token", Client(), datetime.datetime(2026, 10, 2, tzinfo=datetime.UTC).timestamp())
    assert early["rough"] and early["projects"][0]["forecast_usd"] == round(3.1 * 31, 2)  # a day counts as at least one
    line = cost.comparison_lines({"month": month})[0]
    assert line == "This month (2026-10, 15 of 31 days): lab-deploy-staging-f29a $3.10 (on course for $6.41), fast-deploy-prod-c602 $0.00 (on course for $0.00)"

    class Refused:
        def post(self, url, json, headers, timeout):
            return Response({}, 403)
    with pytest.raises(ValueError, match="apply the foundation"):
        cost.month_to_date(TABLE, ["lab-deploy-staging-f29a"], "token", Refused(), middle)
    with pytest.raises(ValueError, match="project.dataset.table"):
        cost.month_to_date("not a table", ["x"], "token", Refused(), middle)


def test_the_cost_report_answer_compares_and_sets_the_month_against_the_budget():
    report = {"service": "demo", "window_days": 14, "generated_at": "2026-10-19T06:30:00+00:00",
              "actual_spend": {"window_days": 30, "total_net_cost_usd": 14.6, "by_project": [],
                               "previous": {"window_days": 30, "total_net_cost_usd": 4.0},
                               "movers": {"by_service": [{"service": "Cloud SQL", "net_cost_usd": 8.6, "before_usd": 0.0, "change_usd": 8.6,
                                                          "change_share": None}], "by_app": []}},
              "month": {"month": "2026-10", "days_elapsed": 15, "days_in_month": 31, "rough": False,
                        "projects": [{"project": "test-project", "net_cost_usd": 12.0, "forecast_usd": 24.8}]}}
    text = chat.cost_report({"report": report, "budgets": {"test-project": 20}, "cost_anomaly_detection": False}, application="demo")
    assert "- The 30 days before: $4.00; Cloud SQL moved most, $0 to $8.60" in text
    assert "- This month in `test-project`: $12.00 so far, on course for $24.80, above its $20.00 budget" in text
    assert "Google's free Cost Anomaly Detection is not recorded as on (docs/SETUP.md 7q)" in text
    assert "Cost Anomaly Detection" not in chat.cost_report({"report": report, "cost_anomaly_detection": True}, application="demo")


# ---- 24.2 (D63): the digest's cost line every day
class SpendingObserver(FakeObserver):
    def __init__(self, spent=None, refuse=None, **kwargs):
        super().__init__(**kwargs)
        self.spent, self.refuse = spent or {}, refuse

    def month_spend(self, table, projects, now=None):
        if self.refuse:
            raise ValueError(self.refuse)
        return {"month": "2026-10", "days_elapsed": 25.6, "days_in_month": 31, "rough": False,
                "projects": [{"project": project, "net_cost_usd": self.spent.get(project, 0.0),
                              "forecast_usd": round(self.spent.get(project, 0.0) / 25.6 * 31, 2)} for project in projects]}

    def policies(self, project_id):
        return {"alert_policies": 80, "enabled_alert_policies": 80, "uptime_checks": 13, "uptime_executions_month": 673920}

    def had_user_requests(self, target, days=30, health_path=None):
        return False


def test_the_digest_says_the_month_so_far_against_each_budget(tmp_path):
    cfg = config(tmp_path, monthly_budgets_usd={"test-project": 20}, cloud_run_spend_caps={"test-project": 20})
    c = Controller(cfg, FakeGitHub(), Store(cfg.database), SpendingObserver(spent={"test-project": 3.2}))
    c.watchdog_problems = lambda now=None: []
    tuesday = NOW + DAY
    record = c.build_digest(tuesday)
    assert record["cost"]["projects"] == [{"project": "test-project", "environment": "staging", "net_cost_usd": 3.2, "forecast_usd": 3.88,
                                           "budget_usd": 20, "native_cap_usd": 20, "over_budget": False}]
    text = chat.digest(record)
    assert text.startswith("**All quiet") and "- Spend this month (25.6 of 31 days): staging $3.20 of a $20.00 budget, on course for $3.88" in text
    over = Controller(cfg, FakeGitHub(), Store(cfg.database), SpendingObserver(spent={"test-project": 18.0}))
    over.watchdog_problems = lambda now=None: []
    record = over.build_digest(tuesday)
    assert record["attention"] and "`test-project` is on course for $21.80 this month, above its $20.00 budget" in chat.digest(record)


def test_the_cost_line_says_why_it_has_no_numbers(tmp_path):
    no_table = Config.model_validate({**config(tmp_path).model_dump(mode="json"), "onboarding": None})
    c = Controller(no_table, FakeGitHub(), Store(str(tmp_path / "db")), SpendingObserver())
    assert c.cost_position()["warning"].startswith("no billing export is recorded")
    refused = Controller(config(tmp_path), FakeGitHub(), Store(str(tmp_path / "db")), SpendingObserver(refuse="The observer may not read the billing export yet"))
    position = refused.cost_position()
    assert chat.cost_position_line(position) == "Spend this month: not read (The observer may not read the billing export yet)"
    assert position["projects"][0]["budget_usd"] is None


# ---- 24.3 and 24.4: anomaly detection, alert slots and idle services on Mondays
def test_mondays_count_alert_slots_and_name_idle_services(tmp_path):
    cfg = config(tmp_path)
    store_with(tmp_path, release("live", NOW - 40 * DAY))
    c = Controller(cfg, FakeGitHub(), Store(cfg.database), SpendingObserver())
    c.watchdog_problems = lambda now=None: []
    record = c.build_digest(NOW)  # a Monday
    assert record["idle"] == ["demo"] and record["alert_slots"][0]["alert_policies"] == 80
    text = chat.digest(record)
    assert ("Alert slots: `test-project` 80 alert policies, 13 uptime checks (about 673,920 of the free 1,000,000 executions a month). "
            "Google charges for alert policies from 1 Sep 2027 at the earliest") in text
    assert "No user request in 30 days: `demo`. Each keeps five alert policies and an uptime check; retire one with scripts/decommission.sh" in text
    assert "Google's free Cost Anomaly Detection is not recorded as on (docs/SETUP.md 7q)" in text
    recorded = Controller(config(tmp_path, cost_anomaly_detection=True), FakeGitHub(), Store(cfg.database), SpendingObserver())
    recorded.watchdog_problems = lambda now=None: []
    assert "Cost Anomaly Detection" not in chat.digest(recorded.build_digest(NOW))
    assert "Alert slots" not in chat.digest(c.build_digest(NOW + DAY))  # only on Mondays


# ---- the foundation and the settings page
def test_the_foundation_lets_the_observer_and_the_machine_read_the_billing_export():
    main = Path("infra/gcp/modules/project/main.tf").read_text()
    block = main[main.index("locals {\n  billing_readers"):main.index("# The cap acts through this role")]
    assert '{ observer = "serviceAccount:${google_service_account.observer.email}" }' in block
    assert 'local.controller_enabled ? { machine = "serviceAccount:${google_service_account.controller[0].email}" } : {}' in block
    assert 'role     = "roles/bigquery.jobUser"' in block and 'role       = "roles/bigquery.dataViewer"' in block
    assert "dataset_id = google_bigquery_dataset.billing[0].dataset_id" in block  # the dataset only, not every table of the project


def test_the_settings_page_records_budgets_the_digest_hour_and_anomaly_detection(tmp_path):
    cfg = config(tmp_path)
    candidate, found = cfg.model_dump(mode="json", exclude_defaults=True), {}
    form = {"auto_deploy_interval_seconds": "300", "backup_keep_days": "14", "cloud_run_spend_caps": "", "digest_hour_utc": "14",
            "monthly_budgets_usd": "test-project: 20\nfast-deploy-prod-c602 = 25", "cost_anomaly_detection": "on"}
    change_platform(candidate, cfg, {}, form, "main", found)
    assert found == {} and candidate["digest_hour_utc"] == 14 and candidate["cost_anomaly_detection"] is True
    assert candidate["monthly_budgets_usd"] == {"test-project": 20, "fast-deploy-prod-c602": 25}
    off, found = cfg.model_dump(mode="json", exclude_defaults=True), {}
    change_platform(off, cfg, {}, {**form, "digest_hour_utc": "", "monthly_budgets_usd": "nonsense"}, "main", found)
    assert off["digest_hour_utc"] is None and found["monthly_budgets_usd"] == "'nonsense' is not project: amount"
