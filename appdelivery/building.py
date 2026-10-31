"""Builds: a team asks for one, this service orders it from Cloud Build in the trusted project and follows it to its end,
and the digest Cloud Build reports is recorded. A release may name only an image recorded here, so an image built or
pushed anywhere else never runs (docs/research/RESEARCH-2026-10-30-trusted-build.md).

The build runs in Cloud Build, not in this service. A follower polls it from a pool of its own, so a long build never
holds a worker a rollout needs; a restart resumes following, since the build carries on in Cloud Build meanwhile."""
import concurrent.futures
import datetime
import threading
import time

from . import cloudbuild
from .errors import DeliveryError, ErrorCode
from .events import bound, event
from .models import BuildRequest, BuildResponse
from .state import StateManager, new_build_id

MAX_WAIT_SECONDS = 45
POLL_SECONDS = 0.5  # waiting for the record, within a call
FOLLOW_SECONDS = 10  # asking Cloud Build, in the follower
GRACE_SECONDS = 600  # past the build's own timeout, after which the follower stops asking (get_build still does)
MAX_MISSES = 6  # consecutive failed reads of a build before the follower gives up until get_build or a restart
MOVING = ("QUEUED", "WORKING")


class Builder:
    def __init__(self, config, state: StateManager, client, executor=None, sleep=time.sleep, clock=time.monotonic):
        self.config, self.state, self.client = config, state, client
        self.executor = executor or concurrent.futures.ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix="appdelivery-build")
        self.sleep, self.clock = sleep, clock
        self.stopping = threading.Event()
        self.ordering: dict[tuple[str, str], threading.Lock] = {}  # per team and environment: its budget is counted and its build ordered under it
        self.ordering_guard = threading.Lock()

    # ---- helpers
    def authorize(self, request: BuildRequest, principal):
        """(environment, source) a principal may build from, or the reason it may not."""
        if request.environment not in self.config.environments:
            raise DeliveryError(ErrorCode.UNKNOWN_ENVIRONMENT, f"No environment {request.environment!r}; environments: {', '.join(sorted(self.config.environments))}")
        if not principal.can_build:
            raise DeliveryError(ErrorCode.PERMISSION_DENIED, f"{principal.name} may not build", {"identity": principal.name})
        if request.environment not in principal.environments:
            raise DeliveryError(ErrorCode.PERMISSION_DENIED, f"{principal.name} may not build in {request.environment}",
                                {"identity": principal.name, "environments": principal.environments})
        environment = self.config.environments[request.environment]
        if environment.build is None:
            raise DeliveryError(ErrorCode.INVALID_BUILD, f"Environment {request.environment} builds nothing: the operator gave it no build settings",
                                {"environment": request.environment})
        source = self.config.sources.get(request.source)
        if source is None or source.infra_owner != principal.infra_owner:
            # another team's source is not found either: which sources exist is not this caller's to learn
            raise DeliveryError(ErrorCode.NOT_FOUND, f"No source {request.source} for {principal.infra_owner}; the operator links a repository once and names it "
                                "under sources", {"source": request.source})
        path = cloudbuild.image_path(source, request)
        if not environment.allows(path):
            raise DeliveryError(ErrorCode.REGISTRY_NOT_ALLOWED, f"{path} is not under the registries allowed in {request.environment}: "
                                f"{', '.join(environment.allowed_registries)}", {"field": "image"})
        return environment, source

    def _ordering(self, infra_owner: str, environment: str) -> threading.Lock:
        """The lock one team's builds in one environment are ordered under: the budget is that team's, so another team's
        order, or a slow answer from Cloud Build to it, never waits on this one."""
        with self.ordering_guard:
            return self.ordering.setdefault((infra_owner, environment), threading.Lock())

    def budget(self, request: BuildRequest, principal, settings) -> None:
        """A team's builds in an environment are bounded, at once and per day (the review of 2026-10-30): one team, or one
        stolen token, cannot fill Cloud Build's pool for every other team or run up the operator's bill."""
        moving = self.state.count_builds(principal.infra_owner, request.environment, MOVING)
        if moving >= settings.max_concurrent_builds:
            raise DeliveryError(ErrorCode.BUILD_LIMIT_EXCEEDED, f"{principal.infra_owner} already has {moving} builds queued or working in {request.environment}; at most "
                                f"{settings.max_concurrent_builds} at once. Wait for one to end, or ask the operator for more",
                                {"environment": request.environment, "moving": moving, "max_concurrent_builds": settings.max_concurrent_builds})
        since = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        today = self.state.count_builds(principal.infra_owner, request.environment, since=since)
        if today >= settings.max_builds_per_day:
            raise DeliveryError(ErrorCode.BUILD_LIMIT_EXCEEDED, f"{principal.infra_owner} ordered {today} builds in {request.environment} in the last 24 hours; at most "
                                f"{settings.max_builds_per_day} a day", {"environment": request.environment, "today": today, "max_builds_per_day": settings.max_builds_per_day})

    def _record(self, build_id: str, principal) -> dict:
        record = self.state.get_build(build_id)
        if record is None or record["infra_owner"] != principal.infra_owner:
            raise DeliveryError(ErrorCode.NOT_FOUND, f"No build {build_id}", {"build_id": build_id})
        return record

    def _where(self, record: dict):
        source = self.config.sources.get(record["source"])
        if source is None:
            raise DeliveryError(ErrorCode.NOT_FOUND, f"Source {record['source']} is no longer in the configuration", {"source": record["source"]})
        return source.project, source.region

    @staticmethod
    def view(record: dict) -> BuildResponse:
        request = record["request"]
        return BuildResponse(build_id=record["build_id"], status=record["status"], environment=record["environment"], source=record["source"],
                             revision=record["revision"], dockerfile=request.get("dockerfile", "Dockerfile"), context=request.get("context", "."), image=record["image"],
                             built=record["built"], requested_by=record["requested_by"], note=request.get("note"), cloud_build_id=record["cloud_build_id"],
                             log_url=record["log_url"], error=record["error"], created_at=record["created_at"], updated_at=record["updated_at"])

    # ---- build
    def build(self, request: BuildRequest, principal, wait_seconds: int = 0) -> BuildResponse:
        environment, source = self.authorize(request, principal)
        build_id = new_build_id()
        tag = cloudbuild.tagged(source, request, build_id)
        body = cloudbuild.compose(request, source, environment.build, build_id, principal.infra_owner)
        with bound(identity=principal.name), self._ordering(principal.infra_owner, request.environment):
            self.budget(request, principal, environment.build)
            event("build_received", build_id=build_id, environment=request.environment, source=request.source, revision=request.revision, image=tag)
            created = self.client(request.environment).create(source.project, source.region, body)
            status, _, _ = cloudbuild.outcome(created, tag)
            self.state.create_build(build_id, request.environment, principal.infra_owner, principal.name, request.source, request.revision, tag,
                                    "QUEUED" if status in cloudbuild.FINAL else status, created["id"], created.get("logUrl"),
                                    {**request.model_dump(mode="json"), "composed": cloudbuild.composed(body)})
            event("build_submitted", build_id=build_id, cloud_build_id=created["id"])
        self._follow_later(build_id)
        if wait_seconds:
            deadline = self.clock() + min(max(wait_seconds, 0), MAX_WAIT_SECONDS)
            while self.clock() < deadline and self.state.get_build(build_id)["status"] in MOVING:
                self.sleep(POLL_SECONDS)
        return self.view(self.state.get_build(build_id))

    def _follow_later(self, build_id: str) -> None:
        try:
            self.executor.submit(self._follow, build_id)
        except RuntimeError:
            event("build_not_followed", build_id=build_id, reason="shutting down")  # resumed at the next start; get_build reads it meanwhile

    def refresh(self, record: dict) -> dict:
        """Ask Cloud Build once and record what it says; a final status is kept as it is."""
        if record["status"] not in MOVING:
            return record
        project, region = self._where(record)
        found = self.client(record["environment"]).get(project, region, record["cloud_build_id"])
        status, digest, error = cloudbuild.outcome(found, record["image"])
        if status != record["status"]:
            built = f"{record['image'].rsplit(':', 1)[0]}@{digest}" if digest else None
            if self.state.update_build(record["build_id"], MOVING, status, built=built, log_url=found.get("logUrl"), error=error):
                with bound(identity=record["requested_by"]):
                    event({"SUCCEEDED": "build_succeeded", "FAILED": "build_failed"}.get(status, "build_working"), build_id=record["build_id"],
                          cloud_status=found.get("status"), built=built)
        return self.state.get_build(record["build_id"])

    def _follow(self, build_id: str) -> None:
        """Poll Cloud Build until the build ends, the service stops, or the build is long past its timeout."""
        record = self.state.get_build(build_id)
        settings = self.config.environments[record["environment"]].build
        deadline = self.clock() + (settings.timeout_seconds if settings else 0) + GRACE_SECONDS
        misses = 0
        while record["status"] in MOVING and not self.stopping.is_set():
            try:
                record = self.refresh(record)
                misses = 0
            except DeliveryError as exc:
                misses += 1
                if misses >= MAX_MISSES:
                    event("build_follow_stopped", build_id=build_id, code=exc.code.value, reason="Cloud Build did not answer")
                    return
            except Exception as exc:  # a follower never dies silently; get_build and the next start pick the build up
                event("build_follow_stopped", build_id=build_id, reason=type(exc).__name__)
                return
            if record["status"] not in MOVING:
                return
            if self.clock() > deadline:
                event("build_follow_stopped", build_id=build_id, reason="past its timeout")
                return
            self.sleep(FOLLOW_SECONDS)

    # ---- reading
    def get(self, build_id: str, principal) -> BuildResponse:
        record = self._record(build_id, principal)
        try:
            record = self.refresh(record)
        except DeliveryError as exc:  # what is recorded is still the answer; the read is retried on the next call
            event("build_refresh_failed", build_id=build_id, code=exc.code.value)
        return self.view(record)

    def list_builds(self, principal, environment: str | None = None, source: str | None = None, limit: int = 50) -> list[BuildResponse]:
        return [self.view(record) for record in self.state.list_builds(principal.infra_owner, environment, source, min(max(limit, 1), 200))]

    # ---- restarts and stopping
    def resume(self) -> list[str]:
        """Builds go on in Cloud Build while this service is down: follow each unfinished one again."""
        found = [record["build_id"] for record in self.state.builds_in(MOVING)]
        for build_id in found:
            event("build_resumed", build_id=build_id)
            self._follow_later(build_id)
        return found

    def stop(self) -> None:
        self.stopping.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
