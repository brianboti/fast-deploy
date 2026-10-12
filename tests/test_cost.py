import json
import subprocess
import sys

import pytest

from labdeploy.cost import PRICING, SECONDS_PER_MONTH, build_report, cost_recommender, estimate, findings, live_settings, parse_cpu, parse_memory_mib, recommend, spec_from_settings, summary_lines
from labdeploy.models import AppSpec


def spec(**overrides):
    return AppSpec.model_validate({"name": "demo", "reviewed": True, **overrides})


def test_bounds_follow_list_prices():
    result = estimate(spec())
    per_second = 1 * PRICING["request_based"]["vcpu_second"] + 0.5 * PRICING["request_based"]["gib_second"]
    assert result["compute_ceiling_usd_month"] == round(2 * per_second * SECONDS_PER_MONTH, 4) == 132.714
    assert result["idle_floor_usd_month"] == 0.0 and result["scenario"] is None
    assert result["pricing_source"].startswith("https://") and "egress" in " ".join(result["excludes"])
    warm = estimate(spec(scaling={"minimum": 1, "maximum": 2}))
    assert warm["idle_floor_usd_month"] == round((0.0000025 + 0.5 * 0.0000025) * SECONDS_PER_MONTH, 4) == 9.855
    assert estimate(spec(resources={"cpu": 2, "memory_mib": 2048}, scaling={"minimum": 0, "maximum": 3}))["compute_ceiling_usd_month"] == round(3 * (0.000048 + 0.000005) * SECONDS_PER_MONTH, 4)


def test_scenario_gives_best_and_worst_range_and_free_tier_view():
    result = estimate(spec(), requests_per_month=100_000, average_request_seconds=0.2)
    scenario = result["scenario"]
    assert scenario["billable_instance_seconds"] == {"best_case": 250.0, "worst_case": 20000.0}
    assert scenario["usd_month"]["worst_case"] == 0.545 and scenario["usd_month"]["best_case"] == 0.0463
    assert scenario["usd_month_if_free_tier_is_unused_elsewhere"] == {"best_case": 0.0, "worst_case": 0.0}
    big = estimate(spec(), requests_per_month=50_000_000, average_request_seconds=0.5)["scenario"]
    assert big["usd_month_if_free_tier_is_unused_elsewhere"]["worst_case"] < big["usd_month"]["worst_case"]


@pytest.mark.parametrize("requests,seconds", [(100, None), (None, 0.2), (-1, 0.2), (100, 0)])
def test_scenario_rejects_incomplete_or_invalid_input(requests, seconds):
    with pytest.raises(ValueError):
        estimate(spec(), requests, seconds)


def test_findings_flag_warm_instances_and_unmeasured_resources():
    ids = {item["id"]: item for item in findings(spec(scaling={"minimum": 1, "maximum": 2}, resources={"cpu": 2, "memory_mib": 1024}, concurrency=8))}
    assert ids["warm_instances"]["severity"] == "warning" and ids["warm_instances"]["estimated_monthly_usd"] > 0
    assert {"memory_unmeasured", "cpu_unmeasured", "low_concurrency", "worst_case_bound", "authenticated_only"} <= set(ids)
    lean = {item["id"] for item in findings(spec())}
    assert lean == {"worst_case_bound", "authenticated_only"}


def test_recommend_memory_cpu_concurrency_and_warm_instances():
    metrics = {"window_days": 15, "requests": 500, "vcpu_seconds": 200_000, "gib_seconds": 100_000, "billable_instance_seconds": 100_000,
               "cpu_utilization_p95": 0.2, "memory_utilization_p95": 0.2, "max_concurrency_p95": 70, "max_active_instances": 1}
    result = recommend(spec(resources={"cpu": 2, "memory_mib": 1024}, scaling={"minimum": 1, "maximum": 2}), metrics)
    assert result["observed"]["usd_month_at_list_price"] == round((200_000 * 0.000024 + 100_000 * 0.0000025 + 500 / 1e6 * 0.4) * 2, 4)
    by_setting = {item["setting"]: item for item in result["recommendations"]}
    assert by_setting["resources.memory_mib"]["recommended"] == 512
    assert by_setting["resources.memory_mib"]["estimated_monthly_saving_usd"] == round(0.5 * 0.0000025 * 100_000 * 2, 4)
    assert by_setting["resources.cpu"]["recommended"] == 1 and by_setting["resources.cpu"]["estimated_monthly_saving_usd"] == round(0.000024 * 100_000 * 2, 4)
    assert by_setting["concurrency"]["recommended"] == 160 and by_setting["concurrency"]["confidence"] == "heuristic"
    assert by_setting["scaling.minimum"]["recommended"] == 0 and by_setting["scaling.minimum"]["estimated_monthly_saving_usd"] > 0


def test_recommend_never_invents_data():
    empty = recommend(spec(), {"window_days": 14})
    assert empty["observed"] is None and empty["recommendations"] == []
    quiet = recommend(spec(), {"window_days": 14, "requests": 0, "vcpu_seconds": 0, "gib_seconds": 0})
    assert quiet["observed"]["usd_month_at_list_price"] == 0 and quiet["recommendations"][0]["setting"] is None
    hot = recommend(spec(resources={"cpu": 1, "memory_mib": 512}), {"window_days": 7, "memory_utilization_p95": 0.9, "gib_seconds": 3500})
    assert hot["recommendations"][0]["recommended"] == 768 and hot["recommendations"][0]["estimated_monthly_saving_usd"] < 0
    steady = recommend(spec(), {"window_days": 7, "billable_instance_seconds": 7 * 86400 * 0.8, "max_active_instances": 1})
    assert steady["recommendations"][0]["setting"] == "billing_model"


def test_live_settings_parse_cloud_run_description():
    assert parse_cpu("1000m") == 1 and parse_cpu("2") == 2 and parse_cpu("500m") == 1
    assert parse_memory_mib("512Mi") == 512 and parse_memory_mib("1Gi") == 1024 and parse_memory_mib("300Mi") == 512
    description = {"spec": {"template": {"metadata": {"annotations": {"autoscaling.knative.dev/maxScale": "2"}},
                                         "spec": {"containerConcurrency": 80, "containers": [{"resources": {"limits": {"cpu": "1000m", "memory": "512Mi"}}}]}}}}
    settings = live_settings(description)
    assert settings == {"cpu": 1, "memory_mib": 512, "concurrency": 80, "minimum": 0, "maximum": 2}
    clamped = spec_from_settings("demo", {"cpu": 8, "memory_mib": 16384, "concurrency": 5000, "minimum": 3, "maximum": 100})
    assert (clamped.resources.cpu, clamped.resources.memory_mib, clamped.concurrency, clamped.scaling.minimum, clamped.scaling.maximum) == (4, 4096, 1000, 1, 5)


def test_cost_recommender_is_best_effort():
    payload = [{"name": "projects/p/locations/us-central1/recommenders/google.run.service.CostRecommender/recommendations/1",
                "description": "Switch to CPU always allocated", "priority": "P4", "stateInfo": {"state": "ACTIVE"},
                "primaryImpact": {"category": "COST", "costProjection": {"cost": {"currencyCode": "USD", "units": "-3"}}}}]
    ok = cost_recommender("p", "us-central1", runner=lambda args: json.dumps(payload))
    assert ok["status"] == "ok" and ok["count"] == 1 and ok["recommendations"][0]["cost_projection"]["cost"]["units"] == "-3"

    def denied(args):
        raise subprocess.CalledProcessError(1, args, stderr="ERROR: (gcloud.recommender) PERMISSION_DENIED: missing permission")
    assert cost_recommender("p", "us-central1", runner=denied)["status"] == "unavailable"
    assert "PERMISSION_DENIED" in cost_recommender("p", "us-central1", runner=denied)["detail"]


def test_report_and_summary_shape():
    description = {"spec": {"template": {"metadata": {"annotations": {"autoscaling.knative.dev/maxScale": "2"}},
                                         "spec": {"containerConcurrency": 80, "containers": [{"resources": {"limits": {"cpu": "1", "memory": "1024Mi"}}}]}}}}
    metrics = {"window_days": 14, "requests": 1000, "vcpu_seconds": 5000, "gib_seconds": 5000, "memory_utilization_p95": 0.1}
    report = build_report("demo", "p", 14, description, metrics, {"status": "unavailable"}, generated_at="2026-10-11T00:00:00+00:00")
    assert report["live_settings"]["memory_mib"] == 1024 and report["rightsizing"]["recommendations"][0]["recommended"] == 512
    lines = summary_lines(report)
    assert lines[0].startswith("Cost report for demo") and any("resources.memory_mib" in line for line in lines)
    assert json.loads(json.dumps(report)) == report


def test_cli_cost_command():
    result = subprocess.run([sys.executable, "-m", "labdeploy.cli", "cost", "examples/demo-app/platform.yaml", "--requests", "100000", "--average-seconds", "0.2"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["scenario"]["usd_month"]["worst_case"] == 0.545 and payload["findings"][-1]["id"] == "authenticated_only"
    half = subprocess.run([sys.executable, "-m", "labdeploy.cli", "cost", "examples/demo-app/platform.yaml", "--requests", "100000"], capture_output=True, text=True)
    assert half.returncode == 2 and "both" in half.stderr
