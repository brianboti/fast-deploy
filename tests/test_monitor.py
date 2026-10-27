"""The settings page's Monitor section (labdeploy/monitor.py, monitor_page.py and charts.py): reports, live status, usage and
cost, drawn as server-rendered SVG. No test touches GitHub or a cloud; Google's APIs answer from a mock transport that counts
every call, so the read budget is a number a test holds."""
import contextlib
import datetime
import hashlib
import html
import json
import re
import time
import types

import httpx
import pytest
import yaml
from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient

from labdeploy import aftercare, charts, chat, monitor, server, settings, telemetry
from labdeploy.cloud import Observer
from labdeploy.config import load_config
from labdeploy.controller import HIDDEN_COSTS, HIDDEN_STATUS, Controller, Store
from labdeploy.oauth import PAGE_HEADERS
from labdeploy.server import build_app

DAY = 86_400
PRODUCTION = {**TARGET, "project_id": "prod-project", "deploy_service_account": "demo-deploy@prod-project.iam.gserviceaccount.com",
              "probe_service_account": "demo-probe@prod-project.iam.gserviceaccount.com", "recovery_bucket": "prod-project-demo-recovery"}
OTHER = {**TARGET, "service": "other", "registry": "other", "deploy_service_account": "other-deploy@test-project.iam.gserviceaccount.com",
         "probe_service_account": "other-probe@test-project.iam.gserviceaccount.com",
         "wif_provider": "projects/123456789/locations/global/workloadIdentityPools/other-gh/providers/github", "recovery_bucket": "test-project-other-recovery"}
OBSERVER = {"service_account": "lab-observer@test-project.iam.gserviceaccount.com"}
# The live reads one view may make when nothing is cached (labdeploy/monitor.py): the overview one Monitoring call per
# service; an application's health four (traffic with latency, instances, memory, the 30-day uptime) and five more for
# production's error budget; its error lines one Logging call, and only when asked. Nothing else reads Google at all.
OVERVIEW_PER_SERVICE, HEALTH, ERROR_BUDGET, LOGS = 1, 4, 5, 1


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def words_of(page):
    """The page as a person reads it: the text without its style sheet and tags, one space between words."""
    text = re.sub(r"<style>.*?</style>", " ", page.text, flags=re.S)
    text = re.sub(r"</?(code|strong|small|em|a)\b[^>]*>", "", text)  # inline markup sits inside a sentence
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text)))


def configuration(tmp_path, observer=True, **extra):
    return {"database": str(tmp_path / "db"),
            "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET, "cost_ceiling_usd": 40,
                                      "production": {"target": PRODUCTION, "environment": "production-demo"}},
                             "other": {"repository": "lab/other", "service": "other", "target": OTHER}},
            "principals": [{"name": "operator", "token_sha256": sha("operator-token"), "applications": ["*"], "can_deploy": True},
                           {"name": "viewer", "token_sha256": sha("viewer-token"), "applications": ["demo"], "can_deploy": True},
                           {"name": "watcher", "token_sha256": sha("watcher-token"), "applications": ["*"]},
                           {"name": "nobody", "token_sha256": sha("nobody-token"), "applications": []}],
            "platform": {"repository": "lab/fast-deploy"}, **({"observer": OBSERVER} if observer else {}), **extra}


class Steady:
    """An observer for the charts: steady canned numbers, every call kept."""

    def __init__(self):
        self.calls = []

    def traffic(self, target, start, end, step):
        self.calls.append(("traffic", target.project_id, step))
        count = int((end - start).total_seconds() // step)
        return {"steps": [{"at": iso(start.timestamp() + (index + 1) * step), "p95_ms": 120.0,
                           "classes": {"2xx": 10, "4xx": 1, **({"5xx": 2} if index == count - 1 else {})}} for index in range(count)], "p95_ms": 125.0}

    def saturation(self, target, start, end, step):
        self.calls.append(("saturation", target.project_id, step))
        moments = [iso(start.timestamp() + (index + 1) * step) for index in range(int((end - start).total_seconds() // step))]
        return {"instances": [(at, 1) for at in moments], "memory": [(at, 0.4) for at in moments]}

    def uptime_days(self, target, days=30, now=None):
        self.calls.append(("uptime", target.project_id, days))
        first = datetime.date.today() - datetime.timedelta(days=days - 1)
        return [{"day": (first + datetime.timedelta(days=offset)).isoformat(), "passed": 0.98 if offset == 3 else 1.0} for offset in range(days)]

    def error_budget(self, target, service_id):
        self.calls.append(("budget", target.project_id, service_id))
        return {"goal": 0.995, "days": 30, "compliance": 0.997, "budget_remaining": 0.6, "requests_counted": 4000, "errors_counted": 8}

    def logs(self, target, minutes=30, severity="ERROR", limit=50):
        self.calls.append(("logs", target.project_id, minutes))
        return [{"at": iso(time.time() - 600), "severity": "ERROR", "message": "boom <script>alert(1)</script>"}]


class Reports(FakeGitHub):
    """GitHub with a weekly cost report per application, the newest run's id, a clean drift report and an App credential."""
    run = 9001

    def __init__(self):
        self.downloads, self.checks = [], 0

    def cost_report(self, app, workflow="rightsize-gcp.yml", name="cost-report", limit=1_048_576):
        self.downloads.append(app.service)
        # The whole report as the workflow writes it: besides the application's own measurements, the project's bill (which
        # holds the other application's line), the month so far, Google's recommender for every service of the project and a
        # repository's Actions minutes.
        report = {"generated_at": "2026-10-19T06:31:02+00:00", "service": app.service, "project_id": "test-project", "window_days": 14,
                  "estimate": {"compute_ceiling_usd_month": 12.5, "idle_floor_usd_month": 0.0},
                  "rightsizing": {"observed": {"usd_month_at_list_price": 0.5, "requests": 40},
                                  "recommendations": [{"setting": "resources.memory_mib", "current": 1024, "recommended": 512,
                                                       "reason": "p95 memory 31% of the limit", "estimated_monthly_saving_usd": 1.42}]},
                  "cloud_run_cost_recommender": {"status": "ok", "count": 1, "recommendations": [{"description": "Switch other to instance-based billing"}]},
                  "release_tolls": {"month": "2026-10", "fresh_images": 2, "image_scans_usd": 0.52, "actions": {"billable": True, "minutes": 91}},
                  "month": {"month": "2026-10", "days_elapsed": 20.5, "days_in_month": 31, "projects": [{"project": "test-project", "net_cost_usd": 8.4,
                                                                                                         "forecast_usd": 12.7}]},
                  "actual_spend": {"window_days": 30, "data_through": "2026-10-18T23:00", "total_net_cost_usd": 9.5, "table": "billing-proj.billing.export",
                                   "by_service": [{"service": "Cloud Run", "net_cost_usd": 6.0}, {"service": "Artifact Registry", "net_cost_usd": 3.5}],
                                   "by_app": [{"app": "demo", "net_cost_usd": 7.0}, {"app": "other", "net_cost_usd": 2.5}],
                                   "by_project": [{"project": "test-project", "environment": "staging", "net_cost_usd": 9.5}],
                                   "previous": {"window_days": 30, "total_net_cost_usd": 8.0, "by_service": [{"service": "Cloud Run", "net_cost_usd": 4.5},
                                                                                                           {"service": "Cloud Build", "net_cost_usd": 1.25}],
                                                "by_app": [{"app": "demo", "net_cost_usd": 5.0}]},
                                   "movers": {"by_service": [{"service": "Cloud Build", "net_cost_usd": 0.0, "before_usd": 1.25, "change_usd": -1.25, "change_share": -1.0}],
                                              "by_app": [{"app": "other", "net_cost_usd": 2.5, "before_usd": 0.0, "change_usd": 2.5, "change_share": None}]},
                                   "app": {"name": app.service, "net_cost_usd": {"demo": 7.0, "other": 2.5}.get(app.service, 0.0)}}}
        return {"workflow_run_id": self.run, "run_url": "https://github.com/lab/demo/actions/runs/9001", "report": report}

    def newest_run_id(self, app, workflow, status="success"):
        self.checks += 1
        return self.run

    def drift_report(self, repository, workflow="infra-drift.yml", name="drift-report"):
        return {"run_url": "https://github.com/lab/fast-deploy/actions/runs/7", "report": {"checked_at": iso(time.time() - DAY), "drifted": False,
                                                                                          "applications": [], "serious": [], "failed": []}}

    def credential_status(self, now=None):
        return {"kind": "github_app", "app_id": "1", "expires_at": None, "days_left": None, "warning": None}


def release(app, at, verdict=None, kind=None, state="succeeded", owner="operator", **extra):
    """A recorded release as the controller keeps it: switched at `at`, its run five minutes long."""
    target = PRODUCTION if kind == "promotion" else TARGET if app == "demo" else OTHER
    plan = {"id": sha(f"{app}{at}{kind}")[:32], "application": app, "kind": kind, "sha": sha(f"commit{app}{at}")[:40],
            "commit": {"message": f"Change of {app}", "date": iso(at - 3600)}, "expires_at": at - 300 + 900, "target": target,
            "spec": {"health_path": "/health", "scaling": {"minimum": 0, "maximum": 3}},
            "cost": {"idle_floor_usd_month": 0.0, "needs_floor_usd_month": 0.0, "compute_ceiling_usd_month": 12.5, "ceiling_policy": {"limit_usd_month": 40}},
            "receipt": {"revision": f"{target['service']}-r{int(at)}", "state": "succeeded" if state == "succeeded" else "failed",
                        "bake": {"since": iso(at), "result": "passed"}},
            "workflow": {"html_url": f"https://github.com/lab/{app}/actions/runs/{int(at)}", "created_at": iso(at - 300), "updated_at": iso(at)},
            "outcome": {"state": state, "completed_at": iso(at), "recorded_at": at, "run_url": f"https://github.com/lab/{app}/actions/runs/{int(at)}"}}
    if verdict:
        plan["release_report"] = {"generated_at": iso(min(at + DAY, time.time() - 60)), "verdict": verdict}
    plan.update(extra)
    return owner, plan, state


def seed(store, now=None):
    now = now or time.time()
    for owner, plan, state in [release("demo", now - 9 * DAY, "same"), release("demo", now - 2 * DAY, "worse"),
                               release("demo", now - 3 * DAY, "better", kind="promotion"), release("other", now - 5 * DAY, "first"),
                               release("other", now - DAY / 2, state="failed")]:
        store.add(owner, plan, state)
    day = datetime.datetime.fromtimestamp(now, datetime.UTC).date().isoformat()
    store.save_digest(day, {"day": day, "generated_at": iso(now - 3600), "services": [
        {"application": "other", "environment": "staging", "server_errors": 0, "uptime": 0.95, "error_lines": 3}],
        "problems": ["the monthly proof that alerts reach you is due: scripts/alert-drill.sh", "other: the newest release attempt is 'failed'"],
        "cost": {"month": day[:7], "days_elapsed": 20.5, "days_in_month": 31, "projects": [
            {"project": "test-project", "environment": "staging", "net_cost_usd": 8.4, "forecast_usd": 12.7, "budget_usd": 20, "native_cap_usd": 20,
             "over_budget": False},
            {"project": "prod-project", "environment": "production", "net_cost_usd": 14.6, "forecast_usd": 22.1, "budget_usd": 20, "native_cap_usd": None,
             "over_budget": True}]}})
    with store.connect() as db:
        db.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (now - 3 * DAY, "operator", "127.0.0.1", "tool", "deploy_latest", "demo", "ok", 900, None),
            (now - 2 * DAY, "operator", "127.0.0.1", "tool", "get_service_health", "other", "ok", 300, None),
            (now - DAY, "operator", "127.0.0.1", "configuration", "platform", None, "ok", None, "digest_hour_utc"),
            (now - 3600, "watcher", "127.0.0.1", "tool", "list_applications", None, "ok", 40, None),
            (now - 1800, "viewer", "127.0.0.1", "tool", "get_digest", None, "ok", 40, None)])


@pytest.fixture
def site(tmp_path):
    """make(observer, github, **configuration) -> store; then client(token) signs a new browser in."""
    stack = contextlib.ExitStack()
    made = {}

    def make(observer=None, github=None, seeded=True, factory=None, background=False, **extra):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(configuration(tmp_path, observer is not None or factory is not None, **extra), sort_keys=False))
        store = Store(str(tmp_path / "db"))
        if seeded:
            seed(store)
        made["github"] = github or Reports()
        made["app"] = build_app(load_config(str(path)), made["github"], config_path=str(path), observer_factory=factory or (lambda config: observer),
                                background=background)
        return store

    def client(token="operator-token"):
        browser = TestClient(made["app"], base_url="https://testserver")
        if not made.get("started"):
            made["started"] = True
            browser = stack.enter_context(browser)
        if token:
            assert browser.post("/settings/sign-in", data={"token": token}, follow_redirects=False).status_code == 303
        return browser

    make.client = client
    with stack:
        yield make


def view(browser, tab, status=200, **params):
    page = browser.get("/settings", params={"tab": tab, **params})
    assert page.status_code == status, page.text[:400]
    return page


def tool(browser, token, name, **arguments):
    """One tool call as Claude makes it, with the identity's own token: the structured answer, summary first."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    return browser.post("/mcp", headers=headers, json=body).json()["result"]["structuredContent"]


# ---- the charts: pure functions from numbers to SVG

def accessible(svg):
    """role="img", named by its title and described by its description, both inside it, and nothing loaded."""
    ident = re.search(r'aria-labelledby="([^" ]+)-t \1-d"', svg).group(1)
    assert svg.startswith("<svg") and 'role="img"' in svg and f'<title id="{ident}-t">' in svg and f'<desc id="{ident}-d">' in svg
    assert "<script" not in svg and "url(" not in svg and "<image" not in svg and "viewBox" not in svg  # sized by the page: text never scales
    return svg


def test_every_chart_is_an_image_with_a_title_a_description_and_a_hover_value_per_point():
    tips = [f"step {index}" for index in range(4)]
    for svg in (charts.sparkline("s", "Requests", "Four hours.", [3, None, 5, 2], tips),
                charts.bars("b", "Errors", "Four steps.", [0, 2, 0, 7], "errors", tips=tips),
                charts.minibars("m", "Releases", "Four days.", [1, 0, 2, 1], tips),
                charts.stacked_bars("k", "Requests by answer", "Four steps.", [{"a": 5, "b": 1}, {"a": 4}, {}, {"a": 1, "b": 0, "c": 1}],
                                    [("a", "Answered", charts.QUIET), ("b", "Refused", charts.WARN), ("c", "Errors", charts.BAD)], "requests", tips),
                charts.line("l", "Latency", "Four steps.", [100, 120, None, 90], "ms", tips=tips, thresholds=[(150, "alert at 150 ms")])):
        accessible(svg)
        for tip in tips:
            assert f"<title>{tip}</title>" in svg  # the hover value of each step, on a column as tall as the plot
    assert charts.sparkline("s", "t", "d", [1, 2], ["a", "b"]).count('class="hit"') == 2


def test_bars_round_their_tops_keep_a_gap_and_never_hide_a_single_error():
    svg = charts.stacked_bars("k", "t", "d", [{"a": 1000, "b": 1}, {"a": 500, "b": 500}], [("a", "A", "#111111"), ("b", "B", "#222222")], tips=["x", "y"])
    marks = [(float(y), float(height), colour) for y, height, colour in re.findall(r'<rect x="[^"]+" width="[^"]+" y="([0-9.]+)" height="([0-9.]+)"(?: rx="2")? '
                                                                                   r'fill="#(\w+)"/>', svg)]
    (lower_y, _, _), (upper_y, upper_height, upper) = marks[0], marks[1]
    assert upper == "222222" and upper_height >= 2  # one error among a thousand requests still shows
    assert upper_y + upper_height == pytest.approx(lower_y - 2)  # 2 pixels of surface between the segments
    assert svg.count('rx="2" fill="#222222"') == 1 and 'rx="2" fill="#111111"' not in svg  # only a topmost segment is rounded, its base square
    assert charts.bars("b", "t", "d", [0, 0], tips=["a", "b"]).count("<rect") == 2  # two hover columns, no bar for nothing


def test_a_threshold_far_above_the_data_is_named_in_words_instead_of_drawn():
    top, shown, hidden = charts.scale([100, 120, 90], [(2000, "alert at 2,000 ms")])
    assert hidden == [(2000, "alert at 2,000 ms")] and not shown and top == 150
    svg = charts.line("l", "t", "d", [100, 120, 90], "ms", thresholds=[(2000, "alert at 2,000 ms")])
    assert "stroke-dasharray" not in svg and "alert at" not in svg
    near = charts.line("l", "t", "d", [100, 120, 90], "ms", thresholds=[(150, "alert at 150 ms")])
    assert 'stroke-dasharray="4 4"' in near and "alert at 150 ms" in near and ">150 ms<" not in near.split("alert")[0][-40:]


def test_release_markers_carry_their_words():
    svg = charts.line("l", "t", "d", [1, 2, 3], markers=[{"at": 0.5, "tip": "Release abc1234 at 21 Oct 10:00 UTC: reached users"}])
    assert 'x1="50%"' in svg and "<title>Release abc1234 at 21 Oct 10:00 UTC: reached users</title>" in svg and 'class="flag"' in svg


def test_the_uptime_strip_reads_without_colour():
    days = [{"day": "2026-10-0" + str(index), "passed": passed, "tip": f"day {index}"} for index, passed in enumerate([1.0, 0.995, 0.97, None], 1)]
    svg = accessible(charts.uptime_strip("u", "Uptime", "Four days.", days, "4 days ago", "today"))
    assert svg.count('class="cell"') == 4 and svg.count("M0 0l4 5h-8z") == 2  # a caret under each day that was not green
    assert [charts.uptime_level(value) for value in (1.0, 0.999, 0.995, 0.97, None)] == ["good", "good", "warning", "bad", None]
    for colour in (charts.GOOD, charts.WARN, charts.BAD, charts.EMPTY):
        assert f'fill="{colour}"' in svg


def test_the_bullet_graph_and_the_running_total():
    svg = accessible(charts.bullet("b", "Spend", "d", 8.4, 12.7, 20, 20))
    assert "budget $20.00 · cap $20.00" in svg  # a budget and a cap at the same place share one label
    apart = charts.bullet("b", "Spend", "d", 8.4, 12.7, 10, 40)
    assert "budget $10.00" in apart and "cap $40.00" in apart and 'stroke-dasharray="3 2"' in apart
    month = accessible(charts.progress("p", "Month", "d", [(0.3, 5.0, "a"), (0.6, 9.0, "b")], (1.0, 15.0, "on course for $15"), [(20, "budget $20")]))
    assert 'stroke-dasharray="5 4"' in month and "budget $20" in month and "<title>on course for $15</title>" in month
    assert ">20 USD<" not in month  # the budget at the top says the top's value


def test_timeline_shapes_hbars_ranges_meter_legend_and_numbers():
    svg = accessible(charts.timeline("t", "Releases", "d", ["demo", "other"], [
        {"row": 0, "at": 0.2, "shape": "circle", "color": charts.GOOD, "tip": "better"},
        {"row": 1, "at": 0.9, "shape": "triangle", "color": charts.BAD, "tip": "worse"},
        {"row": 1, "at": 0.5, "shape": "cross", "color": charts.BAD, "tip": "rolled back"}], [(0, "30 days ago"), (1, "now")]))
    assert svg.count('class="mk"') == 3 and 'r="12" fill="transparent"' in svg  # a hover area of 24 pixels around each shape
    accessible(charts.hbars("h", "By service", "d", [("Cloud Run", 6.0, "$6.00", "Cloud Run $6"), ("Credit", -1.0, "-$1.00", "a credit")]))
    accessible(charts.ranges("r", "Bounds", "d", [("demo", 0.5, 12.0, 40, "$0.50 to $12", "demo")]))
    accessible(charts.meter("e", "60% left", "d", 0.6, charts.GOOD))
    legend = charts.legend([("Answered", charts.QUIET, "box"), ("Alert", charts.INK, "dash"), ("Worse", charts.BAD, "triangle")])
    assert legend.count("<li>") == 3 and 'aria-hidden="true"' in legend
    table = charts.numbers(["When", "Requests"], [("10:00", "1,204")], numeric={1}, caption="Requests")
    assert "<summary>Show the numbers</summary>" in table and '<td class="r num">1,204</td>' in table and '<caption class="sr">Requests</caption>' in table
    assert "&lt;b&gt;" in charts.sparkline("x", "<b>", "d", [1]) and "<b>" not in charts.sparkline("x", "<b>", "d", [1])


def test_axis_tops_and_numbers_read_round():
    assert [charts.nice(value) for value in (0, 0.7, 5.4, 9, 13, 2190, 3000)] == [1, 0.8, 6, 10, 15, 2500, 3000]
    assert [charts.compact(value) for value in (950, 1204, 25_000, 3_400_000, 0.25, None)] == ["950", "1.2K", "25K", "3.4M", "0.25", "–"]
    assert charts.labelled(100, "%") == "100%" and charts.labelled(1200, "requests") == "1.2K requests" and charts.pct(1.4) == "100%"
    assert charts.labelled(1, "errors") == "1 error" and charts.labelled(1, "ms") == "1 ms"


# ---- reading Google: one query per chart line

def test_latency_histograms_and_their_percentile():
    bounds = telemetry.bucket_bounds({"exponentialBuckets": {"numFiniteBuckets": 3, "growthFactor": 2, "scale": 10}})
    assert bounds[0][1] == 10 and bounds[1] == (10, 20) and bounds[-1][0] == 80 and len(bounds) == 5
    counted = telemetry.histogram({"distributionValue": {"count": "20", "bucketOptions": {"exponentialBuckets": {"numFiniteBuckets": 3, "growthFactor": 2, "scale": 10}},
                                                         "bucketCounts": ["0", "10", "10"]}})
    assert counted == {(10.0, 20.0): 10, (20.0, 40.0): 10}
    assert telemetry.percentile(counted, 0.5) == 20 and telemetry.percentile(counted, 0.95) == pytest.approx(38.0)
    assert telemetry.percentile({}, 0.95) is None and telemetry.percentile({(80.0, float("inf")): 3}, 0.95) == 80
    assert telemetry.percentile(telemetry.merge(counted, {(10.0, 20.0): 80}), 0.5) == pytest.approx(10 + 10 * 50 / 90)


class Google:
    """Google's APIs as a mock transport: the metadata server's token, canned Monitoring series, Logging's entries; every call
    counted by API, and BigQuery refused, since no view may query it."""

    def __init__(self):
        self.calls = []

    def __call__(self, request):
        host = request.url.host
        if host == "metadata.google.internal":
            return httpx.Response(200, json={"access_token": "observer-token", "expires_in": 3600})
        self.calls.append((host, request.url.path))
        if host == "monitoring.googleapis.com":
            if "serviceLevelObjectives" in request.url.path:
                return httpx.Response(200, json={"goal": 0.995, "rollingPeriod": "2592000s"})
            params = request.url.params
            end = params["interval.endTime"]
            if "select_slo" in params["filter"]:
                return httpx.Response(200, json={"timeSeries": [{"points": [{"interval": {"endTime": end}, "value": {"doubleValue": 0.6}}]}]})
            if "request_latencies" in params["filter"]:
                assert params["aggregation.groupByFields"] == "metric.label.response_code_class" and params["aggregation.crossSeriesReducer"] == "REDUCE_SUM"
                value = {"distributionValue": {"count": "10", "bucketOptions": {"exponentialBuckets": {"numFiniteBuckets": 10, "growthFactor": 2, "scale": 1}},
                                               "bucketCounts": ["0", "0", "0", "0", "0", "0", "0", "10"]}}
                return httpx.Response(200, json={"timeSeries": [{"metric": {"labels": {"response_code_class": name}},
                                                                 "points": [{"interval": {"endTime": end}, "value": value}]} for name in ("2xx", "5xx")]})
            return httpx.Response(200, json={"timeSeries": [{"points": [{"interval": {"endTime": end}, "value": {"doubleValue": 0.5}}]}]})
        if host == "logging.googleapis.com":
            return httpx.Response(200, json={"entries": [{"timestamp": iso(time.time()), "severity": "ERROR", "textPayload": "boom"}]})
        return httpx.Response(500, json={"error": f"no view may call {host}"})

    def count(self, host):
        return sum(1 for called, _ in self.calls if called == host)


def test_one_query_reads_traffic_by_answer_and_its_latency():
    google = Google()
    observer = Observer(type("C", (), {"observer": type("O", (), {"service_account": OBSERVER["service_account"], "cache_seconds": 300})})(),
                        client=httpx.Client(transport=httpx.MockTransport(google)))
    end = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    found = observer.traffic(type("T", (), TARGET)(), end - datetime.timedelta(hours=1), end, 3600)
    assert google.count("monitoring.googleapis.com") == 1
    assert found["steps"][0]["classes"] == {"2xx": 10, "5xx": 10} and found["steps"][0]["p95_ms"] == pytest.approx(124.8)


def test_the_read_budget_holds_per_view_and_a_reading_is_shared_for_five_minutes(site):
    """Monitoring and Logging calls per view, fixed: the overview one per service, an application's health four (nine in
    production), its error lines one and only when asked; everything else none, and BigQuery never."""
    google = Google()
    site(factory=lambda config: Observer(config, client=httpx.Client(transport=httpx.MockTransport(google))))
    services = 3  # demo in staging and in production, other in staging
    browser = site.client()

    def spent(before):
        return google.count("monitoring.googleapis.com") - before[0], google.count("logging.googleapis.com") - before[1]

    def mark():
        return google.count("monitoring.googleapis.com"), google.count("logging.googleapis.com")

    start = mark()
    page = view(browser, "overview")
    assert spent(start) == (services * OVERVIEW_PER_SERVICE, 0) and "Live, a few minutes behind" in page.text
    start = mark()
    view(browser, "overview")
    view(site.client("viewer-token"), "overview")  # another person, the same five minutes: no new read
    assert spent(start) == (0, 0)
    start = mark()
    view(browser, "health", app="demo")  # 24 hours: its traffic was read for the overview already
    monitoring, logging = spent(start)
    assert monitoring <= HEALTH and logging == 0
    start = mark()
    view(browser, "health", app="demo", range="7d")
    assert spent(start)[0] <= HEALTH and spent(start)[1] == 0
    start = mark()
    view(browser, "health", app="demo", env="production")
    assert spent(start)[0] <= HEALTH + ERROR_BUDGET and spent(start)[1] == 0
    start = mark()
    for _ in range(2):  # error lines: one read when asked, kept a minute
        page = view(browser, "health", app="demo", logs="1")
    assert spent(start) == (0, LOGS) and "boom" in page.text
    start = mark()
    for tab in ("releases", "cost", "activity", "health"):
        view(browser, tab)
    assert spent(start) == (0, 0)
    assert google.count("bigquery.googleapis.com") == 0 and not [host for host, _ in google.calls if host not in ("monitoring.googleapis.com", "logging.googleapis.com")]


def test_the_cache_reads_once_remembers_a_failure_for_a_minute_and_forgets_after_five():
    clock = [1000.0]
    cache = monitor.LiveCache(clock=lambda: clock[0])
    reads = []
    assert cache.get("k", lambda: reads.append(1) or "value") == ("value", 1000.0)
    clock[0] += 299
    assert cache.get("k", lambda: reads.append(1) or "new") == ("value", 1000.0) and len(reads) == 1
    clock[0] += 2
    assert cache.get("k", lambda: reads.append(1) or "new")[0] == "new" and len(reads) == 2

    def broken():
        reads.append("x")
        raise ValueError("The observer identity is not allowed to read this")
    for _ in range(3):
        with pytest.raises(monitor.ReadFailed, match="not allowed"):
            cache.get("bad", broken)
    assert reads.count("x") == 1
    clock[0] += 61
    with pytest.raises(monitor.ReadFailed):
        cache.get("bad", broken)
    assert reads.count("x") == 2


def test_the_weekly_report_is_kept_until_github_has_a_newer_run(tmp_path):
    github = Reports()
    config = load_config_text(tmp_path)
    clock = [time.time()]
    watcher = monitor.Monitor(Controller(config, github, Store(config.database)), clock=lambda: clock[0])
    principal = next(p for p in config.principals if p.name == "operator")
    for _ in range(2):
        found = watcher.weekly_report(principal, "demo")
    assert github.downloads == ["demo"] and found["result"]["report"]["service"] == "demo"
    clock[0] += monitor.REPORT_CHECK_SECONDS + 1
    watcher.weekly_report(principal, "demo")
    assert github.downloads == ["demo"]  # GitHub was asked, and the run had not changed
    github.run = 9002
    clock[0] += monitor.REPORT_CHECK_SECONDS + 1
    watcher.weekly_report(principal, "demo")
    assert github.downloads == ["demo", "demo"]

    def unreachable(app, workflow="rightsize-gcp.yml", name="cost-report", limit=1_048_576):
        github.downloads.append("failed")
        raise httpx.ConnectError("GitHub is down")
    github.cost_report = unreachable
    for _ in range(3):  # a download that failed is said, and tried again five minutes later, not on every view
        assert watcher.weekly_report(principal, "other") == {"error": "GitHub is down"}
    assert github.downloads.count("failed") == 1
    clock[0] += monitor.LIVE_SECONDS + 1
    watcher.weekly_report(principal, "other")
    assert github.downloads.count("failed") == 2


def load_config_text(tmp_path, **extra):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(configuration(tmp_path, **extra)))
    return load_config(str(path))


# ---- the views, scoped as Claude's tools are

def test_every_view_draws_with_the_strict_headers_and_loads_nothing(site):
    site(Steady())
    browser = site.client()
    pages = [view(browser, tab) for tab in ("overview", "health", "releases", "cost", "activity")]
    pages += [view(browser, "health", app="demo", range=name) for name in monitor.RANGES]
    pages += [view(browser, "health", app="demo", env="production", logs="1"), view(browser, "activity", kind="tool", more="1")]
    for page in pages:
        for header, value in PAGE_HEADERS.items():
            assert page.headers[header] == value
        assert "<script" not in page.text.replace("&lt;script&gt;", "") and "url(" not in page.text and "<img" not in page.text
        assert 'role="img"' in page.text or "No " in page.text
        for ident in re.findall(r'aria-labelledby="([^" ]+)-t ', page.text):
            assert page.text.count(f'id="{ident}-t"') == 1  # every chart's name and description belong to it alone
    assert "boom &lt;script&gt;" in pages[-2].text  # a log line is text, never markup
    said = words_of(pages[0])
    assert "Needs attention" in said and "Services healthy" in said and "Releases this week" in said and "Spend this month $23" in said
    assert "demo: its live release" in said and "did worse than the version before" in said  # the worse release, once
    assert "prod-project is on course for $22.10 this month, above its $20.00 budget" in said
    assert "the monthly proof that alerts reach you is due" in said
    assert "the newest release attempt ended failed" in said and "the newest release attempt is 'failed'" not in said  # said once, by its service
    health = words_of(view(browser, "health", app="demo"))
    assert "Requests by answer" in health and "Server errors (5xx)" in health and "95th-percentile latency" in health and "Memory, share of the limit" in health
    assert "Uptime check, by day" in health and "Below 99.9% (marked with a caret)" in health and "Read the newest error lines" in health
    assert "console.cloud.google.com/logs/query" in view(browser, "health", app="demo").text and "Show the numbers" in health


def test_monitor_is_open_to_every_principal_for_its_own_applications(site):
    site(Steady())
    viewer = site.client("viewer-token")  # may deploy demo only: Monitor, scoped; never Configure
    overview = view(viewer, "overview")
    said = words_of(overview)
    assert "demo" in said and "other" not in said and 'id="nav-configure"' not in overview.text and "Spend this month –" in said
    assert "There is no application named other among those your token may see" in words_of(view(viewer, "health", 404, app="other"))
    assert "other" not in words_of(view(viewer, "releases")) and chat.WHOLE_BILL + "." in words_of(view(viewer, "cost"))
    refused = view(viewer, "platform", 403)
    assert "viewer may not change the controller's configuration" in words_of(refused) and "set-cookie" not in refused.headers
    assert 'id="nav-configure"' not in refused.text and 'href="/settings?tab=overview"' in refused.text  # the sidebar shows Monitor only
    activity = words_of(view(viewer, "activity"))
    assert "get_digest" in activity and "deploy_latest" in activity and "get_service_health" not in activity and "list_applications" not in activity
    watcher = site.client("watcher-token")  # every application, read-only: its own events only, as list_events decides
    assert "other" in words_of(view(watcher, "overview")) and "can_deploy" in view(watcher, "people", 403).text
    seen = words_of(view(watcher, "activity"))
    assert "list_applications" in seen and "deploy_latest" not in seen and "Your own events" in seen
    form = view(watcher, "activity").text
    assert 'method="get"' in form and 'name="csrf"' not in form.split('class="filters"')[1].split("</form>")[0]  # a filter changes nothing
    assert "This token sees no application" in words_of(view(site.client("nobody-token"), "overview"))
    operator = site.client()
    assert 'id="nav-configure"' in view(operator, "overview").text and "deploy_latest" in words_of(view(operator, "activity", kind="tool"))


ELSEWHERE = {**OTHER, "project_id": "lab-other", "deploy_service_account": "other-deploy@lab-other.iam.gserviceaccount.com",
             "probe_service_account": "other-probe@lab-other.iam.gserviceaccount.com", "recovery_bucket": "lab-other-recovery"}


class Jobs:
    """The background jobs as a running controller reports them (Scheduler.snapshot): results and an error that name both
    applications. Nothing runs."""

    def __init__(self, controller=None, config=None):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def snapshot(self):
        return {"auto_deploy": {"next_run": "2026-10-21T10:01:00+00:00", "last_run": "2026-10-21T10:00:00+00:00",
                                "last_result": {"demo": "up to date", "other": "released 1234567"}, "last_error": None},
                "watchdog": {"next_run": "2026-10-22T09:00:00+00:00", "last_run": "2026-10-21T09:00:00+00:00",
                             "last_result": {"problems": ["other: no scheduled run recorded yet"]}, "last_error": None},
                "backup": {"next_run": "2026-10-22T03:00:00+00:00", "last_run": "2026-10-21T03:00:00+00:00", "last_result": None,
                           "last_error": "RuntimeError: other's disk is full"}}


class Drifted(Reports):
    """The weekly drift check found the other application's cloud setup changed by hand."""

    def drift_report(self, repository, workflow="infra-drift.yml", name="drift-report"):
        return {"run_url": "https://github.com/lab/fast-deploy/actions/runs/7", "report": {
            "checked_at": iso(time.time() - DAY), "drifted": True, "applications": [{"app": "other", "drifted": True, "categories": ["iam"]}],
            "serious": ["other"], "failed": []}}


def test_cost_for_a_limited_identity_is_its_own_lines_of_the_bill_on_the_page_and_in_chat(site):
    """An identity that does not see every application gets, on the Cost view and from get_cost_report alike, the bill's lines
    of its own applications beside their window before, its proposals, floor and ceiling; never the bill by Google service,
    the projects' totals or names, the month so far, or another application's line. A `*` identity gets all of it."""
    site(Steady())
    viewer, operator = site.client("viewer-token"), site.client()
    said = words_of(view(viewer, "cost"))
    assert said.count(chat.WHOLE_BILL) == 1  # one plain line where the projects' bill was
    assert "demo $7.00 · ▲ $2.00" in said and "Application Last 30 days The 30 before demo $7.00 $5.00" in said
    assert "resources.memory_mib 1024 512 p95 memory 31% of the limit $1.42" in said and "demo · production $0 to $12.50 · ceiling $40.00" in said
    for hidden in ("This month", "By Google service", "Artifact Registry", "Gone since", "Cloud Build", "$9.50", "$8.00", "$6.00", "test-project",
                   "prod-project", "other", "$2.50"):
        assert hidden not in said, hidden
    whole = words_of(view(operator, "cost"))
    assert chat.WHOLE_BILL not in whole
    for shown in ("This month", "test-project · staging", "By Google service", "Cloud Run $6.00", "Gone since the window before: Cloud Build ($1.25).",
                  "$9.50 in the last 30 days against $8.00 in the 30 before, net of credits, in test-project.", "other $2.50 · new"):
        assert shown in whole, shown
    answer = tool(viewer, "viewer-token", "get_cost_report", application="demo")
    report, spend = answer["report"], answer["report"]["actual_spend"]
    assert spend["by_app"] == [{"app": "demo", "net_cost_usd": 7.0}] and spend["previous"] == {"window_days": 30, "by_app": [{"app": "demo", "net_cost_usd": 5.0}]}
    assert spend["app"] == {"name": "demo", "net_cost_usd": 7.0} and spend["movers"] == {"by_app": []}
    assert not {"total_net_cost_usd", "by_service", "by_project", "table"} & set(spend) and not {"month", "cloud_run_cost_recommender"} & set(report)
    assert "actions" not in report["release_tolls"] and "budgets" not in answer and answer["hidden"] == HIDDEN_COSTS
    assert report["rightsizing"]["recommendations"][0]["estimated_monthly_saving_usd"] == 1.42 and report["estimate"]["compute_ceiling_usd_month"] == 12.5
    assert re.search(r"\bother\b", json.dumps({key: value for key, value in answer.items() if key != "hidden"})) is None
    assert f"- Actual spend, last 30 days: $7.00 for `demo`'s own resources, net of credits; $5.00 in the 30 days before\n- {chat.WHOLE_BILL}\n" in answer["summary"]
    assert "This month in" not in answer["summary"] and "moved most" not in answer["summary"]
    everything = tool(operator, "operator-token", "get_cost_report", application="demo")
    assert everything["report"] == Reports().cost_report(types.SimpleNamespace(service="demo"))["report"]  # as the workflow wrote it
    assert "budgets" in everything and "hidden" not in everything and "$9.50 for the whole project" in everything["summary"]


def test_controller_state_for_a_limited_identity_is_its_own_on_the_page_and_in_chat(site, tmp_path, monkeypatch):
    """A background job's result or error can name any application: an identity that does not see every application gets each
    job's last and next run, on the Activity view and from get_controller_status alike, and of the rest of the controller's
    state (applications, drift, re-pins, spend caps, the digest's verdict) only its own. A `*` identity gets all of it."""
    monkeypatch.setattr(server, "Scheduler", Jobs)
    applications = configuration(tmp_path)["applications"]
    applications["other"].update(paused=True, auto_deploy=True, target=ELSEWHERE)
    store = site(Steady(), Drifted(), background=True, applications=applications, platform_updates={},
                 cloud_run_spend_caps={"test-project": 20, "lab-other": 30})
    store.save_platform_update("workflows:other", {"state": "open", "url": "https://github.com/lab/other/pull/20", "waiting": "a person to merge it"})
    viewer, operator = site.client("viewer-token"), site.client()
    said = words_of(view(viewer, "activity"))
    assert ("Job Last run (UTC) Next run Auto-deploy check 21 Oct 10:00 21 Oct 10:01 Watchdog 21 Oct 09:00 22 Oct 09:00 Nightly backup 21 Oct 03:00 "
            "22 Oct 03:00 A job's result can name any application, so the results and errors are shown to an identity that sees every application.") in said
    assert "Infrastructure drift The cloud setup matches the code" in said
    for hidden in ("up to date", "1234567", "no scheduled run recorded", "disk is full", "Drift in"):
        assert hidden not in said, hidden
    whole = words_of(view(operator, "activity"))
    for shown in ("Job Last run (UTC) Result Next run", "demo: up to date; other: released 1234567", "problems: ['other: no scheduled run recorded yet']",
                  "RuntimeError: other's disk is full", "Drift in other (iam)"):
        assert shown in whole, shown
    status = tool(viewer, "viewer-token", "get_controller_status")
    assert status["background"] == {name: {"last_run": entry["last_run"], "next_run": entry["next_run"]} for name, entry in Jobs().snapshot().items()}
    assert status["applications"] == ["demo"] and status["paused_applications"] == [] and status["auto_deploy"] == []
    assert status["spending_guards"]["cloud_run_spend_caps"] == {"test-project": 20} and status["platform_updates"]["applications"] == {}
    assert status["infrastructure_drift"]["status"] == "clean" and status["infrastructure_drift"]["drifted"] == {} and status["infrastructure_drift"]["serious"] == []
    assert "attention" not in status["digest"] and status["hidden"] == HIDDEN_STATUS
    assert re.search(r"\bother\b", json.dumps({key: value for key, value in status.items() if key not in ("summary", "hidden")})) is None
    assert "`other`" not in status["summary"] and f"- {HIDDEN_STATUS.rstrip('.')}\n" in status["summary"]
    everything = tool(operator, "operator-token", "get_controller_status")
    assert everything["background"] == Jobs().snapshot() and everything["applications"] == ["demo", "other"] and "hidden" not in everything
    assert everything["paused_applications"] == ["other"] and everything["auto_deploy"] == ["other"] and "attention" in everything["digest"]
    assert everything["spending_guards"]["cloud_run_spend_caps"] == {"lab-other": 30, "test-project": 20}
    assert everything["infrastructure_drift"]["drifted"] == {"other": ["iam"]} and "other" in everything["platform_updates"]["applications"]
    assert "Weekly cost report not run yet for: `other`" in everything["summary"]


def test_empty_states_say_why(site):
    site(None, seeded=False, digest_hour_utc=13)
    browser = site.client()
    overview = words_of(view(browser, "overview"))
    assert "Live views are off" in overview and "No daily digest yet (written at 13:00 UTC)" in overview and "no service has a release yet" in overview
    assert "Nothing needs you: no release did worse a day later" in overview and "server errors" not in overview.split("Nothing needs you")[1][:120]
    health = words_of(view(browser, "health", app="demo"))
    assert "No release recorded by this controller yet" in health and "observer.service_account" in health
    assert "The daily digest has not run yet: it is written once a day at 13:00 UTC" in words_of(view(browser, "cost"))
    releases = words_of(view(browser, "releases"))
    assert "No release in the last 30 days" in releases and "No release recorded by this controller yet" in releases


def test_empty_states_without_an_export_a_report_or_a_digest(site):
    class Nothing(Reports):
        def cost_report(self, app, workflow="rightsize-gcp.yml", name="cost-report", limit=1_048_576):
            return {"report": None, "note": "No successful rightsizing run with a cost-report artifact yet"}
    store = site(Steady(), Nothing(), seeded=False, digest_hour_utc=None)
    day = datetime.date.today().isoformat()
    store.save_digest(day, {"day": day, "generated_at": iso(time.time() - 60), "services": [], "problems": [],
                            "cost": {"projects": [{"project": "test-project", "environment": "staging", "net_cost_usd": None}],
                                     "warning": "no billing export is recorded (onboarding.billing_export_table), so this month's spend is not read"}})
    browser = site.client()
    cost = words_of(view(browser, "cost"))
    assert "there is no billing export recorded (onboarding.billing_export_table)" in cost
    assert "No weekly cost report with the bill yet: No successful rightsizing run with a cost-report artifact yet" in cost
    assert "The daily digest is off" in words_of(view(browser, "activity"))


def test_signed_out_and_foreign_hosts_see_nothing(site):
    site(Steady())
    browser = site.client(None)
    for tab in ("overview", "health", "cost"):
        page = browser.get("/settings", params={"tab": tab, "app": "demo"})
        assert page.status_code == 200 and "Operator token" in page.text and "demo" not in words_of(page)
    assert site.client(None).get("https://evil.example/settings?tab=overview").status_code in (400, 401)
    assert settings.qualifies(type("P", (), {"applications": ["*"], "can_deploy": True})())


def test_the_overview_counts_releases_and_the_trust_numbers_from_the_records(tmp_path):
    config = load_config_text(tmp_path, observer=False)
    store = Store(config.database)
    seed(store)
    watcher = monitor.Monitor(Controller(config, Reports(), store))
    operator = next(p for p in config.principals if p.name == "operator")
    data = watcher.overview(operator)
    # demo's live staging release did worse; other's uptime check passed 95% on the digest's day; production is healthy.
    assert data["tiles"]["releases"]["week"] == 3 and data["tiles"]["services"] == {"total": 3, "healthy": 1, "bad": 2}
    assert data["tiles"]["trust"]["reached_traffic"] == 4 and not data["checked"]["live"]
    found = watcher.releases(operator)
    assert [row["outcome"] for row in found["rows"]][:2] == ["stopped", "live"] and found["lead_hours"] is not None
    assert len(found["weekly"]) == 8 and found["services"] == [("demo", "production"), ("demo", "staging"), ("other", "staging")]
    assert aftercare.release_words({"state": "failed"}) == "stopped"


# ---- 1.34.0: the long lists have pages

def test_the_long_lists_have_pages(site):
    store = site(Steady())
    now = time.time()
    with store.connect() as db:
        db.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       [(now - 100 - index, "operator", "127.0.0.1", "tool", "list_applications", "demo", "ok", 1, None) for index in range(120)])
    assert store.count_events() == 125 and len(store.find_events(50, offset=100)) == 25
    browser = site.client()  # the sign-in is the 126th event
    first = view(browser, "activity")
    assert 'class="pager"' in first.text and "1–50 of 126 events" in first.text and 'aria-current="page">1<' in first.text
    second = view(browser, "activity", page="2")
    assert "51–100 of 126 events" in second.text and 'rel="prev"' in second.text and 'rel="next"' in second.text
    assert "101–126 of 126 events" in view(browser, "activity", page="99").text  # past the end: the last page
    assert "1–50 of 126 events" in view(browser, "activity", page="x").text  # nonsense: the first
    assert 'class="pager"' not in view(browser, "releases").text  # five releases fit on one page
    rows = [{"application": "demo", "href": f"/settings?tab=deployments&plan={index}", "summary": "Change", "sha": f"{index:08x}", "state": "succeeded",
             "label": "Deployment succeeded"} for index in range(45)]
    from labdeploy import deployment_page
    listed = deployment_page.listing({"rows": rows, "setups": [], "counts": {"active": 0, "attention": 0}}, 3)
    assert "41–45 of 45 deployments" in listed and listed.count('class="d-run"') == 5 and "&amp;page=2" in listed
