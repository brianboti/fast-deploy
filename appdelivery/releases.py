"""Releases: what a team asked to run, where it runs, and the one before it (docs/DELIVERY-SPEC.md section 5).

A release names only an image this service built: its digest must be one a build of the same team recorded (building.py).

A deploy is planned and recorded at once and rolled out by a background worker, one machine at a time. The first machine
that does not come up healthy stops the rollout: the host script has already put that machine back on its previous
container, and the machines done before it are rolled back (or emptied, for a group's first release), so a group never
stays half on one version and half on another. One release per request and group moves at a time. Secret values exist
only in the worker's memory, on their way to the machines; a container's log is cleaned of them before it is kept.
"""
import concurrent.futures
import re
import threading
import time

import httpx

from . import attest, bindings, cloudbuild, planner, provenance, remote
from .building import Builder
from .cloudbuild import CloudBuildClient
from .config import BuildSettings
from .errors import DeliveryError, ErrorCode
from .events import bound, event
from .google import HostToken, ServiceAccountTokens
from .infra import InfraClient, InfraRequest
from .kube import ClusterDeployer, ClusterTarget
from .machine import Machine, MachineSession
from .models import BuildRequest, BuildResponse, ReleaseRequest, ReleaseResponse, RunResponse, TargetView
from .run import JobDeployer, JobTarget, RunDeployer, ServiceTarget
from .state import MOVING, StateManager, new_release_id

MAX_WAIT_SECONDS = 45
POLL_SECONDS = 0.5
MAX_LOG = 4000
REDACTED = "[redacted]"
EXECUTION = re.compile(r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")  # a Cloud Run execution's name


class InlineExecutor:
    """Runs work at once in the caller's thread: tests, and a CLI that wants the outcome before it returns."""

    def submit(self, fn, *args):
        fn(*args)

    def shutdown(self, wait=True, cancel_futures=False):
        pass


def scrub(text: str | None, values: list[str]) -> str | None:
    """A container's log without any secret value the release read (an application may print its own environment)."""
    if text is None:
        return None
    for value in sorted({value for value in values if len(value) >= 4}, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text[-MAX_LOG:]


class Delivery:
    def __init__(self, config, state: StateManager, infra=None, machines=None, tokens=None, http=None, runner=remote.run, executor=None, sleep=time.sleep,
                 clock=time.monotonic, clusters=None, services=None, jobs=None, builds=None, build_executor=None):
        self.config, self.state = config, state
        self.infra = infra or (lambda environment: InfraClient.for_environment(config, environment))
        self.machines = machines or (lambda environment: MachineSession(config.gcp.project_id, config.environments[environment].service_account))
        self.http = http or httpx.Client(timeout=30, follow_redirects=False)
        self.tokens = tokens or ServiceAccountTokens(self.http, HostToken(self.http))
        self.clusters = clusters or (lambda environment: ClusterDeployer(lambda: self.tokens(config.environments[environment].service_account),
                                                                          limits=config.environments[environment].workload))
        self.services = services or (lambda environment: RunDeployer(lambda: self.tokens(config.environments[environment].service_account)))
        self.jobs = jobs or (lambda environment: JobDeployer(lambda: self.tokens(config.environments[environment].service_account)))
        self.runner = runner
        self.executor = executor or concurrent.futures.ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix="appdelivery")
        self.sleep, self.clock = sleep, clock
        self._locks: dict[tuple[str, str], threading.RLock] = {}
        self._guard = threading.Lock()
        # Cloud Build is called as the environment's own service account
        builds = builds or (lambda environment: CloudBuildClient(lambda: self.tokens(config.environments[environment].service_account), self.http))
        self.builder = Builder(config, state, builds, build_executor, sleep, clock)

    # ---- helpers
    def lock(self, request_id: str, group: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault((request_id, group), threading.RLock())

    def _principal(self, name: str):
        found = next((item for item in self.config.principals if item.name == name), None)
        if found is None:
            raise DeliveryError(ErrorCode.PERMISSION_DENIED, f"Principal {name} is no longer in the configuration", {"identity": name})
        return found

    def _record(self, release_id: str, principal) -> dict:
        record = self.state.get_release(release_id)
        if record is None or record["infra_owner"] != principal.infra_owner:
            raise DeliveryError(ErrorCode.NOT_FOUND, f"No release {release_id}", {"release_id": release_id})
        return record

    @staticmethod
    def _can_act(record: dict, principal, what: str) -> None:
        if not principal.can_deploy:
            raise DeliveryError(ErrorCode.PERMISSION_DENIED, f"{principal.name} may not {what} releases", {"identity": principal.name})
        if record["environment"] not in principal.environments:
            raise DeliveryError(ErrorCode.PERMISSION_DENIED, f"{principal.name} may not act in {record['environment']}", {"identity": principal.name})

    def _submit(self, release_id: str, moving: str, fn) -> None:
        try:
            self.executor.submit(self._guarded, release_id, moving, fn)
        except RuntimeError:
            error = DeliveryError(ErrorCode.INTERNAL_ERROR, "The service is shutting down and took no new work").detail().model_dump(mode="json")
            self.state.transition(release_id, (moving,), {"ROLLING_OUT": "FAILED", "ROLLING_BACK": "RUNNING", "REMOVING": "RUNNING"}[moving], error)
            raise DeliveryError(ErrorCode.INTERNAL_ERROR, "The service is shutting down; ask again once it is back") from None

    def _guarded(self, release_id: str, moving: str, fn) -> None:
        """Run a worker; whatever escapes it becomes a failed release with a code, never a silent stop."""
        try:
            fn(release_id)
        except Exception as exc:
            failed = {"ROLLING_OUT": "FAILED", "ROLLING_BACK": "ROLLBACK_FAILED", "REMOVING": "REMOVE_FAILED"}[moving]
            error = exc if isinstance(exc, DeliveryError) else DeliveryError(ErrorCode.INTERNAL_ERROR, f"The worker stopped on an unexpected {type(exc).__name__}")
            self.state.transition(release_id, (moving,), failed, error.detail().model_dump(mode="json"))
            event("worker_failed", release_id=release_id, code=error.code.value, exception=type(exc).__name__)

    def _wait(self, release_id: str, seconds: int) -> None:
        deadline = self.clock() + min(max(seconds, 0), MAX_WAIT_SECONDS)
        while self.clock() < deadline:
            record = self.state.get_release(release_id)
            if record is None or record["status"] not in MOVING:
                return
            self.sleep(POLL_SECONDS)

    def view(self, record: dict) -> ReleaseResponse:
        return ReleaseResponse(release_id=record["release_id"], status=record["status"], environment=record["environment"], request_id=record["request_id"],
                               group=record["group"], image=record["image"], previous=record["previous"], build_id=record["plan"].get("build_id"),
                               can_roll_back=record["can_roll_back"],
                               requested_by=record["requested_by"], approved_by=record["approved_by"], note=record["request"].get("note"),
                               env=record["plan"].get("env", {}), warnings=record["plan"].get("warnings", []),
                               targets=[TargetView(resource=item["resource_id"], name=item["name"], zone=item["zone"], status=item["status"],
                                                   started_at=item["started_at"], finished_at=item["finished_at"], error=item["error"], log=item["log"])
                                        for item in record["targets"]],
                               error=record["error"], created_at=record["created_at"], updated_at=record["updated_at"])

    # ---- builds
    def build(self, request: BuildRequest, principal, wait_seconds: int = 0) -> BuildResponse:
        return self.builder.build(request, principal, wait_seconds)

    def get_build(self, build_id: str, principal) -> BuildResponse:
        return self.builder.get(build_id, principal)

    def list_builds(self, principal, environment: str | None = None, source: str | None = None, limit: int = 50) -> list[BuildResponse]:
        return self.builder.list_builds(principal, environment, source, limit)

    def built_here(self, release: ReleaseRequest, principal) -> str:
        """The build of this service, for the same team, that produced exactly this digest; else no release. An image made
        or pushed anywhere else, even into the team's own registry, has no such build."""
        found = self.state.built(principal.infra_owner, release.image)
        if found is None:
            raise DeliveryError(ErrorCode.NOT_BUILT_HERE, f"{release.image} was not built by this service for {principal.infra_owner}: images are built only in the "
                                "trusted project. Ask for one with build_image and release the digest it reports (built)", {"image": release.image})
        return found["build_id"]

    # ---- plan and deploy
    def plan(self, release: ReleaseRequest, principal):
        return self._plan(release, principal)[1]

    def _plan(self, release: ReleaseRequest, principal):
        """(the release with its cluster named, its plan)."""
        planner.authorize(release, principal, self.config)
        build_id = self.built_here(release, principal)
        infra = self.infra(release.environment).get_request(release.request_id)
        release = planner.resolve(release, infra)
        planned = planner.plan(release, principal, self.config, infra)
        running = self.state.running_release(release.request_id, release.target.key())
        planned.replaces = running["release_id"] if running else None
        planned.build_id = build_id
        return release, planned

    def deploy(self, release: ReleaseRequest, principal, wait_seconds: int = 0) -> ReleaseResponse:
        release, planned = self._plan(release, principal)
        group = release.target.key()
        with self.lock(release.request_id, group):
            busy = self.state.busy_release(release.request_id, group)
            if busy is not None:
                raise DeliveryError(ErrorCode.INVALID_STATE, f"Release {busy['release_id']} on group {group} is {busy['status']}; one release moves at a time",
                                    {"release_id": busy["release_id"], "status": busy["status"]})
            release_id = new_release_id()
            status = "AWAITING_APPROVAL" if planned.approval_required else "ROLLING_OUT"
            self.state.create_release(release_id, release.request_id, group, release.environment, principal.infra_owner, principal.name, status, release.image,
                                      planned.replaces, release.model_dump(mode="json"), {"env": planned.env, "warnings": planned.warnings, "build_id": planned.build_id},
                                      [item.model_dump() for item in planned.machines])
        with bound(release_id, principal.name):
            event("release_received", request_id=release.request_id, group=group, environment=release.environment, image=release.image, status=status,
                  replaces=planned.replaces)
        if status == "ROLLING_OUT":
            self._submit(release_id, "ROLLING_OUT", self._rollout)
            if wait_seconds:
                self._wait(release_id, wait_seconds)
        return self.view(self.state.get_release(release_id))

    def approve(self, release_id: str, principal, wait_seconds: int = 0) -> ReleaseResponse:
        record = self._record(release_id, principal)
        if not principal.can_approve or record["environment"] not in principal.environments:
            raise DeliveryError(ErrorCode.PERMISSION_DENIED, f"{principal.name} may not approve releases in {record['environment']}", {"identity": principal.name})
        if principal.name == record["requested_by"]:
            raise DeliveryError(ErrorCode.PERMISSION_DENIED, "A release is approved by a principal other than the one that asked for it", {"identity": principal.name})
        with self.lock(record["request_id"], record["group"]):
            if not self.state.transition(release_id, ("AWAITING_APPROVAL",), "ROLLING_OUT", approved_by=principal.name):
                raise DeliveryError(ErrorCode.INVALID_STATE, f"Release {release_id} is {record['status']}, not waiting for approval", {"status": record["status"]})
        with bound(release_id, principal.name):
            event("release_approved")
        self._submit(release_id, "ROLLING_OUT", self._rollout)
        if wait_seconds:
            self._wait(release_id, wait_seconds)
        return self.view(self.state.get_release(release_id))

    def _prepare(self, record: dict, release: ReleaseRequest):
        """Everything checked again as it is now (time may have passed since the plan, or the approval), and the machines
        or the cluster still the ones recorded. Returns (infra request, machine session or cluster deployer)."""
        principal = self._principal(record["requested_by"])
        if record["status"] == "ROLLING_OUT":  # what was built here stays built here; checked again for a release approved later
            self.built_here(release, principal)
        infra = self.infra(release.environment).get_request(release.request_id)
        planned = planner.plan(release, principal, self.config, infra)
        if {item.resource for item in planned.machines} != {item["resource_id"] for item in record["targets"]}:
            raise DeliveryError(ErrorCode.INVALID_STATE, f"The targets of {release.target.key()} changed since the release was recorded; deploy again",
                                {"recorded": [item["resource_id"] for item in record["targets"]], "now": [item.resource for item in planned.machines]})
        return infra, self._sessions(release)(release.environment)

    def _sessions(self, release: ReleaseRequest):
        """What reaches the release's target: a cluster deployer, a Cloud Run deployer, or a machine session."""
        if release.target.job:
            return self.jobs
        return self.clusters if release.target.cluster else self.services if release.target.service else self.machines

    def _target(self, infra: InfraRequest, release: ReleaseRequest):
        if release.target.job:
            job = infra.job(release.target.job)
            return JobTarget(job.id, job.name, job.provider_id)
        if release.target.service:
            service = infra.service(release.target.service)
            return ServiceTarget(service.id, service.name, service.provider_id)
        return self._cluster_target(infra, release)

    @staticmethod
    def _cluster_target(infra: InfraRequest, release: ReleaseRequest) -> ClusterTarget:
        cluster = infra.cluster(release.target.cluster)
        door = cluster.public
        return ClusterTarget(cluster.id, cluster.name, cluster.endpoint, cluster.ca_certificate, cluster.namespace,
                             gateway=door.gateway if door else None, https=bool(door and door.hostnames))

    def _registry_token(self, release: ReleaseRequest, environment) -> str | None:
        """A token that reads the image and nothing else: the environment's pull account, minted fresh for this one use. The
        environment's own token never leaves this service (the review of 2026-10-30: root on a team's machine could read it)."""
        if not release.registry().endswith(planner.ARTIFACT_REGISTRY_SUFFIX):
            return None
        mint = getattr(self.tokens, "fresh", self.tokens)
        return mint(environment.pull_service_account)

    def _rollout(self, release_id: str) -> None:
        record = self.state.get_release(release_id)
        release = ReleaseRequest.model_validate(record["request"])
        environment = self.config.environments[release.environment]
        with bound(release_id, record["requested_by"]):
            event("rollout_started", machines=len(record["targets"]))
            values: dict[str, str] = {}
            try:
                infra, session = self._prepare(record, release)
                if environment.require_provenance:
                    build = self.state.built(record["infra_owner"], release.image)
                    if build is None:
                        raise DeliveryError(ErrorCode.NOT_BUILT_HERE, f"{release.image} was not built by this service", {"image": release.image})
                    found = provenance.occurrences(self.http, self.tokens(environment.service_account), release.image)
                    built_in = self.config.environments.get(build["environment"])  # an environment since removed keeps its builds verifiable
                    settings = built_in.build if built_in is not None else None
                    builder = settings.builder if settings is not None else BuildSettings.model_fields["builder"].default
                    event("provenance_verified", **provenance.verify(release.image, build, found, expected=cloudbuild.expected(build, builder)))
                if environment.require_attestation:
                    verified = attest.verify(release, environment, self._registry_token(release, environment), self.runner)
                    event("image_verified", **verified)
                # a service's container reads its secrets itself at start, so only references to them are made here
                resolve = bindings.references if release.target.service or release.target.job else bindings.resolve
                values = resolve(release, infra, self.http, self.tokens(environment.service_account))
            except DeliveryError as exc:
                for target in record["targets"]:
                    self.state.set_target(release_id, target["resource_id"], "SKIPPED")
                self.state.transition(release_id, ("ROLLING_OUT",), "FAILED", exc.detail().model_dump(mode="json"))
                event("rollout_failed", code=exc.code.value, stage="prepare")
                return
            try:
                if release.target.single:
                    self._roll_single(record, release, infra, session, values)
                else:
                    self._roll(record, release, environment, session, values)
            finally:
                values.clear()

    def _previous(self, record: dict) -> tuple[str, ReleaseRequest] | None:
        if not record["previous"]:
            return None
        previous = self.state.get_release(record["previous"])
        return previous["release_id"], ReleaseRequest.model_validate(previous["request"])

    def _roll_single(self, record: dict, release: ReleaseRequest, infra: InfraRequest, deployer, values: dict) -> None:
        """One workload on a cluster, or one service: Kubernetes rolls the replicas and a failure puts the previous release
        back; Cloud Run gives a new revision traffic only once it is ready. Either way the previous release keeps what it
        needs, so a failure here leaves the running release's rollback as it was."""
        release_id, [target] = record["release_id"], record["targets"]
        secret_values = [value for name, binding in release.env.items() if binding.secret is not None and isinstance(value := values[name], str)]
        where = "cluster" if release.target.cluster else "service" if release.target.service else "job"
        workload = release.target.workload or target["name"]
        self.state.set_target(release_id, target["resource_id"], "DEPLOYING", started=True)
        event(f"{where}_deploying", **{where: target["name"]}, workload=workload, replicas=release.target.replicas)
        try:
            report = deployer.deploy(self._target(infra, release), release_id, release, dict(values), self._previous(record))
        except DeliveryError as exc:
            self.state.set_target(release_id, target["resource_id"], "FAILED", exc.detail().model_dump(mode="json"), finished=True)
            error = DeliveryError(ErrorCode.ROLLOUT_FAILED, f"The rollout of {workload} stopped: {exc.message}", {where: target["name"], "cause": exc.detail().model_dump(mode="json")})
            self.state.transition(release_id, ("ROLLING_OUT",), "FAILED", error.detail().model_dump(mode="json"))
            event("rollout_failed", code=exc.code.value, **{where: target["name"]})
            return
        if report.result == "healthy":
            self.state.set_target(release_id, target["resource_id"], "HEALTHY", finished=True)
            with self.lock(record["request_id"], record["group"]):
                self.state.transition(release_id, ("ROLLING_OUT",), "RUNNING", can_roll_back=record["previous"] is not None)
                if record["previous"]:
                    self.state.transition(record["previous"], ("RUNNING",), "SUPERSEDED", can_roll_back=False)
            event("rollout_completed", **{where: target["name"]}, workload=workload)
            return
        restored = report.fields.get("restored")
        after = {"yes": "the previous release runs again", "none": "nothing ran there before, so nothing runs now",
                 "failed": "putting the previous release back did not become healthy either"}.get(restored, "")
        cause = DeliveryError(ErrorCode.UNHEALTHY, f"{workload} did not become healthy on {target['name']}: {report.fields.get('reason', '')}; {after}",
                              {where: target["name"], "workload": workload, "restored": restored})
        self.state.set_target(release_id, target["resource_id"], "UNHEALTHY", cause.detail().model_dump(mode="json"), scrub(report.log(), secret_values), finished=True)
        if restored == "failed" and record["previous"]:
            with self.lock(record["request_id"], record["group"]):
                self.state.transition(record["previous"], ("RUNNING",), "RUNNING", can_roll_back=False)
        error = DeliveryError(ErrorCode.ROLLOUT_FAILED, f"The rollout of {workload} stopped: {cause.message}", {where: target["name"], "cause": cause.detail().model_dump(mode="json")})
        self.state.transition(release_id, ("ROLLING_OUT",), "FAILED", error.detail().model_dump(mode="json"))
        event("rollout_failed", **{where: target["name"]}, restored=restored)

    def _roll(self, record: dict, release: ReleaseRequest, environment, session: MachineSession, values: dict[str, str]) -> None:
        release_id = record["release_id"]
        text = bindings.env_file(values)
        secret_values = [values[name] for name, binding in release.env.items() if binding.secret is not None]
        done, failure, touched = [], None, False
        for target in record["targets"]:
            machine = Machine(target["name"], target["zone"])
            self.state.set_target(release_id, target["resource_id"], "DEPLOYING", started=True)
            event("machine_deploying", machine=machine.name)
            try:
                report = session.deploy(machine, release_id, release, text, self._registry_token(release, environment), environment.workload)
            except DeliveryError as exc:
                self.state.set_target(release_id, target["resource_id"], "FAILED", exc.detail().model_dump(mode="json"), finished=True)
                failure = (machine.name, exc.detail().model_dump(mode="json"))
                touched = exc.code == ErrorCode.MACHINE_ERROR  # the script ran and stopped somewhere; an unreachable machine was not changed
                event("machine_failed", machine=machine.name, code=exc.code.value)
                break
            if report.result == "healthy":
                self.state.set_target(release_id, target["resource_id"], "HEALTHY", finished=True)
                done.append(target)
                event("machine_healthy", machine=machine.name, attempts=report.attempts)
                continue
            if report.result == "unhealthy":
                restored = report.fields.get("restored") == "yes"
                error = DeliveryError(ErrorCode.UNHEALTHY, f"{machine.name} did not answer 2xx on {release.health.path} within {release.health.timeout_seconds} seconds; "
                                      + ("its previous container runs again" if restored else "nothing ran there before, so nothing runs now"),
                                      {"machine": machine.name, "restored": restored})
                self.state.set_target(release_id, target["resource_id"], "UNHEALTHY", error.detail().model_dump(mode="json"), scrub(report.log(), secret_values), finished=True)
            else:
                error = DeliveryError(ErrorCode.PULL_FAILED if report.result == "pull_failed" else ErrorCode.MACHINE_ERROR,
                                      f"{machine.name} could not {'log in to the registry' if report.fields.get('reason') == 'login' else 'pull the image'}; nothing changed there"
                                      if report.result == "pull_failed" else f"{machine.name} answered {report.result}", {"machine": machine.name, **report.fields})
                self.state.set_target(release_id, target["resource_id"], "FAILED", error.detail().model_dump(mode="json"), finished=True)
            failure = (machine.name, error.detail().model_dump(mode="json"))
            # an unhealthy machine was demoted and restored, and the script may have stopped anywhere: its previous container is spent
            touched = report.result != "pull_failed"
            event("machine_unhealthy" if report.result == "unhealthy" else "machine_failed", machine=machine.name, result=report.result)
            break
        if failure is None:
            with self.lock(record["request_id"], record["group"]):
                self.state.transition(release_id, ("ROLLING_OUT",), "RUNNING", can_roll_back=record["previous"] is not None)
                if record["previous"]:
                    self.state.transition(record["previous"], ("RUNNING",), "SUPERSEDED", can_roll_back=False)
            event("rollout_completed", machines=len(done))
            return
        undone = self._undo(record, release, session, done)
        for target in self.state.get_release(release_id)["targets"]:
            if target["status"] == "PENDING":  # never reached: the rollout stopped before it
                self.state.set_target(release_id, target["resource_id"], "SKIPPED")
        if record["previous"] and (touched or done):
            # Putting a machine back starts its previous container, so the machine keeps nothing older: the release running
            # again can no longer be rolled back on it (found on a real machine on 2026-10-29, docs/DELIVERY-SPIKE.md)
            with self.lock(record["request_id"], record["group"]):
                self.state.transition(record["previous"], ("RUNNING",), "RUNNING", can_roll_back=False)
        error = DeliveryError(ErrorCode.ROLLOUT_FAILED, f"The rollout stopped at {failure[0]}: {failure[1]['message']}",
                              {"machine": failure[0], "cause": failure[1], "undone": undone})
        self.state.transition(release_id, ("ROLLING_OUT",), "FAILED", error.detail().model_dump(mode="json"))
        event("rollout_failed", machine=failure[0], code=failure[1]["code"], undone=undone)

    def _undo(self, record: dict, release: ReleaseRequest, session: MachineSession, done: list[dict]) -> list[str]:
        """Put the machines a failed rollout had finished back on the previous release, or empty them for a group's first."""
        undone = []
        for target in reversed(done):
            machine = Machine(target["name"], target["zone"])
            try:
                if record["previous"]:
                    report = session.rollback(machine, release)
                    ok, status = report.result == "rolled_back", "RESTORED"
                else:
                    report = session.remove(machine)
                    ok, status = report.result == "removed", "REMOVED"
            except DeliveryError as exc:
                self.state.set_target(record["release_id"], target["resource_id"], "UNDO_FAILED", exc.detail().model_dump(mode="json"))
                event("machine_undo_failed", machine=machine.name, code=exc.code.value)
                continue
            if ok:
                self.state.set_target(record["release_id"], target["resource_id"], status)
                undone.append(machine.name)
                event("machine_restored", machine=machine.name, status=status)
            else:
                error = DeliveryError(ErrorCode.ROLLBACK_FAILED, f"{machine.name} answered {report.result} when it was put back", {"machine": machine.name})
                self.state.set_target(record["release_id"], target["resource_id"], "UNDO_FAILED", error.detail().model_dump(mode="json"))
        return undone

    # ---- rollback and remove
    def rollback(self, release_id: str, principal, wait_seconds: int = 0) -> ReleaseResponse:
        record = self._record(release_id, principal)
        self._can_act(record, principal, "roll back")
        if record["status"] != "RUNNING" or not record["can_roll_back"] or not record["previous"]:
            reason = "it is not the release running on its group" if record["status"] != "RUNNING" else \
                "its machines keep no earlier container (a group's first release, or one a rollback restored): deploy the version you want instead"
            raise DeliveryError(ErrorCode.INVALID_STATE, f"Release {release_id} cannot be rolled back: {reason}", {"status": record["status"]})
        self._move(record, "ROLLING_BACK", self._rollback)
        if wait_seconds:
            self._wait(release_id, wait_seconds)
        return self.view(self.state.get_release(release_id))

    # ---- running a job
    def _job_release(self, release_id: str, principal, action: str | None):
        record = self._record(release_id, principal)
        if action:
            self._can_act(record, principal, action)
        release = ReleaseRequest.model_validate(record["request"])
        if not release.target.job:
            raise DeliveryError(ErrorCode.INVALID_STATE, f"Release {release_id} is not on a job; only a job's release runs on request", {"release_id": release_id})
        if record["status"] != "RUNNING":
            raise DeliveryError(ErrorCode.INVALID_STATE, f"Release {release_id} is {record['status']}, not the release its job holds", {"status": record["status"]})
        infra = self.infra(release.environment).get_request(release.request_id)
        return record, release, self._target(infra, release), self.jobs(release.environment)

    def start_run(self, release_id: str, principal, wait_seconds: int = 0) -> RunResponse:
        """Run the job now, as it holds this release; with wait_seconds, wait that long for it to finish."""
        record, release, target, deployer = self._job_release(release_id, principal, "run")
        with bound(release_id, principal.name):
            run = deployer.start(target, release_id)
            event("job_run_started", job=target.name, execution=run.execution)
        started = self.clock()
        while run.status == "RUNNING" and self.clock() - started < min(wait_seconds, MAX_WAIT_SECONDS):
            self.sleep(POLL_SECONDS)
            run = deployer.execution(target, release_id, run.execution)
        return run

    def get_run(self, release_id: str, execution: str, principal) -> RunResponse:
        record, release, target, deployer = self._job_release(release_id, principal, None)
        if not EXECUTION.fullmatch(execution) or not execution.startswith(f"{target.name}-"):  # it becomes part of a Cloud Run URL (L11)
            raise DeliveryError(ErrorCode.NOT_FOUND, f"{execution} is not a run of {target.name}", {"execution": execution})
        return deployer.execution(target, release_id, execution)

    def remove(self, release_id: str, principal, wait_seconds: int = 0) -> ReleaseResponse:
        record = self._record(release_id, principal)
        self._can_act(record, principal, "remove")
        if record["status"] != "RUNNING":
            raise DeliveryError(ErrorCode.INVALID_STATE, f"Release {release_id} is {record['status']}; only the release running on a group can be removed",
                                {"status": record["status"]})
        self._move(record, "REMOVING", self._remove)
        if wait_seconds:
            self._wait(release_id, wait_seconds)
        return self.view(self.state.get_release(release_id))

    def _move(self, record: dict, moving: str, fn) -> None:
        with self.lock(record["request_id"], record["group"]):
            busy = self.state.busy_release(record["request_id"], record["group"])
            if busy is not None:
                raise DeliveryError(ErrorCode.INVALID_STATE, f"Release {busy['release_id']} on group {record['group']} is {busy['status']}; one release moves at a time",
                                    {"release_id": busy["release_id"], "status": busy["status"]})
            if not self.state.transition(record["release_id"], ("RUNNING",), moving):
                raise DeliveryError(ErrorCode.INVALID_STATE, f"Release {record['release_id']} changed meanwhile; ask again", {"release_id": record["release_id"]})
        self._submit(record["release_id"], moving, fn)

    def _rollback(self, release_id: str) -> None:
        record = self.state.get_release(release_id)
        release = ReleaseRequest.model_validate(record["request"])
        with bound(release_id, record["requested_by"]):
            event("rollback_started", previous=record["previous"])
            try:
                infra, session = self._prepare(record, release)
            except DeliveryError as exc:
                self.state.transition(release_id, ("ROLLING_BACK",), "RUNNING", exc.detail().model_dump(mode="json"))
                event("rollback_failed", code=exc.code.value, stage="prepare")
                return
            if release.target.single:
                [target] = record["targets"]
                previous_id, previous = self._previous(record)
                try:
                    if release.target.job:  # a job keeps no earlier container: the previous release's is made again, its secrets pinned now
                        values = bindings.references(previous, infra, self.http, self.tokens(self.config.environments[release.environment].service_account))
                        report = session.rollback(self._target(infra, release), release_id, previous_id, previous, values)
                    else:
                        report = session.rollback(self._target(infra, release), release_id, previous_id, previous)
                    problem = None if report.result == "rolled_back" else DeliveryError(ErrorCode.ROLLBACK_FAILED, f"{target['name']} answered {report.result}",
                                                                                         {"target": target["name"], **report.fields})
                except DeliveryError as exc:
                    problem = exc
                if problem is not None:
                    self.state.set_target(release_id, target["resource_id"], "UNDO_FAILED", problem.detail().model_dump(mode="json"))
                    error = DeliveryError(ErrorCode.ROLLBACK_FAILED, f"The rollback on {target['name']} failed: {problem.message}", {"target": target["name"]})
                    self.state.transition(release_id, ("ROLLING_BACK",), "ROLLBACK_FAILED", error.detail().model_dump(mode="json"))
                    event("rollback_failed", cluster=target["name"], code=problem.code.value)
                    return
                self.state.set_target(release_id, target["resource_id"], "ROLLED_BACK")
            for target in ([] if release.target.single else record["targets"]):
                machine = Machine(target["name"], target["zone"])
                try:
                    report = session.rollback(machine, release)
                    problem = None if report.result == "rolled_back" else DeliveryError(ErrorCode.ROLLBACK_FAILED, f"{machine.name} answered {report.result}",
                                                                                         {"machine": machine.name, **{k: v for k, v in report.fields.items() if k != "log"}})
                except DeliveryError as exc:
                    problem = exc
                if problem is not None:
                    self.state.set_target(release_id, target["resource_id"], "UNDO_FAILED", problem.detail().model_dump(mode="json"))
                    error = DeliveryError(ErrorCode.ROLLBACK_FAILED, f"The rollback stopped at {machine.name}: {problem.message}", {"machine": machine.name})
                    self.state.transition(release_id, ("ROLLING_BACK",), "ROLLBACK_FAILED", error.detail().model_dump(mode="json"))
                    event("rollback_failed", machine=machine.name, code=problem.code.value)
                    return
                self.state.set_target(release_id, target["resource_id"], "ROLLED_BACK")
            with self.lock(record["request_id"], record["group"]):
                self.state.transition(release_id, ("ROLLING_BACK",), "ROLLED_BACK", can_roll_back=False)
                # the previous release runs again, and its machines keep nothing earlier: it cannot be rolled back itself
                self.state.transition(record["previous"], ("SUPERSEDED",), "RUNNING", can_roll_back=False)
            event("rollback_completed", running=record["previous"])

    def _remove(self, release_id: str) -> None:
        record = self.state.get_release(release_id)
        release = ReleaseRequest.model_validate(record["request"])
        with bound(release_id, record["requested_by"]):
            event("remove_started")
            try:
                infra, session = self._prepare(record, release)
            except DeliveryError as exc:
                self.state.transition(release_id, ("REMOVING",), "RUNNING", exc.detail().model_dump(mode="json"))
                event("remove_failed", code=exc.code.value, stage="prepare")
                return
            failed = []
            if release.target.single:
                [target] = record["targets"]
                try:
                    session.remove(self._target(infra, release), release.target.workload)
                    self.state.set_target(release_id, target["resource_id"], "REMOVED")
                except DeliveryError as exc:
                    self.state.set_target(release_id, target["resource_id"], "UNDO_FAILED", exc.detail().model_dump(mode="json"))
                    failed.append(target["name"])
            for target in ([] if release.target.single else record["targets"]):
                machine = Machine(target["name"], target["zone"])
                try:
                    report = session.remove(machine)
                    if report.result != "removed":
                        raise DeliveryError(ErrorCode.REMOVE_FAILED, f"{machine.name} answered {report.result}", {"machine": machine.name})
                    self.state.set_target(release_id, target["resource_id"], "REMOVED")
                except DeliveryError as exc:
                    self.state.set_target(release_id, target["resource_id"], "UNDO_FAILED", exc.detail().model_dump(mode="json"))
                    failed.append(machine.name)
            if failed:
                error = DeliveryError(ErrorCode.REMOVE_FAILED, f"Could not remove the containers from {', '.join(failed)}; remove again", {"machines": failed})
                self.state.transition(release_id, ("REMOVING",), "REMOVE_FAILED", error.detail().model_dump(mode="json"))
                event("remove_failed", machines=failed)
                return
            self.state.transition(release_id, ("REMOVING",), "REMOVED", can_roll_back=False)
            event("remove_completed")

    # ---- reading
    def get(self, release_id: str, principal) -> ReleaseResponse:
        return self.view(self._record(release_id, principal))

    def list_releases(self, principal, request_id: str | None = None, environment: str | None = None, limit: int = 50) -> list[ReleaseResponse]:
        return [self.view(record) for record in self.state.list_releases(principal.infra_owner, request_id, environment, min(max(limit, 1), 200))]

    def running(self, request_id: str, principal, live: bool = False) -> dict:
        """The release running on each group of a request, and with live, what each machine reports now."""
        groups = []
        for record in self.state.running_releases(request_id):
            if record["infra_owner"] != principal.infra_owner:
                continue
            entry = {"group": record["group"], "release_id": record["release_id"], "image": record["image"], "environment": record["environment"], "machines": []}
            release = ReleaseRequest.model_validate(record["request"])
            infra, session = None, None
            if live:  # the host's status action is an action too: everything checked again first, as before any other (L13)
                try:
                    infra, session = self._prepare(record, release)
                except DeliveryError as exc:
                    entry["error"] = exc.detail().model_dump(mode="json")
            for target in record["targets"]:
                machine = {"resource": target["resource_id"], "name": target["name"], "status": target["status"]}
                if session is not None:
                    try:
                        if release.target.single:
                            report = session.status(self._target(infra, release), release.target.workload)
                        else:
                            report = session.status(Machine(target["name"], target["zone"]))
                        machine.update(running=report.fields.get("running") == "yes", image=report.fields.get("image") or None,
                                       matches=report.fields.get("image") == record["image"])
                    except DeliveryError as exc:
                        machine["error"] = exc.detail().model_dump(mode="json")
                entry["machines"].append(machine)
            groups.append(entry)
        return {"request_id": request_id, "groups": groups}

    def service_status(self, version: str) -> dict:
        environments = {name: {"service_account": item.service_account, "allowed_registries": item.allowed_registries, "require_attestation": item.require_attestation,
                               "require_provenance": item.require_provenance, "require_approval": item.require_approval,
                               "build": {"service_account": item.build.service_account, "machine_type": item.build.machine_type} if item.build else None}
                        for name, item in self.config.environments.items()}
        return {"status": "OK", "version": version, "cloudinfra": self.config.cloudinfra.url, "environments": environments,
                "moving": len(self.state.releases_in(MOVING)), "building": len(self.state.builds_in(("QUEUED", "WORKING")))}

    # ---- restarts
    def recover(self) -> list[dict]:
        """No worker survives a restart: a release left moving is marked as interrupted, so it can be deployed, rolled back
        or removed again. The host script is idempotent by container name, so the next action puts each machine right."""
        found = []
        interrupted = DeliveryError(ErrorCode.INTERNAL_ERROR, "Interrupted by a restart of the service", {"cause": "INTERRUPTED"}).detail().model_dump(mode="json")
        for record in self.state.releases_in(MOVING):
            after = {"ROLLING_OUT": "FAILED", "ROLLING_BACK": "ROLLBACK_FAILED", "REMOVING": "REMOVE_FAILED"}[record["status"]]
            for target in record["targets"]:
                if target["status"] == "DEPLOYING":
                    self.state.set_target(record["release_id"], target["resource_id"], "FAILED", interrupted, finished=True)
                elif target["status"] == "PENDING":
                    self.state.set_target(record["release_id"], target["resource_id"], "SKIPPED")
            self.state.transition(record["release_id"], (record["status"],), after, interrupted)
            with bound(record["release_id"], "appdelivery-recovery"):
                event("release_recovered", was=record["status"], now=after)
            found.append({"release_id": record["release_id"], "was": record["status"], "now": after})
        return found
