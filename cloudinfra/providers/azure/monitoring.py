"""Health from the provider's state, utilisation from Azure Monitor's platform metrics; read-only.

A machine's Percentage CPU is a share of all its vCPUs, sampled each minute with no agent
(learn.microsoft.com/azure/azure-monitor/reference/supported-metrics/microsoft-compute-virtualmachines-metrics); memory
and disk use need the Azure Monitor agent, which the service does not install. One machine's metrics are an ARM read
(learn.microsoft.com/rest/api/monitor/metrics/list), counted against the subscription's read budget like any other.
"""
import datetime

from ...models.resources import ResourceStatus
from ...models.response import Health, Usage
from .api import API_VERSIONS

VERSION = API_VERSIONS["Microsoft.Insights"]
CPU = "Percentage CPU"


def _iso(moment) -> str:
    return moment.astimezone(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class AzureMonitoring:
    def __init__(self, api, clock=None):
        self.api = api
        self.clock = clock or (lambda: datetime.datetime.now(datetime.UTC))

    def _points(self, resource, minutes: int, aggregation: str) -> list[float]:
        end = self.clock()
        start = end - datetime.timedelta(minutes=minutes)
        data = self.api.get(self.api.arm(f"{resource.provider_id}/providers/Microsoft.Insights/metrics"), VERSION,
                            params={"metricnames": CPU, "timespan": f"{_iso(start)}/{_iso(end)}", "interval": "PT1M", "aggregation": aggregation})
        key = aggregation.lower()
        return [float(point[key]) for metric in data.get("value") or [] for series in metric.get("timeseries") or [] for point in series.get("data") or []
                if point.get(key) is not None]

    def health(self, resource) -> Health:
        ready = resource.status == ResourceStatus.READY
        healthy = None if resource.status in (ResourceStatus.UNKNOWN, ResourceStatus.PENDING, ResourceStatus.CREATING) else ready
        detail = {"provider_status": resource.status.value}
        if resource.kind.value == "compute":
            detail.update({"internal_ip": resource.internal_ip, "public_ip": resource.public_ip})
        return Health(resource_id=resource.id, kind=resource.kind.value, status=resource.status.value, healthy=healthy, detail=detail)

    def busy_cores(self, resource, minutes: int) -> float | None:
        """The busiest minute of a machine in the window, in cores; None when nothing was recorded."""
        values = self._points(resource, minutes, "Maximum")
        return round(max(values) / 100 * resource.cpu, 3) if values else None

    def usage(self, resource, minutes: int) -> Usage:
        kind = resource.kind.value
        if kind == "compute":
            means, peaks = self._points(resource, minutes, "Average"), self._points(resource, minutes, "Maximum")
            metrics = {"cpu_utilization_mean": round(sum(means) / len(means) / 100, 4) if means else None,
                       "cpu_utilization_max": round(max(peaks) / 100, 4) if peaks else None}
            note = "CPU utilisation of the machine from Azure Monitor; memory and disk need the Azure Monitor agent, which this service does not install."
        else:
            metrics, note = {}, f"No utilisation metric is defined for a {kind} resource; see get_resource_health for its state."
        return Usage(resource_id=resource.id, kind=kind, window_minutes=minutes, metrics=metrics, note=note)
