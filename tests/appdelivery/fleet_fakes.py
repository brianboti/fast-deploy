"""A fake fleet for the release tests: machines that answer the way host/release.sh does (proven on a real machine on
2026-10-29), a fake cloudinfra whose answer a test can change, and a Delivery wired to them with an inline executor."""
import base64

import httpx
from build_fakes import FakeCloudBuild
from delivery_fakes import DEV_SA, IMAGE, PROD_SA, Runner, cluster_response, infra_response, job_response, make_config, principal, release_dict, service_response
from kube_fakes import FakeKube
from run_fakes import FakeRun, FakeRunJob

from appdelivery.errors import DeliveryError, ErrorCode
from appdelivery.infra import InfraRequest
from appdelivery.kube import ClusterDeployer
from appdelivery.machine import HostReport
from appdelivery.models import ReleaseRequest
from appdelivery.releases import Delivery, InlineExecutor
from appdelivery.remote import Completed
from appdelivery.run import JobDeployer, RunDeployer
from appdelivery.state import StateManager

SECRET_VALUE = "s3cr3t-Value-Never-Kept"
V1 = "us-central1-docker.pkg.dev/acme-apps/shop/web@sha256:" + "1" * 64
V2 = "us-central1-docker.pkg.dev/acme-apps/shop/web@sha256:" + "2" * 64
V3 = "us-central1-docker.pkg.dev/acme-apps/shop/web@sha256:" + "3" * 64


class FakeFleet:
    """Each machine holds app and previous images, as the script keeps the containers app and app-previous."""

    def __init__(self):
        self.app: dict[str, str | None] = {}
        self.previous: dict[str, str | None] = {}
        self.unhealthy: set[str] = set()  # images that never answer 2xx
        self.unreachable: set[str] = set()  # machine names IAP will not reach
        self.pull_fails: set[str] = set()
        self.calls: list[dict] = []

    def session(self, environment):
        return FakeSession(self, environment)


class FakeSession:
    def __init__(self, fleet: FakeFleet, environment: str):
        self.fleet, self.environment = fleet, environment

    def _reach(self, machine, action, **fields):
        self.fleet.calls.append({"machine": machine.name, "action": action, "environment": self.environment, **fields})
        if machine.name in self.fleet.unreachable:
            raise DeliveryError(ErrorCode.MACHINE_UNREACHABLE, f"Cannot reach {machine.name}: the SSH session failed", {"machine": machine.name})

    def deploy(self, machine, release_id, release, env_text, token):
        self._reach(machine, "deploy", release_id=release_id, image=release.image, env_text=env_text, token=token)
        fleet, name = self.fleet, machine.name
        if release.image in fleet.pull_fails:
            return HostReport("pull_failed", {"reason": "pull"})
        old = fleet.app.get(name)
        if release.image in fleet.unhealthy:
            # the new container is removed and the running one becomes current again; the older previous is gone
            fleet.previous[name] = None
            log = base64.b64encode(f"starting with {env_text.strip()}\npanic: bad config\n".encode()).decode()
            return HostReport("unhealthy", {"restored": "yes" if old else "none", "image": old or "", "log": log})
        fleet.previous[name], fleet.app[name] = old, release.image
        return HostReport("healthy", {"image": release.image, "release": release_id, "previous": old or ""})

    def rollback(self, machine, release):
        self._reach(machine, "rollback")
        previous = self.fleet.previous.get(machine.name)
        if not previous:
            return HostReport("no_previous")
        self.fleet.app[machine.name], self.fleet.previous[machine.name] = previous, None
        return HostReport("rolled_back", {"image": previous})

    def remove(self, machine):
        self._reach(machine, "remove")
        self.fleet.app[machine.name] = self.fleet.previous[machine.name] = None
        return HostReport("removed")

    def status(self, machine):
        self._reach(machine, "status")
        image = self.fleet.app.get(machine.name)
        return HostReport("status", {"image": image or "", "running": "yes" if image else "no", "previous": self.fleet.previous.get(machine.name) or ""})


class FakeInfra:
    def __init__(self, cluster=False, service=False, job=False):
        self.answers = {"dev": dev_answer(cluster, service, job), "prod": answer(cluster, service, job)}
        self.reads = 0

    def __call__(self, environment):
        infra = self

        class Client:
            def get_request(self, request_id):
                infra.reads += 1
                return InfraRequest.model_validate(infra.answers[environment])
        return Client()


def answer(cluster=False, service=False, job=False):
    return cluster_response() if cluster else service_response() if service else job_response() if job else infra_response()


def dev_answer(cluster=False, service=False, job=False):
    answer = globals()["answer"](cluster, service, job)
    answer["access"][0]["member"] = f"serviceAccount:{DEV_SA}"
    return answer


def secret_manager():
    def handle(request):
        return httpx.Response(200, json={"name": request.url.path.removeprefix("/v1/").replace("/versions/latest:access", "/versions/7"),
                                          "payload": {"data": base64.b64encode(SECRET_VALUE.encode()).decode()}})
    return httpx.Client(transport=httpx.MockTransport(handle))


class World:
    """A Delivery, its state, its fake fleet and cloudinfra, and the principals of the fake configuration."""

    def __init__(self, database=":memory:", cosign=None, cluster=False, service=False, job=False, cloud_build=None):
        self.config = make_config(database=database)
        self.state = StateManager(database)
        self.fleet, self.infra, self.kube, self.run, self.run_job = FakeFleet(), FakeInfra(cluster, service, job), FakeKube(), FakeRun(), FakeRunJob()
        self.cosign = cosign or Runner(Completed(0, "[]", ""))
        self.cloud_build = cloud_build or FakeCloudBuild()
        self.tokens = []
        self.cluster_tokens = []
        self.delivery = Delivery(self.config, self.state, infra=self.infra, machines=self.fleet.session, tokens=self.token, http=secret_manager(), runner=self.cosign,
                                 executor=InlineExecutor(), clusters=self.deployer, services=self.run_deployer, jobs=self.job_deployer,
                                 builds=self.cloud_build, build_executor=InlineExecutor())
        self.delivery.builder.sleep = lambda seconds: None  # a build's follower polls Cloud Build every few seconds
        self.team, self.oncall = principal(self.config, "shop-team"), principal(self.config, "shop-oncall")
        for image in (V1, V2, V3, IMAGE):
            self.built(image)

    def built(self, image, infra_owner="shop-team", cloud_build_id=None, revision="a" * 40):
        """Record image@digest as one a build of this service produced, as building.py does when Cloud Build reports success."""
        self.seeded = getattr(self, "seeded", 0) + 1
        build_id = f"bld-{self.seeded:012x}"
        tag = f"{image.split('@')[0]}:{build_id}"
        self.state.create_build(build_id, "dev", infra_owner, "shop-team", "shop-web", revision, tag, "WORKING", cloud_build_id or "cloud-" + build_id, None,
                                {"environment": "dev", "source": "shop-web", "revision": revision, "image": "web"})
        self.state.update_build(build_id, ("WORKING",), "SUCCEEDED", built=image)
        return build_id

    def deployer(self, environment):
        service_account = self.config.environments[environment].service_account
        self.cluster_tokens.append(service_account)
        return ClusterDeployer(lambda: self.token(service_account), kube=lambda target: self.kube, sleep=lambda seconds: None)

    def run_deployer(self, environment):
        service_account = self.config.environments[environment].service_account
        return RunDeployer(lambda: self.token(service_account), run=self.run, sleep=lambda seconds: None)

    def job_deployer(self, environment):
        service_account = self.config.environments[environment].service_account
        return JobDeployer(lambda: self.token(service_account), run=self.run_job, sleep=lambda seconds: None)

    def token(self, service_account):
        self.tokens.append(service_account)
        return {DEV_SA: "dev-token", PROD_SA: "prod-token"}[service_account]

    def release(self, image=V1, environment="dev", **overrides):
        return ReleaseRequest.model_validate(release_dict(environment=environment, image=image, **overrides))

    def deploy(self, image=V1, environment="dev", **overrides):
        return self.delivery.deploy(self.release(image, environment, **overrides), self.team)
