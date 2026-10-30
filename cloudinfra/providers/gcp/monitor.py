"""Monitoring behind the generic monitor primitives (Cloud Monitoring): an email notification channel per address, an
uptime check per public endpoint of the request, and an alert policy per check that fires when the check fails from more
than one place, as Google's uptime alert does (https://docs.cloud.google.com/monitoring/uptime-checks/uptime-alerting-policies).
Endpoints are the request's own: its http load balancer, a cluster's front door or a public service, HTTPS when they have
a name. Deleting goes in reverse: policies, checks, channels."""
from urllib.parse import urlparse

from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import MonitorResource, ResourceStatus
from . import translate
from .api import MONITORING


def endpoint(check, created) -> tuple[str, bool, int]:
    """(host, https, port) of the endpoint a check names, from what the request made."""
    kind, _, name = check.to.partition(":")
    if kind == "load_balancer":
        found = next((item for item in created if item.kind.value == "load_balancer"), None)
        if found is not None and found.hostnames:
            return found.hostnames[0], True, 443
        return (found.address if found else None), False, (found.port if found else 80)
    if kind == "cluster":
        found = next((item for item in created if item.kind.value == "cluster" and item.public is not None and (not name or item.name.endswith(f"-{name}"))), None)
        door = found.public if found else None
        if door is not None and door.hostnames:
            return door.hostnames[0], True, 443
        return (door.address if door else None), False, 80
    found = next((item for item in created if item.kind.value == "service" and item.name.endswith(f"-{name}")), None)
    return (urlparse(found.url).hostname if found and found.url else None), True, 443


class GcpMonitor:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def _base(self, collection: str) -> str:
        return f"{MONITORING}/projects/{self.api.project}/{collection}"

    def create(self, spec, planned, ctx, created) -> MonitorResource:
        resource = MonitorResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region,
                                   provider_id=f"projects/{self.api.project}", status=ResourceStatus.CREATING, notify=list(spec.notify), labels=translate.labels(ctx))
        labels = translate.labels(ctx)
        for address in spec.notify:
            made = self.api.post(self._base("notificationChannels"), json={"type": "email", "displayName": f"{planned.name} {address}",
                                                                            "labels": {"email_address": address}, "userLabels": labels})
            resource = resource.model_copy(update={"channels": [*resource.channels, made["name"]]})
            ctx.accepted(resource)  # each piece is known as Google takes it, so a failure further on deletes every one made
        for index, check in enumerate(spec.uptime, start=1):
            host, https, port = endpoint(check, created)
            if not host:
                raise InfrastructureError(ErrorCode.INVALID_STATE, f"monitor.uptime: {check.to} has no public address yet", {"to": check.to})
            made = self.api.post(self._base("uptimeCheckConfigs"), json={
                "displayName": f"{planned.name}-{index}", "period": f"{check.period_seconds}s", "timeout": "10s",
                "monitoredResource": {"type": "uptime_url", "labels": {"project_id": self.api.project, "host": host}},
                "httpCheck": {"path": check.path, "port": port, "useSsl": https, "validateSsl": https}, "userLabels": labels})
            url = f"{'https' if https else 'http'}://{host}{'' if port in (80, 443) else f':{port}'}{check.path}"
            resource = resource.model_copy(update={"checks": [*resource.checks, {"name": made["name"], "url": url, "period_seconds": check.period_seconds}]})
            ctx.accepted(resource)
            check_id = made["name"].rsplit("/", 1)[-1]
            policy = self.api.post(self._base("alertPolicies"), json={
                "displayName": f"{planned.name}-{index}: {url} is down", "combiner": "OR", "notificationChannels": list(resource.channels), "userLabels": labels,
                "conditions": [{"displayName": f"Uptime check failed: {url}", "conditionThreshold": {
                    "filter": f'metric.type="monitoring.googleapis.com/uptime_check/check_passed" AND metric.label.check_id="{check_id}" AND resource.type="uptime_url"',
                    "aggregations": [{"alignmentPeriod": "1200s", "perSeriesAligner": "ALIGN_NEXT_OLDER", "crossSeriesReducer": "REDUCE_COUNT_FALSE",
                                      "groupByFields": ["resource.label.*"]}],
                    "comparison": "COMPARISON_GT", "thresholdValue": 1, "duration": "60s", "trigger": {"count": 1}}}]})
            resource = resource.model_copy(update={"policies": [*resource.policies, policy["name"]]})
            ctx.accepted(resource)
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def get(self, resource: MonitorResource) -> MonitorResource:
        for name in resource.checks[:1]:
            try:
                self.api.get(f"{MONITORING}/{name['name']}")
            except InfrastructureError as exc:
                if exc.code == ErrorCode.NOT_FOUND:
                    return resource.model_copy(update={"status": ResourceStatus.DELETED})
                raise
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def delete(self, resource: MonitorResource, ctx) -> None:
        for name in [*resource.policies, *(item["name"] for item in resource.checks), *resource.channels]:
            try:
                self.api.delete(f"{MONITORING}/{name}")
            except InfrastructureError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
