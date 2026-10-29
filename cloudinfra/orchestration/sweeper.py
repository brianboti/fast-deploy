"""The sweeper: the service's own clock. Every sweep_interval_seconds it looks at READY and STOPPED requests, and at failed
ones that have a lifetime, and

- records a warning 48 and 2 hours before a request expires (in the audit log, and in every answer about the request);
- at expiry, stops the request's machines and databases and keeps its data for limits.expiry_retention_days, or destroys
  it when the request asked for on_expiry: destroy or the operator keeps nothing; while such a destroy is refused (a network
  another request still uses), what can stop is stopped meanwhile; a failed request is destroyed at expiry;
- destroys a request kept at expiry once its retention has passed, unless it was extended;
- stops a machine that has been idle for its autostop window: its busiest CPU sample in the window used fewer cores than
  limits.idle_cpu_cores, at least one window after it last started, and not while keep_awake holds.

The sweeper decides under the request's lock; what it starts runs in the workers, as a caller's work does, so a sweep
reports what it started, and the outcome is in the request's operations. An attempt that fails is tried again after a wait
that doubles each time (5 minutes, at most a day), so a lasting failure adds a failed operation a day rather than one at
every sweep. Nothing expires or stops by itself unless a request or the operator asked for it. The sweeper acts as its own
principal, which no token maps to, and every action it takes is an operation and an audit event like any other."""
import datetime
import threading

from ..config import Principal
from ..models.errors import ErrorCode, InfrastructureError
from ..models.resources import ResourceKind, ResourceStatus
from ..models.response import RequestStatus
from ..observability.logging import bound, event
from . import lifecycle

SWEEPER = Principal(name="cloudinfra-sweeper", token_sha256="0" * 64, can_provision=True, can_destroy=True, scope="all")
WATCHED = (ResourceStatus.READY, ResourceStatus.UNKNOWN, ResourceStatus.UPDATING)  # machines idle stop looks at, after a refresh
IN_USE = (RequestStatus.READY, RequestStatus.STOPPED)
FAILED = (RequestStatus.FAILED, RequestStatus.DESTROY_FAILED)  # swept only to be destroyed at expiry


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
        """One sweep: what it warned about, what it started (stopping, destroying, idle_stopping), the expired requests it
        keeps until their retention ends, and the errors it met."""
        found = {"warned": [], "stopping": [], "destroying": [], "kept": [], "idle_stopping": [], "errors": []}
        orchestrator = self.orchestrator
        for listed in orchestrator.state.sweepable_requests():
            if self._halt.is_set():  # shutting down: leave the rest to the next start
                break
            request_id = listed["request_id"]
            with bound(request_id=request_id, identity=SWEEPER.name), orchestrator.lock(request_id):
                # decide on the request as it is now, not as it was when the sweep began: a caller may have extended it,
                # kept it awake or stopped it in between, and holding its lock keeps it so while the sweeper decides
                record = orchestrator.state.get_request(request_id)
                if record is None or record["status"] not in (*IN_USE, *FAILED):
                    continue
                now = orchestrator.wallclock()
                try:
                    if not self._expiry(record, now, found) and record["status"] == RequestStatus.READY:
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
            if due and record["status"] in IN_USE:
                life["warnings_sent"] = [*life.get("warnings_sent", []), *due]
                state.set_lifecycle(request_id, life)
                event("expiry_warning", owner=record["owner"], expires_at=life["expires_at"], hours_left=round(hours, 1), warning=due[-1], on_expiry=life.get("on_expiry"))
                found["warned"].append(request_id)
            return False
        if not lifecycle.retry_due(life.get("expiry_attempts", 0), life.get("expiry_attempted_at"), now):
            return True  # the last attempt failed a short while ago
        retention = self.orchestrator.config.limits.expiry_retention_days
        retain = lifecycle.parse(life.get("retain_until"))
        ended = retain is not None and now >= retain
        if record["status"] in FAILED or lifecycle.destroys_at_expiry(life.get("on_expiry"), retention) or ended:
            self._attempt(request_id, life, now)
            try:
                self._destroy(request_id, "retention ended" if ended else "expired", found)
            except InfrastructureError as exc:
                if exc.code == ErrorCode.INVALID_STATE and record["status"] == RequestStatus.READY:
                    # refused, such as for a network that another request's machines still use: stop what can stop, so that
                    # nothing runs while the destroy waits to be tried again
                    try:
                        self._stop(record, life, found)
                    except InfrastructureError:
                        pass  # the refused destroy is the error to report
                raise
            return True
        if retain is not None:
            return True  # kept until its retention ends
        if record["status"] == RequestStatus.READY:
            self._attempt(request_id, life, now)
            if self._stop(record, life, found):
                return True
        # already stopped, or nothing in it can stop: kept for the retention period, then destroyed; nothing is recorded as
        # stopped that is not
        life = dict(state.get_request(request_id)["lifecycle"] or {})
        life["retain_until"] = lifecycle.iso(now + datetime.timedelta(days=retention))
        for key in ("expiry_attempts", "expiry_attempted_at"):
            life.pop(key, None)
        state.set_lifecycle(request_id, life)
        event("request_expired", action="keep", expires_at=life["expires_at"], retain_until=life["retain_until"])
        found["kept"].append(request_id)
        return True

    def _attempt(self, request_id: str, life: dict, now: datetime.datetime) -> None:
        """Count an attempt at what expiry does; if it fails, the next one waits longer (lifecycle.retry_due). A stop that
        completes at expiry, or an extension, clears the count."""
        life.update(expiry_attempts=life.get("expiry_attempts", 0) + 1, expiry_attempted_at=lifecycle.iso(now))
        self.orchestrator.state.set_lifecycle(request_id, life)

    def _stop(self, record: dict, life: dict, found: dict) -> bool:
        """Stop an expired request's machines and databases; the stop keeps the data for the retention period once it
        completes. False when nothing in it can stop."""
        try:
            self.orchestrator.stop(record["request_id"], SWEEPER, reason="expired", expired_at=life["expires_at"])
        except InfrastructureError as exc:
            if exc.details.get("reason") == "nothing_to_stop":
                return False
            raise
        event("request_expired", action="stop", expires_at=life["expires_at"])
        found["stopping"].append(record["request_id"])
        return True

    def _destroy(self, request_id: str, reason: str, found: dict) -> None:
        self.orchestrator.destroy(request_id, SWEEPER)
        event("request_expired", action="destroy", reason=reason)
        found["destroying"].append(request_id)

    def _idle(self, record: dict, now: datetime.datetime, found: dict) -> None:
        """Hand the idle machines of a READY request to the workers to stop. A machine whose refresh or measurement fails is
        reported and passed over, never the others; one whose last stops failed waits, as expiry does."""
        awake = lifecycle.parse((record["lifecycle"] or {}).get("awake_until"))
        if awake is not None and now < awake:
            return
        orchestrator, request_id = self.orchestrator, record["request_id"]
        provider = orchestrator.registry.get(record["provider"])
        threshold = orchestrator.config.limits.idle_cpu_cores
        attempts = dict((record["lifecycle"] or {}).get("idle_attempts") or {})
        changing = {item.resource_id for item in orchestrator.state.running_operations_of(request_id)}
        idle = []
        for resource in orchestrator.state.resources_of(request_id):
            if resource.kind != ResourceKind.compute or not resource.autostop_idle_minutes or resource.status not in WATCHED or resource.id in changing:
                continue
            tried, at = attempts.get(resource.id) or (0, None)
            if not lifecycle.retry_due(tried, at, now):
                continue
            try:
                resource = orchestrator._refresh(record, resource)  # one failed refresh must not hide a machine from idle stop for good
                if resource.status != ResourceStatus.READY:
                    continue
                window = resource.autostop_idle_minutes
                started = lifecycle.parse(resource.started_at) or lifecycle.parse(resource.created_at)
                if started is not None and now - started < datetime.timedelta(minutes=window):
                    continue
                cores = provider.busy_cores(resource, window)
            except InfrastructureError as exc:
                found["errors"].append({"request_id": request_id, "resource_id": resource.id, "error": exc.detail().model_dump(mode="json")})
                continue
            if cores is None or cores >= threshold:  # no samples is not proof of idleness
                continue
            attempts[resource.id] = [tried + 1, lifecycle.iso(now)]
            idle.append(resource)
            event("machine_idle", resource_id=resource.id, busy_cores=cores, window_minutes=window, threshold_cores=threshold)
        if idle:
            life = dict(orchestrator.state.get_request(request_id)["lifecycle"] or {})  # as a warning may have just left it
            life["idle_attempts"] = attempts
            orchestrator.state.set_lifecycle(request_id, life)
            orchestrator.stop_idle(record, idle, SWEEPER)
            found["idle_stopping"].extend(item.id for item in idle)
