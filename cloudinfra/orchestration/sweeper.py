"""The sweeper: the service's own clock. Every sweep_interval_seconds it looks at READY and STOPPED requests and

- records a warning 48 and 2 hours before a request expires (in the audit log, and in every answer about the request);
- at expiry, stops the request's machines and databases and keeps its data for limits.expiry_retention_days, or destroys
  it when the request asked for on_expiry: destroy or the operator keeps nothing;
- destroys a request stopped at expiry once its retention has passed, unless it was extended;
- stops a machine that has been idle for its autostop window: its busiest CPU sample in the window used fewer cores than
  limits.idle_cpu_cores, at least one window after it last started, and not while keep_awake holds.

Nothing expires or stops by itself unless a request or the operator asked for it. The sweeper acts as its own principal,
which no token maps to, and every action it takes is an operation and an audit event like any other."""
import datetime
import threading

from ..config import Principal
from ..models.errors import InfrastructureError
from ..models.resources import ResourceKind, ResourceStatus
from ..models.response import RequestStatus
from ..observability.logging import bound, event
from . import lifecycle

SWEEPER = Principal(name="cloudinfra-sweeper", token_sha256="0" * 64, can_provision=True, can_destroy=True, scope="all")
WATCHED = (ResourceStatus.READY, ResourceStatus.UNKNOWN, ResourceStatus.UPDATING)  # machines idle stop looks at, after a refresh


class Sweeper:
    def __init__(self, orchestrator, interval_seconds: int):
        self.orchestrator, self.interval = orchestrator, interval_seconds
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- the background thread
    def start(self) -> None:
        if self.interval <= 0 or self._thread is not None:
            return
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, name="cloudinfra-sweeper", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._halt.wait(self.interval):  # the first sweep comes one interval after start
            try:
                self.tick()
            except Exception as exc:  # a sweep that fails is logged and retried at the next interval
                event("sweep_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")

    # ---- one sweep
    def tick(self) -> dict:
        found = {"warned": [], "stopped": [], "destroyed": [], "idle_stopped": [], "errors": []}
        orchestrator = self.orchestrator
        for listed in orchestrator.state.sweepable_requests():
            if self._halt.is_set():  # shutting down: leave the rest to the next start
                break
            request_id = listed["request_id"]
            with bound(request_id=request_id, identity=SWEEPER.name), orchestrator.lock(request_id):
                # decide on the request as it is now, not as it was when the sweep began: a caller may have extended it,
                # kept it awake or stopped it in between, and holding its lock keeps it so while the sweeper acts
                record = orchestrator.state.get_request(request_id)
                if record is None or record["status"] not in (RequestStatus.READY, RequestStatus.STOPPED):
                    continue
                now = orchestrator.wallclock()
                try:
                    if not self._expiry(record, now, found):
                        self._idle(record, now, found)
                except InfrastructureError as exc:
                    found["errors"].append({"request_id": request_id, "error": exc.detail().model_dump(mode="json")})
                    event("sweep_request_failed", error=exc.detail().model_dump(mode="json"))
        if any(found.values()):
            event("sweep_completed", **{key: value for key, value in found.items() if value})
        return found

    def _expiry(self, record: dict, now: datetime.datetime, found: dict) -> bool:
        """Warnings and expiry; True when the request expired, so idle stop does not look at it."""
        state, request_id = self.orchestrator.state, record["request_id"]
        life = dict(record["lifecycle"] or {})
        expires = lifecycle.parse(life.get("expires_at"))
        if expires is None:
            return False
        if now < expires:
            hours = (expires - now).total_seconds() / 3600
            due = [label for label, limit in lifecycle.WARNINGS if hours <= limit and label not in life.get("warnings_sent", [])]
            if due:
                life["warnings_sent"] = [*life.get("warnings_sent", []), *due]
                state.set_lifecycle(request_id, life)
                event("expiry_warning", owner=record["owner"], expires_at=life["expires_at"], hours_left=round(hours, 1), warning=due[-1], on_expiry=life.get("on_expiry"))
                found["warned"].append(request_id)
            return False
        retention = self.orchestrator.config.limits.expiry_retention_days
        if life.get("on_expiry") == "destroy" or retention == 0:
            self._destroy(request_id, "expired", found)
            return True
        retain = lifecycle.parse(life.get("retain_until"))
        if retain is not None:
            if now >= retain:
                self._destroy(request_id, "retention ended", found)
            return True
        # The first sweep past expiry (or past a new expiry after an extension): stop what can stop and keep the data; the
        # stop sets the retention when it completes. A resource in the middle of a change, or a provider error, is left to
        # the next sweep: nothing is marked stopped while something may still run.
        if record["status"] == RequestStatus.READY:
            try:
                self.orchestrator.stop(request_id, SWEEPER, reason="expired", expired_at=life["expires_at"])
            except InfrastructureError as exc:
                if exc.details.get("reason") != "nothing_to_stop":
                    raise
            else:
                event("request_expired", action="stop", expires_at=life["expires_at"])
                found["stopped"].append(request_id)
                return True
        # already stopped, or nothing in it can stop: mark it, and destroy it when the retention ends
        retain_until = lifecycle.iso(now + datetime.timedelta(days=retention))
        life.update({"stop_reason": "expired", "stopped_at": life.get("stopped_at") or lifecycle.iso(now), "retain_until": retain_until})
        state.set_lifecycle(request_id, life)
        event("request_expired", action="stop", expires_at=life["expires_at"], retain_until=retain_until)
        found["stopped"].append(request_id)
        return True

    def _destroy(self, request_id: str, reason: str, found: dict) -> None:
        self.orchestrator.destroy(request_id, SWEEPER)
        event("request_expired", action="destroy", reason=reason)
        found["destroyed"].append(request_id)

    def _idle(self, record: dict, now: datetime.datetime, found: dict) -> None:
        if record["status"] != RequestStatus.READY:
            return
        awake = lifecycle.parse((record["lifecycle"] or {}).get("awake_until"))
        if awake is not None and now < awake:
            return
        orchestrator = self.orchestrator
        provider = orchestrator.registry.get(record["provider"])
        threshold = orchestrator.config.limits.idle_cpu_cores
        for resource in orchestrator.state.resources_of(record["request_id"]):
            if resource.kind != ResourceKind.compute or not resource.autostop_idle_minutes or resource.status not in WATCHED:
                continue
            resource = orchestrator._refresh(record, resource)  # one failed refresh must not hide a machine from idle stop for good
            if resource.status != ResourceStatus.READY:
                continue
            window = resource.autostop_idle_minutes
            started = lifecycle.parse(resource.started_at) or lifecycle.parse(resource.created_at)
            if started is not None and now - started < datetime.timedelta(minutes=window):
                continue
            busy = provider.busy_cores(resource, window)
            if busy is None or busy >= threshold:  # no samples is not proof of idleness
                continue
            orchestrator.stop_resource(record, resource, "idle", SWEEPER)
            event("machine_idle", resource_id=resource.id, busy_cores=busy, window_minutes=window, threshold_cores=threshold)
            found["idle_stopped"].append(resource.id)
