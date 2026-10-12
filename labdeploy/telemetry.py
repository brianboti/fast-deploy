"""Read-only Cloud Monitoring queries for right-sizing. Runs on the deployment runner only."""
import datetime
import re

import httpx

RESOURCE = 'resource.type="cloud_run_revision"'
BASE = "https://monitoring.googleapis.com/v3"


def _iso(moment):
    return moment.astimezone(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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

        active = 'metric.labels.state="active"'
        instance_samples = self.query("run.googleapis.com/container/instance_count", "ALIGN_MAX", "REDUCE_SUM", start, end, 300,
                                      f"{by_service} AND {active}")
        return {
            "window_days": window_days, "window_start": _iso(start), "window_end": _iso(end),
            "vcpu_seconds": total("run.googleapis.com/container/cpu/allocation_time"),
            "gib_seconds": total("run.googleapis.com/container/memory/allocation_time"),
            "billable_instance_seconds": total("run.googleapis.com/container/billable_instance_time"),
            "requests": total("run.googleapis.com/request_count"),
            "cpu_utilization_p95": peak("run.googleapis.com/container/cpu/utilizations", "ALIGN_PERCENTILE_95"),
            "memory_utilization_p95": peak("run.googleapis.com/container/memory/utilizations", "ALIGN_PERCENTILE_95"),
            "max_concurrency_p95": peak("run.googleapis.com/container/max_request_concurrencies", "ALIGN_PERCENTILE_95", active),
            "request_latency_ms_p50": peak("run.googleapis.com/request_latencies", "ALIGN_PERCENTILE_50"),
            "request_latency_ms_p95": peak("run.googleapis.com/request_latencies", "ALIGN_PERCENTILE_95"),
            "max_active_instances": max(instance_samples) if instance_samples else None,
        }
