"""Phase 25: faster starts and the failures nobody saw (docs/ROADMAP.md 25.1 to 25.6). No test touches GitHub or a cloud."""
import datetime
import json
import types
from pathlib import Path

import httpx
from conftest import TARGET, FakeGitHub
from test_phase23 import NOW, FakeObserver, config

from labdeploy import aftercare, chat
from labdeploy.cloud import Observer, distribution_summary, impacted_locations
from labdeploy.controller import Controller, Store
from labdeploy.models import AppSpec
from labdeploy.release import STARTUP_PROBE_TIMING, CloudRun


# ---- 25.1 and 25.2: the probe's timing and startup CPU boost, on every container of every release
def test_every_startup_probe_tries_each_second_within_the_same_minute_and_the_boost_is_asked_for():
    assert STARTUP_PROBE_TIMING == "initialDelaySeconds=0,periodSeconds=1,timeoutSeconds=1,failureThreshold=60"
    calls = []
    adapter = CloudRun("test-project", "us-central1", "demo", "demo-probe@test-project.iam.gserviceaccount.com", runner=lambda args: calls.append(args) or "")
    adapter.container_names = lambda: ["app", "proxy"]
    spec = AppSpec.model_validate({"name": "demo", "reviewed": True, "health_path": None, "port": 3000, "runtime": "node-http", "node": "24",
                                   "proxy": True})
    adapter.deploy("us-central1-docker.pkg.dev/test-project/demo/demo@sha256:" + "c" * 64, spec, "rabc")
    deploy = next(call for call in calls if call[:3] == ["gcloud", "run", "deploy"])
    probes = [deploy[index + 1] for index, flag in enumerate(deploy) if flag == "--startup-probe"]
    assert len(probes) == 2 and all(probe.endswith(STARTUP_PROBE_TIMING) for probe in probes)  # the proxy's and the app's
    assert probes[1].startswith("tcpSocket.port=3000,") and "--cpu-boost" in deploy


# ---- 25.3: the maximum-instances alert at the application's own limit
def test_the_saturation_alert_follows_the_contracts_maximum(tmp_path):
    def rendered(maximum, infra=None):
        cfg = config(tmp_path)
        if infra:
            cfg.applications["demo"].infra = infra
        gh = FakeGitHub()
        gh.spec = {"name": "demo", "reviewed": True, "scaling": {"maximum": maximum}}
        main = Controller(cfg, gh, Store(cfg.database)).render_infrastructure(cfg.principals[0], "demo", state_bucket="test-project-tfstate")["files"]["main.tf"]
        return next(line.split("=")[1].strip() for line in main.splitlines() if line.strip().startswith("instance_alert_count"))
    assert rendered(3) == "3" and rendered(2) == "2"
    assert rendered(1) == "2"  # one busy instance is an application of one's ordinary state: the refused-requests alert watches it
    assert rendered(3, {"instance_alert_count": 5}) == "5"  # the operator's own setting wins


# ---- 25.4 and 25.6: Google's incidents and access changes, in the foundation
def test_the_foundation_alerts_on_googles_incidents_and_on_access_changed_by_hand():
    main = Path("infra/gcp/modules/project/main.tf").read_text()
    assert '"servicehealth.googleapis.com", "dlp.googleapis.com"])' in main  # Sensitive Data Protection since 1.30.0 (14.10)
    google = main[main.index('resource "google_monitoring_alert_policy" "google_incident"'):]
    google = google[:google.index("\n}\n")]
    assert 'logs/servicehealth.googleapis.com%2Factivity' in google and 'labels.\\"servicehealth.googleapis.com/new_event\\"=\\"true\\"' in google
    assert 'jsonPayload.category=\\"INCIDENT\\"' in google and 'jsonPayload.impactedLocations=~\\"${var.region}|global\\"' in google
    assert 'severity              = "WARNING"' in google and "rolling back a release will not help" in google
    access = main[main.index('resource "google_monitoring_alert_policy" "access_changed"'):]
    access = access[:access.index("\n}\n")]
    assert 'cloudaudit.googleapis.com%2Factivity' in access and 'protoPayload.methodName:\\"SetIamPolicy\\"' in access
    assert 'NOT protoPayload.authenticationInfo.principalEmail:\\"lab-deploy-infra-\\"' in access  # the pipeline's own grants are expected
    assert "var.access_change_quiet_members" in access and 'severity              = "ERROR"' in access
    root = Path("infra/gcp/project/main.tf").read_text() + Path("infra/gcp/project/variables.tf").read_text()
    assert "access_change_quiet_members  = var.access_change_quiet_members" in root and 'variable "access_change_quiet_members"' in root


# ---- 25.5: the observer's reads for the digest
def observer(handler):
    cfg = types.SimpleNamespace(observer=types.SimpleNamespace(service_account="lab-deploy-observer@test-project.iam.gserviceaccount.com", cache_seconds=300))
    found = Observer(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))
    found.token = lambda: "token"
    return found


def test_a_start_up_distribution_gives_the_count_the_95th_percentile_and_the_slowest():
    dist = {"count": "10", "mean": 6000.0, "bucketOptions": {"explicitBuckets": {"bounds": [1000, 5000, 10000, 60000]}},
            "bucketCounts": ["0", "2", "7", "1", "0"]}
    assert distribution_summary([dist]) == {"starts": 10, "mean_s": 6.0, "p95_s": 35.0, "slowest_s": 60.0}


def test_refused_requests_googles_incidents_and_quotas_are_read_as_documented():
    seen = []

    def handler(request):
        if request.url.path == "/v2/entries:list":
            body = json.loads(request.content)
            seen.append(body["filter"])
            if "servicehealth" in body["filter"]:
                event = {"eventId": "e1", "title": "Cloud Run elevated errors", "state": "ACTIVE", "relevance": "RELATED",
                         "impactedLocations": ["us-central1"], "category": "INCIDENT"}
                return httpx.Response(200, json={"entries": [{"jsonPayload": event, "timestamp": "t2"}, {"jsonPayload": {**event, "state": "CLOSED"}, "timestamp": "t1"}]})
            return httpx.Response(200, json={"entries": [{}, {}, {}]})
        metric = request.url.params["filter"]
        if "allocation/usage" in metric:
            series = [{"resource": {"labels": {"service": "monitoring.googleapis.com"}}, "metric": {"labels": {"quota_metric": "monitoring.googleapis.com/alertPolicies/count"}},
                       "points": [{"value": {"int64Value": "430"}}]},
                      {"resource": {"labels": {"service": "iam.googleapis.com"}}, "metric": {"labels": {"quota_metric": "iam.googleapis.com/roles/custom_roles_count"}},
                       "points": [{"value": {"int64Value": "9"}}]}]
            return httpx.Response(200, json={"timeSeries": series})
        if "quota/limit" in metric:
            series = [{"resource": {"labels": {"service": "monitoring.googleapis.com"}}, "metric": {"labels": {"quota_metric": "monitoring.googleapis.com/alertPolicies/count"}},
                       "points": [{"value": {"int64Value": "500"}}]},
                      {"resource": {"labels": {"service": "iam.googleapis.com"}}, "metric": {"labels": {"quota_metric": "iam.googleapis.com/roles/custom_roles_count"}},
                       "points": [{"value": {"int64Value": "300"}}]}]
            return httpx.Response(200, json={"timeSeries": series})
        raise AssertionError(str(request.url))
    found = observer(handler)
    target = types.SimpleNamespace(**TARGET)
    assert found.refused_requests(target, 60) == 3 and "httpRequest.status=429" in seen[0] and "run.googleapis.com%2Frequests" in seen[0]
    incidents = found.google_incidents("test-project")
    assert incidents == [{"title": "Cloud Run elevated errors", "state": "ACTIVE", "detailed_state": None, "relevance": "RELATED",
                          "locations": ["us-central1"], "at": "t2"}]  # one line per incident, its newest state
    assert found.quota_pressure("test-project") == [{"service": "monitoring.googleapis.com", "quota": "monitoring.googleapis.com/alertPolicies/count",
                                                     "usage": 430.0, "limit": 500.0, "share": 0.86}]


def test_an_incidents_locations_are_read_from_the_text_service_health_writes():
    # Live (2026-10-22): impactedLocations is one text that looks like a Python list, and the digest said "in [, ', a, f, r".
    def handler(request):
        event = {"eventId": "e1", "title": "Vertex AI errors", "state": "ACTIVE", "impactedLocations": "['africa-south1', 'us-central1']"}
        return httpx.Response(200, json={"entries": [{"jsonPayload": event, "timestamp": "t"}]})
    incidents = observer(handler).google_incidents("test-project")
    assert incidents[0]["locations"] == ["africa-south1", "us-central1"]
    assert impacted_locations(["us-east1"]) == ["us-east1"] and impacted_locations(None) == [] and impacted_locations("[]") == []
    assert impacted_locations("us-east1, global") == ["us-east1", "global"] and len(impacted_locations(str([f"r{n}" for n in range(9)]))) == 5
    record = {"day": "2026-10-22", "google": incidents}
    assert "Google reports an incident (active): Vertex AI errors, in africa-south1, us-central1." in chat.digest(record)


# ---- 25.3 to 25.5 in the digest and the health view
class DayObserver(FakeObserver):
    def refused_requests(self, target, minutes=1440, cap=200):
        return 7

    def starts(self, target, start, end):
        return {"starts": 3, "mean_s": 18.0, "p95_s": 26.0, "slowest_s": 27.3}

    def google_incidents(self, project_id, minutes=1440):
        return [{"title": "Cloud Run elevated errors", "state": "ACTIVE", "locations": ["us-central1"]}]

    def quota_pressure(self, project_id):
        return [{"service": "monitoring.googleapis.com", "quota": "monitoring.googleapis.com/alertPolicies/count", "usage": 430.0, "limit": 500.0, "share": 0.86}]


def test_the_digest_names_refused_requests_slow_starts_googles_incidents_and_quotas(tmp_path):
    cfg = config(tmp_path)
    c = Controller(cfg, FakeGitHub(), Store(cfg.database), DayObserver())
    c.watchdog_problems = lambda now=None: []
    record = c.build_digest(NOW + 86_400)
    assert record["attention"] and record["services"][0]["refused"] == 7 and record["slow_starts"][0]["application"] == "demo"
    text = chat.digest(record)
    assert ("Google reports an incident (active): Cloud Run elevated errors, in us-central1. Errors now are likely Google's; "
            "a rollback will not help") in text
    assert "`demo`, last day: 7 requests refused (429: every instance busy, or the application said no)" in text
    assert "`test-project`: the quota monitoring.googleapis.com/alertPolicies/count is at 86% of its limit (430 of 500)" in text
    assert "Slowest starts in the last day: `demo` 27.3 s (3 starts)" in text
    assert aftercare.slow_starts([{"application": "x", "starts": {"slowest_s": 4.0, "starts": 2}}]) == []


def test_the_health_view_says_when_requests_were_refused(tmp_path):
    cfg = config(tmp_path)
    health = Controller(cfg, FakeGitHub(), Store(cfg.database), DayObserver()).service_health(cfg.principals[0], "demo")
    assert any("7 request(s) answered 429" in note for note in health["notes"]) and health["metrics"]["refused"] == 7
    text = chat.health(health)
    assert "**Next:** look at the error lines" in text


def test_the_review_and_the_runbook_sections_exist():
    review = Path("docs/RESEARCH-2026-10-21-scaling.md").read_text()
    assert "## 6. A plan for every issue" in review and "STARTUP_PROBE_TIMING" not in review
    runbook = Path("docs/RUNBOOK.md").read_text()
    assert "## Requests refused" in runbook and "## Slow starts" in runbook and "## Google's own incidents" in runbook
    assert datetime.date(2026, 10, 21)  # the measurements' date, recorded in the review
