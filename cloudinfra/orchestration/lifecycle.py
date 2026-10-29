"""Time on a request: when it expires, how close that is, and what keeps billing while it is stopped.

A request's lifecycle is a small record kept beside it: expires_at, on_expiry, max_expires_at, stopped_at, stop_reason
(requested, expired or idle), retain_until, awake_until and the warnings already sent. Nothing here calls a provider."""
import datetime

from ..models.resources import ResourceKind, ResourceStatus
from ..models.response import Lifecycle

WARNINGS = (("48h", 48), ("2h", 2))  # labels and hours before expiry


def iso(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse(value: str | None) -> datetime.datetime | None:
    """A timestamp from this service or the provider; one without an offset is taken as UTC."""
    if not value:
        return None
    found = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return found if found.tzinfo is not None else found.replace(tzinfo=datetime.UTC)


def lifetime_hours(request, limits) -> int | None:
    """How long a request lives: its own lifetime, else the operator's default, else the operator's maximum, else forever."""
    if request.lifetime:
        return request.lifetime.hours
    return limits.default_lifetime_hours or limits.max_lifetime_hours


def initial(request, limits, now: datetime.datetime) -> dict:
    """The lifecycle a new request starts with: none unless the request or the operator asks for an expiry."""
    hours = lifetime_hours(request, limits)
    if not hours:
        return {}
    found = {"expires_at": iso(now + datetime.timedelta(hours=hours)), "on_expiry": request.lifetime.on_expiry if request.lifetime else "stop", "warnings_sent": []}
    if limits.max_lifetime_hours:
        found["max_expires_at"] = iso(now + datetime.timedelta(hours=limits.max_lifetime_hours))
    return found


def still_billing(resources) -> list[str]:
    """While stopped: what keeps costing money, by name."""
    found = []
    for resource in resources:
        if resource.status in (ResourceStatus.DELETED, ResourceStatus.PENDING, ResourceStatus.FAILED):
            continue
        if resource.kind == ResourceKind.compute and resource.status == ResourceStatus.STOPPED:
            found.append(f"{resource.name}: its {resource.boot_disk_gb} GB boot disk")
        elif resource.kind == ResourceKind.database and resource.status == ResourceStatus.STOPPED:
            found.append(f"{resource.name}: its {resource.storage_gb} GB of storage and its address")
        elif resource.kind == ResourceKind.storage and resource.type == "persistent":
            found.append(f"{resource.name}: a {resource.size_gb} GB disk")
        elif resource.kind == ResourceKind.storage:
            found.append(f"{resource.name}: what the bucket holds")
        elif resource.kind in (ResourceKind.cache, ResourceKind.cluster, ResourceKind.load_balancer, ResourceKind.secret):
            found.append(f"{resource.name}: a {resource.kind.value.replace('_', ' ')} cannot stop")
    return found


def view(record: dict, resources, now: datetime.datetime) -> Lifecycle | None:
    lifecycle = {key: value for key, value in (record.get("lifecycle") or {}).items() if value}
    stopped_any = any(resource.status == ResourceStatus.STOPPED for resource in resources)
    if not lifecycle and not stopped_any:
        return None
    expires = parse(lifecycle.get("expires_at"))
    return Lifecycle(expires_at=lifecycle.get("expires_at"), on_expiry=lifecycle.get("on_expiry"),
                     expires_in_hours=round((expires - now).total_seconds() / 3600, 1) if expires else None, max_expires_at=lifecycle.get("max_expires_at"),
                     stopped_at=lifecycle.get("stopped_at"), stop_reason=lifecycle.get("stop_reason"), retain_until=lifecycle.get("retain_until"),
                     awake_until=lifecycle.get("awake_until"), warnings_sent=lifecycle.get("warnings_sent", []),
                     still_billing=still_billing(resources) if stopped_any else [])


def then(on_expiry: str | None, retention_days: int) -> str:
    """What happens at expiry, as the sweeper does it."""
    if on_expiry == "destroy" or retention_days == 0:
        return "it is destroyed"
    return f"it stops, and is destroyed {retention_days} days later"


def warnings(record: dict, now: datetime.datetime, retention_days: int) -> list[str]:
    """Sentences for the answer: an expiry within two days, an expiry passed, and what happens then."""
    lifecycle = record.get("lifecycle") or {}
    expires = parse(lifecycle.get("expires_at"))
    found = []
    if expires and record["status"].value in ("READY", "STOPPED"):
        hours = (expires - now).total_seconds() / 3600
        extend = "unless control_infrastructure extend adds hours"
        if hours <= 0 and lifecycle.get("retain_until"):
            found.append(f"Expired at {lifecycle['expires_at']}; destroyed at {lifecycle['retain_until']} {extend}")
        elif hours <= 0:
            found.append(f"Expired at {lifecycle['expires_at']}; at the service's next check {then(lifecycle.get('on_expiry'), retention_days)}, {extend}")
        elif hours <= 48:
            found.append(f"Expires in {hours:.1f} hours, at {lifecycle['expires_at']}; then {then(lifecycle.get('on_expiry'), retention_days)}, {extend}")
    return found
