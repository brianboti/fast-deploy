"""Health from the provider's state, utilisation from Cloud Monitoring; read-only."""
import datetime

from ...models.resources import ResourceStatus
from ...models.response import Health, Usage
from .api import MONITORING


def _iso(moment):
    return moment.astimezone(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _cpu(resource) -> str:
    """The CPU utilisation series of one machine: by its numeric id, which no other machine has, or, before the id is known,
    by its name and zone, since names are unique within a zone only."""
    base = 'metric.type="compute.googleapis.com/instance/cpu/utilization" AND resource.type="gce_instance"'
    if resource.instance_id:
        return f'{base} AND resource.labels.instance_id="{resource.instance_id}"'
    return f'{base} AND metric.labels.instance_name="{resource.name}" AND resource.labels.zone="{resource.zone}"'


class GcpMonitoring:
    def __init__(self, api, clock=None):
        self.api = api
        self.clock = clock or (lambda: datetime.datetime.now(datetime.UTC))

    def _series(self, condition, minutes, aligner, busiest=False):
        """One number for a window: the mean of the points, or with busiest the largest (for idle detection)."""
        end = self.clock()
        start = end - datetime.timedelta(minutes=minutes)
        params = {"filter": condition, "interval.startTime": _iso(start), "interval.endTime": _iso(end), "aggregation.alignmentPeriod": f"{minutes * 60}s",
                  "aggregation.perSeriesAligner": aligner, "aggregation.crossSeriesReducer": "REDUCE_MAX" if busiest else "REDUCE_MEAN", "view": "FULL"}
        data = self.api.get(f"{MONITORING}/projects/{self.api.project}/timeSeries", **params)
        values = [float(point["value"].get("doubleValue", point["value"].get("int64Value", 0))) for series in data.get("timeSeries", []) for point in series.get("points", [])]
        if not values:
            return None
        return round(max(values) if busiest else sum(values) / len(values), 4)

    def health(self, resource) -> Health:
        ready = resource.status == ResourceStatus.READY
        healthy = None if resource.status in (ResourceStatus.UNKNOWN, ResourceStatus.PENDING, ResourceStatus.CREATING) else ready
        detail = {"provider_status": resource.status.value}
        if resource.kind.value == "compute":
            detail.update({"internal_ip": resource.internal_ip, "public_ip": resource.public_ip})
        if resource.kind.value == "database":
            detail.update({"host": resource.host, "port": resource.port})
        return Health(resource_id=resource.id, kind=resource.kind.value, status=resource.status.value, healthy=healthy, detail=detail)

    def busy_cores(self, resource, minutes: int) -> float | None:
        """The busiest CPU sample of a machine in the window, in cores: Compute Engine reports utilisation as a share of all
        vCPUs, so one busy core on a large machine is a small share. None when nothing was recorded."""
        busiest = self._series(_cpu(resource), minutes, "ALIGN_MAX", busiest=True)
        return None if busiest is None else round(busiest * resource.cpu, 3)

    def usage(self, resource, minutes: int) -> Usage:
        kind = resource.kind.value
        if kind == "compute":
            condition = _cpu(resource)
            metrics = {"cpu_utilization_mean": self._series(condition, minutes, "ALIGN_MEAN"), "cpu_utilization_max": self._series(condition, minutes, "ALIGN_MAX")}
            note = "CPU utilisation of the machine from Cloud Monitoring; memory and disk need the guest agent, which this service does not install."
        elif kind == "database":
            base = f'resource.type="cloudsql_database" AND resource.labels.database_id="{self.api.project}:{resource.name}"'
            metrics = {name: self._series(f'metric.type="cloudsql.googleapis.com/database/{metric}" AND {base}', minutes, "ALIGN_MEAN")
                       for name, metric in (("cpu_utilization_mean", "cpu/utilization"), ("memory_utilization_mean", "memory/utilization"), ("disk_utilization_mean", "disk/utilization"))}
            note = "Cloud SQL utilisation from Cloud Monitoring."
        else:
            metrics, note = {}, f"No utilisation metric is defined for a {kind} resource; see get_resource_health for its state."
        return Usage(resource_id=resource.id, kind=kind, window_minutes=minutes, metrics=metrics, note=note)
