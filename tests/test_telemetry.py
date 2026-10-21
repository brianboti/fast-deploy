import datetime

import httpx
import pytest

from labdeploy.telemetry import CloudMonitoring

NOW = datetime.datetime(2026, 10, 11, 12, 0, tzinfo=datetime.UTC)


def monitoring(handler):
    return CloudMonitoring("test-project", lambda: "tok", httpx.Client(transport=httpx.MockTransport(handler)))


def test_collect_builds_scoped_aggregated_queries():
    seen = []

    def handler(request):
        params = request.url.params
        seen.append(params["filter"])
        assert request.url.path == "/v3/projects/test-project/timeSeries"
        assert request.headers["authorization"] == "Bearer tok"
        assert 'resource.labels.service_name="demo"' in params["filter"] and 'resource.type="cloud_run_revision"' in params["filter"]
        assert params["interval.startTime"] == "2026-09-27T12:00:00Z" and params["interval.endTime"] == "2026-10-11T12:00:00Z"
        metric = params["filter"].split('"')[1]
        if metric == "logging.googleapis.com/user/lab-deploy/demo/requests":  # the objective's counter of users' requests (26.1)
            assert params["aggregation.perSeriesAligner"] == "ALIGN_SUM" and params["aggregation.alignmentPeriod"] == f"{14 * 86400}s"
            return httpx.Response(200, json={"timeSeries": [{"points": [{"value": {"int64Value": "31"}}]}]})
        if metric.endswith("allocation_time") or metric.endswith("billable_instance_time") or metric.endswith("request_count"):
            assert params["aggregation.perSeriesAligner"] == "ALIGN_SUM" and params["aggregation.crossSeriesReducer"] == "REDUCE_SUM"
            assert params["aggregation.alignmentPeriod"] == f"{14 * 86400}s"
            value = {"int64Value": "1200"} if metric.endswith("request_count") else {"doubleValue": 42.5}
            return httpx.Response(200, json={"timeSeries": [{"points": [{"value": value}, {"value": value}]}]})
        if metric.endswith("instance_count"):
            assert params["aggregation.perSeriesAligner"] == "ALIGN_MAX" and params["aggregation.alignmentPeriod"] == "300s"
            assert 'metric.labels.state="active"' in params["filter"]
            return httpx.Response(200, json={"timeSeries": [{"points": [{"value": {"int64Value": "1"}}, {"value": {"int64Value": "3"}}]}]})
        assert params["aggregation.perSeriesAligner"].startswith("ALIGN_PERCENTILE_") and params["aggregation.crossSeriesReducer"] == "REDUCE_MAX"
        if metric.endswith("max_request_concurrencies"):
            assert 'metric.labels.state="active"' in params["filter"]
        return httpx.Response(200, json={"timeSeries": [{"points": [{"value": {"doubleValue": 0.25}}]}]})

    metrics = monitoring(handler).collect("demo", 14, now=NOW)
    assert metrics["vcpu_seconds"] == 85.0 and metrics["requests"] == 2400 and metrics["max_active_instances"] == 3
    assert metrics["cpu_utilization_p95"] == 0.25 and metrics["request_latency_ms_p95"] == 0.25
    assert metrics["window_start"] == "2026-09-27T12:00:00Z" and len(seen) == 11 and metrics["user_requests"] == 31


def test_missing_data_stays_none_and_pages_are_followed():
    calls = []

    def handler(request):
        calls.append(request.url.params.get("pageToken"))
        if "request_count" in request.url.params["filter"]:
            if request.url.params.get("pageToken") is None:
                return httpx.Response(200, json={"timeSeries": [{"points": [{"value": {"int64Value": "5"}}]}], "nextPageToken": "p2"})
            return httpx.Response(200, json={"timeSeries": [{"points": [{"value": {"int64Value": "7"}}]}]})
        return httpx.Response(200, json={})

    metrics = monitoring(handler).collect("demo", 7, now=NOW)
    assert metrics["requests"] == 12 and "p2" in calls
    assert metrics["vcpu_seconds"] is None and metrics["memory_utilization_p95"] is None and metrics["max_active_instances"] is None
    assert metrics["user_requests"] == 0  # the counter exists and counted nobody


def test_a_service_without_an_objective_has_unknown_user_requests():
    def handler(request):
        if "logging.googleapis.com/user/" in request.url.params["filter"]:
            return httpx.Response(404, json={"error": {"message": "Cannot find metric(s) that match type"}})
        return httpx.Response(200, json={})
    assert monitoring(handler).collect("demo", 7, now=NOW)["user_requests"] is None


def test_rejects_bad_input_and_surfaces_permission_errors():
    client = monitoring(lambda request: httpx.Response(403, json={"error": {"message": "denied"}}))
    with pytest.raises(ValueError, match="service name"):
        client.collect('demo" OR 1=1', 7)
    with pytest.raises(ValueError, match="window_days"):
        client.collect("demo", 0)
    with pytest.raises(httpx.HTTPStatusError):
        client.collect("demo", 7, now=NOW)
