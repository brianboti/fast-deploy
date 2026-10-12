"""Deterministic cost model for Cloud Run staging services.

Three layers, each explicit about its evidence:

1. ``estimate``: bounds computed from published list prices and the reviewed
   specification alone. No traffic is assumed unless the caller supplies a scenario.
2. ``findings``: static cost-hygiene checks taken from Google's cost-optimization
   guidance for Cloud Run services.
3. ``recommend``: right-sizing from measured Cloud Monitoring data that the
   rightsizing workflow collects on the deployment runner. Nothing is inferred
   from source code, and nothing is applied automatically.

Prices are list prices for Tier 1 regions as published on the Cloud Run pricing
page and must be re-verified when Google changes them. This module never calls a
pricing API. The ``report`` entry point runs only on the GitHub Actions runner,
never inside the MCP server, which holds no cloud credential.
"""
import datetime
import json
import math
import os
import re
import subprocess
from pathlib import Path

from .models import AppSpec

PRICING = {
    "source": "https://cloud.google.com/run/pricing",
    "verified": "2026-10-11",
    "region_tier": "Tier 1 (includes us-central1)",
    "currency": "USD",
    # CPU allocated only during request processing (the model this platform deploys with).
    "request_based": {"vcpu_second": 0.000024, "gib_second": 0.0000025, "million_requests": 0.40},
    # Minimum instances kept warm between requests.
    "idle": {"vcpu_second": 0.0000025, "gib_second": 0.0000025},
    # CPU always allocated; reported for comparison only, the platform does not deploy this way.
    "instance_based": {"vcpu_second": 0.000018, "gib_second": 0.000002},
    # Shared by every project under one billing account; applied as a discount, not per service.
    "free_tier_monthly": {"vcpu_seconds": 180_000, "gib_seconds": 360_000, "requests": 2_000_000},
}
SECONDS_PER_MONTH = 730 * 3600  # Google's pricing calculator convention
EXCLUDED = [
    "network egress beyond the free allowance",
    "Cloud Logging beyond the free allotment",
    "Artifact Registry image storage",
    "Cloud Storage recovery checkpoints",
    "GitHub Actions minutes",
    "databases or any service outside this deployment",
]
RECOMMENDER = "google.run.service.CostRecommender"
PRICE_CHECK_DAYS = 90
# Charges outside the Cloud Run figures that a release or an idle app still incurs. Described, not added to the bounds.
OVERHEADS = {
    "image_scan_usd_per_release": 0.26,
    "image_scan": "Artifact Analysis charges about USD 0.26 per new image digest while scanning is enabled in Terraform (the default); "
                  "for a small staging app this is often more than the month's compute.",
    "actions_minutes": "About 8 runner minutes per release plus about 120 six-hourly recovery checks a month at one to two minutes each. "
                       "Free for public repositories; private repositories draw on the GitHub plan's monthly allotment.",
    "registry_storage": "About USD 0.10 per GiB-month for stored images. The cleanup policy keeps the newest releases and deletes nothing while it is in dry run.",
}


def _usd(value):
    return round(float(value), 4)


def instance_second_usd(spec, pricing=PRICING, idle=False):
    """Price of one instance-second at the spec's CPU and memory allocation."""
    rates = pricing["idle"] if idle else pricing["request_based"]
    return spec.resources.cpu * rates["vcpu_second"] + spec.resources.memory_mib / 1024 * rates["gib_second"]


def estimate(spec, requests_per_month=None, average_request_seconds=None, pricing=PRICING, today=None):
    """Bounds from list prices. A scenario (requests and average duration) adds a best/worst range."""
    active = instance_second_usd(spec, pricing)
    idle_floor = spec.scaling.minimum * instance_second_usd(spec, pricing, idle=True) * SECONDS_PER_MONTH
    ceiling = spec.scaling.maximum * active * SECONDS_PER_MONTH
    result = {
        "currency": pricing["currency"],
        "billing_model": "request-based: CPU is allocated only while requests are being processed",
        "pricing_source": pricing["source"],
        "pricing_verified": pricing["verified"],
        "region_tier": pricing["region_tier"],
        "per_instance_hour_usd": _usd(active * 3600),
        "idle_floor_usd_month": _usd(idle_floor),
        "compute_ceiling_usd_month": _usd(ceiling),
        "scenario": None,
        "assumptions": [
            "Idle floor: the configured minimum instances kept warm all month at idle rates, with zero traffic.",
            "Compute ceiling: every allowed instance busy for the whole month (730 hours); request fees are additional.",
            "List prices for Tier 1 regions; committed-use discounts and the shared free tier are not applied.",
        ],
        "excludes": list(EXCLUDED),
        "release_overheads": dict(OVERHEADS),
        "warnings": [],
    }
    verified = datetime.date.fromisoformat(pricing["verified"])
    age = ((today or datetime.date.today()) - verified).days
    if age > PRICE_CHECK_DAYS:
        result["warnings"].append(f"List prices were verified on {verified}, {age} days ago; re-check {pricing['source']} before relying on these figures.")
    if requests_per_month is None and average_request_seconds is None:
        return result
    if requests_per_month is None or average_request_seconds is None:
        raise ValueError("A scenario needs both requests_per_month and average_request_seconds")
    if requests_per_month < 0 or average_request_seconds <= 0:
        raise ValueError("Scenario requests must be non-negative and the average duration positive")
    rates, free = pricing["request_based"], pricing["free_tier_monthly"]
    request_seconds = requests_per_month * average_request_seconds
    bounds = {"best_case": request_seconds / spec.concurrency, "worst_case": request_seconds}

    def charge(instance_seconds, apply_free_tier):
        vcpu_seconds = instance_seconds * spec.resources.cpu
        gib_seconds = instance_seconds * spec.resources.memory_mib / 1024
        requests = requests_per_month
        if apply_free_tier:
            vcpu_seconds = max(0.0, vcpu_seconds - free["vcpu_seconds"])
            gib_seconds = max(0.0, gib_seconds - free["gib_seconds"])
            requests = max(0, requests - free["requests"])
        return idle_floor + vcpu_seconds * rates["vcpu_second"] + gib_seconds * rates["gib_second"] + requests / 1_000_000 * rates["million_requests"]

    result["scenario"] = {
        "requests_per_month": requests_per_month,
        "average_request_seconds": average_request_seconds,
        "billable_instance_seconds": {key: round(value, 2) for key, value in bounds.items()},
        "usd_month": {key: _usd(charge(value, False)) for key, value in bounds.items()},
        "usd_month_if_free_tier_is_unused_elsewhere": {key: _usd(charge(value, True)) for key, value in bounds.items()},
        "note": "Best case: requests overlap up to the concurrency limit. Worst case: every request is served alone. "
                "Real usage falls between them; the free tier is shared by the whole billing account.",
    }
    return result


def findings(spec, pricing=PRICING):
    """Static cost-hygiene checks from Google's Cloud Run cost-optimization guidance."""
    items = []
    if spec.scaling.minimum > 0:
        idle = spec.scaling.minimum * instance_second_usd(spec, pricing, idle=True) * SECONDS_PER_MONTH
        items.append({"id": "warm_instances", "severity": "warning", "setting": "scaling.minimum", "current": spec.scaling.minimum,
                      "estimated_monthly_usd": _usd(idle),
                      "message": f"Each warm instance is billed around the clock: about ${idle:.2f} per month with zero traffic. "
                                 "Staging usually tolerates cold starts, so set scaling.minimum: 0 unless a measured latency requirement says otherwise."})
    if spec.resources.memory_mib > 512:
        items.append({"id": "memory_unmeasured", "severity": "info", "setting": "resources.memory_mib", "current": spec.resources.memory_mib,
                      "message": "Memory is billed on what is allocated, not what is used. Keep 512 MiB until observed memory utilization "
                                 "shows more is needed; the rightsizing report measures this."})
    if spec.resources.cpu > 1:
        items.append({"id": "cpu_unmeasured", "severity": "info", "setting": "resources.cpu", "current": spec.resources.cpu,
                      "message": "vCPU-seconds are the largest Cloud Run charge. Confirm CPU utilization under peak load before allocating more than one vCPU."})
    if spec.concurrency < 20:
        items.append({"id": "low_concurrency", "severity": "info", "setting": "concurrency", "current": spec.concurrency,
                      "message": "Low concurrency makes Cloud Run start more instances for the same traffic. Raise it if the application serves "
                                 "parallel I/O-bound requests safely; keep it low for CPU-heavy or non-thread-safe code."})
    ceiling = estimate(spec, pricing=pricing)["compute_ceiling_usd_month"]
    items.append({"id": "worst_case_bound", "severity": "info", "setting": "scaling.maximum", "current": spec.scaling.maximum,
                  "estimated_monthly_usd": ceiling,
                  "message": f"With at most {spec.scaling.maximum} instance(s), compute cannot exceed about ${ceiling:.2f} per month even if every "
                             "instance is busy all month. Lowering scaling.maximum lowers this bound; it is a bound, not a billing cap."})
    items.append({"id": "authenticated_only", "severity": "info", "setting": "exposure", "current": spec.exposure,
                  "message": "Cloud Run requires IAM authentication for this service, so anonymous internet traffic cannot create charges."})
    return items


def recommend(spec, metrics, pricing=PRICING):
    """Measured right-sizing. ``metrics`` comes from telemetry.collect; missing values are tolerated, never guessed."""
    rates = pricing["request_based"]
    days = metrics.get("window_days") or 0
    scale = 30 / days if days else None
    vcpu_seconds, gib_seconds, requests = metrics.get("vcpu_seconds"), metrics.get("gib_seconds"), metrics.get("requests")
    observed = None
    if scale and vcpu_seconds is not None and gib_seconds is not None:
        monthly = (vcpu_seconds * rates["vcpu_second"] + gib_seconds * rates["gib_second"]
                   + (requests or 0) / 1_000_000 * rates["million_requests"]) * scale
        observed = {"window_days": days, "requests": requests, "vcpu_seconds": vcpu_seconds, "gib_seconds": gib_seconds,
                    "billable_instance_seconds": metrics.get("billable_instance_seconds"), "usd_month_at_list_price": _usd(monthly)}
    memory_gib = spec.resources.memory_mib / 1024
    instance_seconds = gib_seconds / memory_gib if gib_seconds else None
    monthly_instance_seconds = instance_seconds * scale if instance_seconds and scale else None
    recommendations = []

    def add(setting, current, recommended, reason, saving, confidence="measured"):
        recommendations.append({"setting": setting, "current": current, "recommended": recommended, "reason": reason,
                                "estimated_monthly_saving_usd": None if saving is None else _usd(saving), "confidence": confidence})

    memory_p95 = metrics.get("memory_utilization_p95")
    if memory_p95 is not None:
        needed_mib = spec.resources.memory_mib * memory_p95 * 1.25
        target = max(512, min(spec.resources.memory_mib, math.ceil(needed_mib / 256) * 256))
        if target < spec.resources.memory_mib:
            saving = (spec.resources.memory_mib - target) / 1024 * rates["gib_second"] * monthly_instance_seconds if monthly_instance_seconds else None
            add("resources.memory_mib", spec.resources.memory_mib, target,
                f"p95 memory utilization was {memory_p95:.0%} over {days} days; {target} MiB keeps 25% headroom", saving)
        elif memory_p95 > 0.85 and spec.resources.memory_mib < 4096:
            extra = 256 / 1024 * rates["gib_second"] * monthly_instance_seconds if monthly_instance_seconds else None
            add("resources.memory_mib", spec.resources.memory_mib, spec.resources.memory_mib + 256,
                f"p95 memory utilization was {memory_p95:.0%}; add headroom to avoid out-of-memory restarts", None if extra is None else -extra)
    cpu_p95 = metrics.get("cpu_utilization_p95")
    if cpu_p95 is not None and spec.resources.cpu > 1 and cpu_p95 < 0.4:
        saving = rates["vcpu_second"] * monthly_instance_seconds if monthly_instance_seconds else None
        add("resources.cpu", spec.resources.cpu, spec.resources.cpu - 1, f"p95 CPU utilization was {cpu_p95:.0%} over {days} days", saving)
    concurrency_p95 = metrics.get("max_concurrency_p95")
    if concurrency_p95 is not None and cpu_p95 is not None and memory_p95 is not None and spec.concurrency < 1000:
        if concurrency_p95 >= 0.8 * spec.concurrency and cpu_p95 < 0.6 and memory_p95 < 0.7:
            add("concurrency", spec.concurrency, min(1000, spec.concurrency * 2),
                f"instances reached {concurrency_p95:.0f} concurrent requests (limit {spec.concurrency}) while CPU and memory had headroom; "
                "fewer instances would serve the same traffic", None, "heuristic")
    if spec.scaling.minimum > 0 and requests is not None and days and requests / days < 1000:
        idle = spec.scaling.minimum * instance_second_usd(spec, pricing, idle=True) * SECONDS_PER_MONTH
        add("scaling.minimum", spec.scaling.minimum, 0, f"only {requests} requests in {days} days do not justify warm instances", idle)
    billable, peak_instances = metrics.get("billable_instance_seconds"), metrics.get("max_active_instances")
    if billable and peak_instances and days:
        busy = billable / (days * 86400 * peak_instances)
        if busy > 0.6:
            add("billing_model", "request-based", "instance-based (not offered by this platform yet)",
                f"instances were busy about {busy:.0%} of the time; steady traffic can be cheaper with always-allocated CPU. "
                "Compare with the Cloud Run cost recommender output in this report", None, "heuristic")
    if requests == 0:
        add(None, None, None, "no requests in the window; a scaled-to-zero service costs nothing, but remove staging services nobody uses", None)
    return {"observed": observed, "recommendations": recommendations,
            "note": "Proposals for the reviewed platform.yaml; nothing is applied automatically. Savings use list prices scaled to a 30-day month."}


def parse_cpu(value):
    text = str(value)
    cores = int(text[:-1]) / 1000 if text.endswith("m") else float(text)
    return max(1, math.ceil(cores))


def parse_memory_mib(value):
    match = re.fullmatch(r"(\d+)(Mi|Gi|M|G)?", str(value))
    if not match:
        raise ValueError(f"Unrecognized memory limit: {value}")
    mib = int(match.group(1)) * {"Mi": 1, "Gi": 1024, "M": 1, "G": 1024}[match.group(2) or "Mi"]
    return max(256, math.ceil(mib / 256) * 256)


def live_settings(description):
    """The cost-driving settings of the deployed service, read from `gcloud run services describe`."""
    template = description["spec"]["template"]
    container = template["spec"]["containers"][0]
    limits = container.get("resources", {}).get("limits", {})
    annotations = template.get("metadata", {}).get("annotations", {})
    return {"cpu": parse_cpu(limits.get("cpu", "1")), "memory_mib": parse_memory_mib(limits.get("memory", "512Mi")),
            "concurrency": int(template["spec"].get("containerConcurrency", 80)),
            "minimum": int(annotations.get("autoscaling.knative.dev/minScale", "0")),
            "maximum": int(annotations.get("autoscaling.knative.dev/maxScale", "100"))}


def _clamp(value, low, high):
    return max(low, min(high, value))


def spec_from_settings(service, settings):
    """Model view of the live service, clamped to schema bounds so the estimator can price it."""
    return AppSpec(name=service, reviewed=True,
                   resources={"cpu": _clamp(settings["cpu"], 1, 4), "memory_mib": _clamp(settings["memory_mib"], 512, 4096)},
                   scaling={"minimum": _clamp(settings["minimum"], 0, 1), "maximum": _clamp(settings["maximum"], 1, 5)},
                   concurrency=_clamp(settings["concurrency"], 1, 1000))


def cost_recommender(project, region, runner=None):
    """Google's own Cloud Run cost recommender, best effort: an unavailable result is reported, never invented."""
    if runner is None:
        from .release import run as runner
    try:
        raw = runner(["gcloud", "recommender", "recommendations", "list", "--project", project, "--location", region,
                      "--recommender", RECOMMENDER, "--format=json"])
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        return {"status": "unavailable", "recommender": RECOMMENDER, "detail": detail[-1][:200] if detail else "gcloud failed"}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"status": "unavailable", "recommender": RECOMMENDER, "detail": str(exc)[:200]}
    items = json.loads(raw or "[]")
    return {"status": "ok", "recommender": RECOMMENDER, "count": len(items), "recommendations": [
        {"name": item.get("name"), "description": item.get("description"), "state": item.get("stateInfo", {}).get("state"),
         "priority": item.get("priority"), "cost_projection": item.get("primaryImpact", {}).get("costProjection")} for item in items]}


def build_report(service, project_id, window_days, description, metrics, recommender, generated_at=None):
    settings = live_settings(description)
    spec = spec_from_settings(service, settings)
    return {"generated_at": generated_at or datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "service": service, "project_id": project_id, "window_days": window_days, "live_settings": settings,
            "estimate": estimate(spec), "findings": findings(spec), "metrics": metrics,
            "rightsizing": recommend(spec, metrics), "cloud_run_cost_recommender": recommender}


def summary_lines(report):
    observed = report["rightsizing"]["observed"]
    lines = [f"Cost report for {report['service']} ({report['window_days']}-day window)",
             f"Worst-case compute bound ${report['estimate']['compute_ceiling_usd_month']:.2f}/month; "
             f"idle floor ${report['estimate']['idle_floor_usd_month']:.2f}/month"]
    if observed:
        lines.append(f"Observed usage priced at list rates: ${observed['usd_month_at_list_price']:.2f}/month ({observed['requests']} requests)")
    else:
        lines.append("No usage metrics were returned for the window")
    for item in report["rightsizing"]["recommendations"]:
        saving = item["estimated_monthly_saving_usd"]
        money = "" if saving is None else f"; about ${saving:.2f}/month"
        lines.append(f"- {item['setting'] or 'note'}: {item['current']} to {item['recommended']} ({item['reason']}{money})")
    return lines


def report():
    """Runner entry point: measure the deployed service and write cost-report.json."""
    from .release import CloudRun, run
    from .target import environment_target
    from .telemetry import CloudMonitoring
    target = environment_target()
    window_days = int(os.environ.get("WINDOW_DAYS", "14"))
    if not 1 <= window_days <= 90:
        raise ValueError("WINDOW_DAYS must be between 1 and 90")
    adapter = CloudRun(target.project_id, target.region, target.service, target.probe_service_account)
    description = adapter.describe()
    monitoring = CloudMonitoring(target.project_id, lambda: run(["gcloud", "auth", "print-access-token"]))
    metrics = monitoring.collect(target.service, window_days)
    result = build_report(target.service, target.project_id, window_days, description, metrics, cost_recommender(target.project_id, target.region))
    Path("cost-report.json").write_text(json.dumps(result, indent=2) + "\n")
    lines = summary_lines(result)
    print("\n".join(lines))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as handle:
            handle.write("## " + lines[0] + "\n\n" + "\n".join(lines[1:]) + "\n")


if __name__ == "__main__":
    report()
