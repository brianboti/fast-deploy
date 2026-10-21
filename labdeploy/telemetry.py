"""Read-only Cloud Monitoring queries: right-sizing on the deployment runner, and the controller's live views and charts."""
import datetime
import math
import re

import httpx

RESOURCE = 'resource.type="cloud_run_revision"'
BASE = "https://monitoring.googleapis.com/v3"


def _iso(moment):
    return moment.astimezone(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---- distributions (Cloud Run's request latency): histograms as {(low, high): count}
def bucket_bounds(options):
    """The [low, high) range of every bucket a distribution's bucketOptions describe: the underflow bucket first, the
    overflow bucket last (Monitoring API, Distribution.BucketOptions)."""
    if "exponentialBuckets" in options:
        spec = options["exponentialBuckets"]
        edges = [float(spec.get("scale", 1)) * float(spec.get("growthFactor", 2)) ** index for index in range(int(spec.get("numFiniteBuckets", 0)) + 1)]
    elif "linearBuckets" in options:
        spec = options["linearBuckets"]
        edges = [float(spec.get("offset", 0)) + float(spec.get("width", 1)) * index for index in range(int(spec.get("numFiniteBuckets", 0)) + 1)]
    elif "explicitBuckets" in options:
        edges = [float(bound) for bound in options["explicitBuckets"].get("bounds") or []]
    else:
        return []
    if not edges:
        return [(-math.inf, math.inf)]
    return [(-math.inf, edges[0]), *zip(edges, edges[1:], strict=False), (edges[-1], math.inf)]


def histogram(value):
    """One distribution point as {(low, high): count}, the empty buckets left out; {} when it counted nothing."""
    distribution = (value or {}).get("distributionValue") or {}
    counts = [int(count) for count in distribution.get("bucketCounts") or []]
    bounds = bucket_bounds(distribution.get("bucketOptions") or {})
    if not bounds or not counts:
        count, mean = int(distribution.get("count") or 0), float(distribution.get("mean") or 0)
        return {(mean, mean): count} if count else {}
    return {bounds[index]: count for index, count in enumerate(counts[:len(bounds)]) if count}


def merge(*histograms):
    merged = {}
    for item in histograms:
        for bucket, count in item.items():
            merged[bucket] = merged.get(bucket, 0) + count
    return merged


def percentile(counted, share):
    """The value below which `share` of a histogram's samples fall, interpolated within its bucket as Monitoring's own
    percentile aligners do; the overflow bucket gives its lower edge. None for an empty histogram."""
    total = sum(counted.values())
    if not total:
        return None
    wanted, seen = share * total, 0
    ordered = sorted(counted.items())
    for (low, high), count in ordered:
        if seen + count >= wanted:
            if high == math.inf:
                return low
            low = min(0.0, high) if low == -math.inf else low
            return low + (high - low) * (wanted - seen) / count
        seen += count
    return ordered[-1][0][0]


class CloudMonitoring:
    def __init__(self, project, token, client=None):
        self.project, self.token = project, token
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)

    def query(self, metric, aligner, reducer, start, end, alignment_seconds, extra_filter=None):
        """Aligned and reduced point values for one metric type; empty when nothing was recorded."""
        condition = f'metric.type="{metric}" AND {RESOURCE}'
        if extra_filter:
            condition += " AND " + extra_filter
        params = {"filter": condition, "interval.startTime": _iso(start), "interval.endTime": _iso(end),
                  "aggregation.alignmentPeriod": f"{int(alignment_seconds)}s", "aggregation.perSeriesAligner": aligner,
                  "aggregation.crossSeriesReducer": reducer, "view": "FULL"}
        values = []
        while True:
            response = self.client.get(f"{BASE}/projects/{self.project}/timeSeries", params=params,
                                       headers={"Authorization": "Bearer " + self.token()})
            response.raise_for_status()
            data = response.json()
            for series in data.get("timeSeries", []):
                for point in series.get("points", []):
                    value = point.get("value", {})
                    if "doubleValue" in value:
                        values.append(float(value["doubleValue"]))
                    elif "int64Value" in value:
                        values.append(int(value["int64Value"]))
            page = data.get("nextPageToken")
            if not page:
                return values
            params = {**params, "pageToken": page}

    def points(self, filter_text, aligner, reducer, start, end, alignment_seconds):
        """Every aligned point of the series a full filter selects, as (end time, value), oldest first; for questions that
        need to know when (the newest day with errors), where `query` only gives the values."""
        params = {"filter": filter_text, "interval.startTime": _iso(start), "interval.endTime": _iso(end),
                  "aggregation.alignmentPeriod": f"{int(alignment_seconds)}s", "aggregation.perSeriesAligner": aligner,
                  "aggregation.crossSeriesReducer": reducer, "view": "FULL"}
        found = []
        while True:
            response = self.client.get(f"{BASE}/projects/{self.project}/timeSeries", params=params,
                                       headers={"Authorization": "Bearer " + self.token()})
            response.raise_for_status()
            data = response.json()
            for series in data.get("timeSeries", []):
                for point in series.get("points", []):
                    value = point.get("value", {})
                    number = float(value["doubleValue"]) if "doubleValue" in value else int(value["int64Value"]) if "int64Value" in value else None
                    if number is not None:
                        found.append(((point.get("interval") or {}).get("endTime") or "", number))
            page = data.get("nextPageToken")
            if not page:
                return sorted(found)
            params = {**params, "pageToken": page}

    def distributions(self, filter_text, start, end, alignment_seconds, group_by=None):
        """A distribution metric (Cloud Run's request latency) summed per alignment period across the series a filter selects,
        kept apart by one label when `group_by` names it (metric.label.response_code_class): {label value: [(end time,
        histogram)]}, oldest first. One call returns one time series per label value, however many points each holds."""
        params = {"filter": filter_text, "interval.startTime": _iso(start), "interval.endTime": _iso(end),
                  "aggregation.alignmentPeriod": f"{int(alignment_seconds)}s", "aggregation.perSeriesAligner": "ALIGN_DELTA",
                  "aggregation.crossSeriesReducer": "REDUCE_SUM", "view": "FULL"}
        if group_by:
            params["aggregation.groupByFields"] = group_by
        label = group_by.rsplit(".", 1)[-1] if group_by else None
        found = {}
        while True:
            response = self.client.get(f"{BASE}/projects/{self.project}/timeSeries", params=params,
                                       headers={"Authorization": "Bearer " + self.token()})
            response.raise_for_status()
            data = response.json()
            for series in data.get("timeSeries", []):
                key = str(((series.get("metric") or {}).get("labels") or {}).get(label, "")) if label else ""
                points = found.setdefault(key, {})
                for point in series.get("points", []):
                    at = (point.get("interval") or {}).get("endTime") or ""
                    points[at] = merge(points.get(at, {}), histogram(point.get("value")))
            page = data.get("nextPageToken")
            if not page:
                return {key: sorted(points.items()) for key, points in found.items()}
            params = {**params, "pageToken": page}

    def collect(self, service, window_days=14, now=None):
        """Normalized usage for one service over the window. Missing data stays None; it is never estimated."""
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", service):
            raise ValueError("Invalid service name")
        if not 1 <= window_days <= 90:
            raise ValueError("window_days must be between 1 and 90")
        end = now or datetime.datetime.now(datetime.UTC)
        start = end - datetime.timedelta(days=window_days)
        whole = window_days * 86400
        by_service = f'resource.labels.service_name="{service}"'

        def total(metric, extra=None):
            values = self.query(metric, "ALIGN_SUM", "REDUCE_SUM", start, end, whole, " AND ".join(filter(None, (by_service, extra))))
            return sum(values) if values else None

        def peak(metric, aligner, extra=None, alignment=whole):
            values = self.query(metric, aligner, "REDUCE_MAX", start, end, alignment, " AND ".join(filter(None, (by_service, extra))))
            return max(values) if values else None

        def user_requests():
            # The objective's log-based counter (infra/gcp/modules/service/slo.tf): requests that are not the platform's uptime
            # check or the health path. A counter with no points counted none; a service without an objective has no counter
            # (Monitoring answers 404), which is unknown, not zero.
            try:
                values = self.query(f"logging.googleapis.com/user/lab-deploy/{service}/requests", "ALIGN_SUM", "REDUCE_SUM", start, end, whole,
                                    by_service)
            except httpx.HTTPStatusError:
                return None
            return sum(values)

        active = 'metric.labels.state="active"'
        instance_samples = self.query("run.googleapis.com/container/instance_count", "ALIGN_MAX", "REDUCE_SUM", start, end, 300,
                                      f"{by_service} AND {active}")
        return {
            "window_days": window_days, "window_start": _iso(start), "window_end": _iso(end),
            "vcpu_seconds": total("run.googleapis.com/container/cpu/allocation_time"),
            "gib_seconds": total("run.googleapis.com/container/memory/allocation_time"),
            "billable_instance_seconds": total("run.googleapis.com/container/billable_instance_time"),
            "requests": total("run.googleapis.com/request_count"),
            "user_requests": user_requests(),
            "cpu_utilization_p95": peak("run.googleapis.com/container/cpu/utilizations", "ALIGN_PERCENTILE_95"),
            "memory_utilization_p95": peak("run.googleapis.com/container/memory/utilizations", "ALIGN_PERCENTILE_95"),
            "max_concurrency_p95": peak("run.googleapis.com/container/max_request_concurrencies", "ALIGN_PERCENTILE_95", active),
            "request_latency_ms_p50": peak("run.googleapis.com/request_latencies", "ALIGN_PERCENTILE_50"),
            "request_latency_ms_p95": peak("run.googleapis.com/request_latencies", "ALIGN_PERCENTILE_95"),
            "max_active_instances": max(instance_samples) if instance_samples else None,
        }
