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
import time
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


NEEDS_PRICING = {
    # List prices read on 2026-10-12 from the Cloud Storage, Pub/Sub and Firestore pricing pages (docs/SOURCES.md).
    "bucket": {"storage_usd_per_gib_month": 0.020, "free_gib_months": 5, "note": "Standard storage in us-central1; the Always Free 5 GB is shared by the whole account"},
    "topic": {"usd_per_tib": 40.0, "free_gib_month": 10, "note": "Message delivery, after the account's free 10 GiB each month"},
    "firestore": {"usd_per_100k_reads": 0.03, "usd_per_100k_writes": 0.09, "usd_per_100k_deletes": 0.01, "storage_usd_per_gib_month": 0.15,
                  "free_quota": "none: Google's free daily quota applies to a project's default database only; this platform creates a named database per app",
                  "note": "Standard edition, regional (us-central1): cents per 100,000 operations and 15 cents per GiB-month stored, from the first operation"},
    "postgres": {"instance_usd_month": 7.67, "storage_usd_per_gb_month": 0.09, "storage_gb": 10, "backup_usd_per_gb_month": 0.08,
                 "note": "db-f1-micro (shared core, Enterprise, zonal) in us-central1 with 10 GB of HDD: billed every hour it exists; stopped, only the storage"},
}


def needs_costs(spec, pricing=NEEDS_PRICING):
    """Every declared need with its list price and a monthly floor of zero: these cost by use, nothing while idle."""
    items = []
    for name in spec.needs.buckets:
        items.append({"need": "bucket", "name": name, "floor_usd_month": 0.0, "pricing": pricing["bucket"]})
    for name in spec.needs.topics:
        items.append({"need": "topic", "name": name, "floor_usd_month": 0.0, "pricing": pricing["topic"]})
    if spec.needs.database == "firestore":
        items.append({"need": "firestore", "name": spec.name, "floor_usd_month": 0.0, "pricing": pricing["firestore"]})
    if spec.needs.database == "postgres":
        p = pricing["postgres"]
        floor = p["instance_usd_month"] + p["storage_gb"] * p["storage_usd_per_gb_month"]
        items.append({"need": "postgres", "name": spec.name, "floor_usd_month": _usd(floor), "pricing": p})
    return items


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
        "needs": needs_costs(spec),
        "needs_floor_usd_month": _usd(sum(item["floor_usd_month"] for item in needs_costs(spec))),
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
    if spec.needs.database == "postgres":
        floor = next(item["floor_usd_month"] for item in needs_costs(spec, pricing=NEEDS_PRICING) if item["need"] == "postgres")
        items.append({"id": "database_always_on", "severity": "warning", "setting": "needs.database", "current": "postgres", "estimated_monthly_usd": floor,
                      "message": f"The Cloud SQL instance bills every hour it exists, about ${floor:.2f} per month before traffic, unlike everything else here. "
                                 "Use database: firestore when the data model allows it; when SQL is required, keep the smallest tier and let the spending cap stop it."})
    if spec.needs.buckets or spec.needs.topics or spec.needs.database != "none":
        items.append({"id": "needs_by_use", "severity": "info", "setting": "needs", "current": spec.needs.model_dump(),
                      "message": "Buckets, topics and Firestore cost by use and nothing while idle: 2 cents per GiB-month stored after the free 5 GB, "
                                 "40 dollars per TiB of messages after the free 10 GiB, and cents per 100,000 document operations from the first one (a named database "
                                 "gets no free quota). Keep stored data small and delete what the app no longer needs."})
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
    report = {"generated_at": generated_at or datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
              "service": service, "project_id": project_id, "window_days": window_days, "live_settings": settings,
              "estimate": estimate(spec), "findings": findings(spec), "metrics": metrics,
              "rightsizing": recommend(spec, metrics), "cloud_run_cost_recommender": recommender}
    # The estimate prices a spec clamped to the platform's bounds; a drifted service may allow more instances than that.
    live_max = settings.get("maximum")
    if isinstance(live_max, int) and live_max > spec.scaling.maximum:
        report["estimate"]["live_compute_ceiling_usd_month"] = _usd(live_max * instance_second_usd(spec) * SECONDS_PER_MONTH)
        report["estimate"]["warnings"].append(f"The live service allows {live_max} instances, above the platform's bound of {spec.scaling.maximum}; "
                                              "its real compute bound is live_compute_ceiling_usd_month.")
    return report


def summary_lines(report):
    observed = report["rightsizing"]["observed"]
    lines = [f"Cost report for {report['service']} ({report['window_days']}-day window)",
             f"Worst-case compute bound ${report['estimate']['compute_ceiling_usd_month']:.2f}/month; "
             f"idle floor ${report['estimate']['idle_floor_usd_month']:.2f}/month"]
    if isinstance(report.get("drift"), list) and report["drift"]:
        lines.append("Drift from platform.yaml: " + "; ".join(f"{d['setting']} is {d['live']} live but {d['platform_yaml']} reviewed" for d in report["drift"]))
    actual = report.get("actual_spend") or {}
    if actual.get("by_service") is not None:
        top = ", ".join(f"{r['service']} ${r['net_cost_usd']:.2f}" for r in actual["by_service"][:4])
        lines.append(f"Actual project spend, last {actual['window_days']} days, net of credits: ${actual['total_net_cost_usd']:.2f} ({top or 'nothing billed'})")
        if actual.get("app"):
            lines.append(f"Of which labelled {actual['app']['name']}: ${actual['app']['net_cost_usd']:.2f}")
    elif actual.get("warning"):
        lines.append(f"Actual spend unavailable: {actual['warning']}")
    if observed:
        lines.append(f"Observed usage priced at list rates: ${observed['usd_month_at_list_price']:.2f}/month ({observed['requests']} requests)")
    else:
        lines.append("No usage metrics were returned for the window")
    for item in report["rightsizing"]["recommendations"]:
        saving = item["estimated_monthly_saving_usd"]
        money = "" if saving is None else f"; about ${saving:.2f}/month"
        lines.append(f"- {item['setting'] or 'note'}: {item['current']} to {item['recommended']} ({item['reason']}{money})")
    return lines


def drift(settings, spec):
    """Live Cloud Run settings that differ from the reviewed platform.yaml: what someone changed outside the pipeline."""
    reviewed = {"cpu": spec.resources.cpu, "memory_mib": spec.resources.memory_mib, "concurrency": spec.concurrency,
                "minimum": spec.scaling.minimum, "maximum": spec.scaling.maximum}
    return [{"setting": key, "platform_yaml": reviewed[key], "live": settings.get(key)} for key in reviewed if settings.get(key) != reviewed[key]]


def reviewed_spec_from_github(repository, ref="main", token=None):
    """platform.yaml at the branch tip through the GitHub API, read with the workflow's own token."""
    import base64
    import urllib.request

    from .models import parse_spec
    request = urllib.request.Request(f"https://api.github.com/repos/{repository}/contents/platform.yaml?ref={ref}",
                                     headers={"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed https host
        data = json.load(response)
    return parse_spec(base64.b64decode(data["content"]).decode())


BILLING_QUERY = """
SELECT service.description AS service,
       IFNULL((SELECT l.value FROM UNNEST(labels) l WHERE l.key = 'lab-deploy-app' LIMIT 1), '') AS app,
       ROUND(SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), 4) AS net_cost_usd
FROM `{table}`
WHERE project.id = @project AND usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
GROUP BY service, app
HAVING net_cost_usd <> 0
ORDER BY net_cost_usd DESC
"""


FRESHNESS_QUERY = "SELECT UNIX_SECONDS(MAX(usage_end_time)) AS latest FROM `{table}`"
STALE_AFTER_SECONDS = 2 * 86400  # the export is up to a day behind; two days without data means it is not arriving


def billing_actuals(table, project_id, token, days=30, client=None, app=None, now=None):
    """Actual net spend from the Cloud Billing export in BigQuery, by Google service and by the lab-deploy-app label: the bill, not an estimate.
    An export without recent data makes the spend unknown, never zero."""
    import httpx
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+", table):
        raise ValueError("BILLING_EXPORT_TABLE must be project.dataset.table")

    def query(text, parameters=()):
        body = {"query": text, "useLegacySql": False, "parameterMode": "NAMED", "timeoutMs": 30000, "maxResults": 100, "queryParameters": list(parameters)}
        response = (client or httpx).post(f"https://bigquery.googleapis.com/bigquery/v2/projects/{project_id}/queries", json=body,
                                          headers={"Authorization": "Bearer " + token}, timeout=45)
        response.raise_for_status()
        data = response.json()
        if not data.get("jobComplete", True):
            raise ValueError("BigQuery did not finish the billing query in time")
        return data.get("rows", [])

    rows = query(FRESHNESS_QUERY.format(table=table))
    latest = rows[0]["f"][0]["v"] if rows else None
    latest = float(latest) if latest is not None else None
    data_through = datetime.datetime.fromtimestamp(latest, datetime.UTC).isoformat(timespec="minutes") if latest is not None else None
    if latest is None or (now or time.time()) - latest > STALE_AFTER_SECONDS:
        return {"window_days": days, "table": table, "data_through": data_through, "total_net_cost_usd": None, "by_service": None, "by_app": None,
                "warning": (f"The billing export has no data after {data_through[:10]}" if data_through else "The billing export is empty")
                + "; actual spend is unknown, not zero. An export starts within a day of being enabled; if this lasts, check Billing → "
                  "Billing export in the console (the dataset and the standard usage cost export)."}
    cells = [{"service": row["f"][0]["v"], "app": row["f"][1]["v"] or "", "net_cost_usd": float(row["f"][2]["v"])} for row in query(
        BILLING_QUERY.format(table=table), [{"name": "project", "parameterType": {"type": "STRING"}, "parameterValue": {"value": project_id}},
                                            {"name": "days", "parameterType": {"type": "INT64"}, "parameterValue": {"value": str(days)}}])]
    by_service, by_app = {}, {}
    for cell in cells:
        by_service[cell["service"]] = round(by_service.get(cell["service"], 0.0) + cell["net_cost_usd"], 4)
        label = cell["app"] or "(unlabelled)"
        by_app[label] = round(by_app.get(label, 0.0) + cell["net_cost_usd"], 4)
    result = {"window_days": days, "table": table, "data_through": data_through, "total_net_cost_usd": round(sum(c["net_cost_usd"] for c in cells), 4),
              "by_service": [{"service": s, "net_cost_usd": v} for s, v in sorted(by_service.items(), key=lambda kv: -kv[1])],
              "by_app": [{"app": a, "net_cost_usd": v} for a, v in sorted(by_app.items(), key=lambda kv: -kv[1])],
              "note": "Net of credits, from Google's billing export; up to a day behind. Resources carry the lab-deploy-app label; "
                      "Firestore and Cloud Run request fees are not labelled and show as unlabelled. Compare with usd_month_at_list_price above."}
    if app:
        own = [c for c in cells if c["app"] == app]
        result["app"] = {"name": app, "net_cost_usd": round(sum(c["net_cost_usd"] for c in own), 4),
                         "by_service": sorted(({"service": c["service"], "net_cost_usd": c["net_cost_usd"]} for c in own), key=lambda r: -r["net_cost_usd"])}
    return result


def issue_body(report, run_url):
    """Markdown for the living cost-report issue: the summary lines, proposals, drift and actual spend."""
    lines = ["Updated by the weekly right-sizing workflow. Proposals are suggestions for `platform.yaml`; nothing is applied automatically.", ""]
    lines += [f"- {line}" for line in summary_lines(report)[1:]]
    proposals = (report.get("rightsizing") or {}).get("recommendations") or []
    if proposals:
        lines += ["", "| Setting | Current | Proposed | Why | Monthly saving |", "|---|---|---|---|---|"]
        for item in proposals:
            saving = item.get("estimated_monthly_saving_usd")
            lines.append(f"| `{item.get('setting') or 'note'}` | {item.get('current')} | {item.get('recommended')} | {item.get('reason')} | {'' if saving is None else f'${saving:.2f}'} |")
    drift = report.get("drift")
    if isinstance(drift, list) and drift:
        lines += ["", "Drift from the reviewed configuration:"] + [f"- `{d['setting']}` is {d['live']} live but {d['platform_yaml']} in platform.yaml" for d in drift]
    actual = report.get("actual_spend") or {}
    if actual.get("by_service") is not None:
        lines += ["", f"Actual spend, last {actual['window_days']} days, net of credits: **${actual['total_net_cost_usd']:.2f}**"]
        lines += [f"- {r['service']}: ${r['net_cost_usd']:.2f}" for r in actual["by_service"][:8]]
        if actual.get("by_app"):
            lines += ["", "By application label:"] + [f"- {r['app']}: ${r['net_cost_usd']:.2f}" for r in actual["by_app"][:8]]
    elif actual.get("warning"):
        lines += ["", f"Actual spend unavailable: {actual['warning']}"]
    lines += ["", f"Generated {report.get('generated_at')} by [this run]({run_url}). "
              "Ask Claude for `get_cost_report` for the full JSON, or `propose_rightsizing` to turn proposals into a pull request."]
    return "\n".join(lines)


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
    table = os.environ.get("BILLING_EXPORT_TABLE", "")
    if table:
        try:
            result["actual_spend"] = billing_actuals(table, target.project_id, run(["gcloud", "auth", "print-access-token"]), app=target.service)
        except Exception as exc:  # noqa: BLE001 - the export is optional; the report says why it is missing
            result["actual_spend"] = {"warning": f"Could not read the billing export: {str(exc)[:200]}"}
    repository = os.environ.get("GITHUB_REPOSITORY")
    if repository:
        try:
            result["drift"] = drift(result["live_settings"], reviewed_spec_from_github(repository, os.environ.get("GITHUB_REF_NAME", "main"), os.environ.get("GH_TOKEN")))
        except Exception as exc:  # noqa: BLE001 - drift is best effort; the report says why it is missing
            result["drift"] = {"warning": f"Could not compare with platform.yaml: {str(exc)[:200]}"}
    try:
        from .release import reconcile_serving_tags
        result["serving_tags"] = reconcile_serving_tags(adapter, f"{target.region}-docker.pkg.dev/{target.project_id}/{target.registry}/{target.service}", run)
    except Exception as exc:  # the report is still useful without the tag reconcile
        result["serving_tags"] = {"warning": str(exc)[:200]}
    Path("cost-report.json").write_text(json.dumps(result, indent=2) + "\n")
    lines = summary_lines(result)
    print("\n".join(lines))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as handle:
            handle.write("## " + lines[0] + "\n\n" + "\n".join(lines[1:]) + "\n")


if __name__ == "__main__":
    report()
