"""The delivery service's provider seam (appdelivery/clouds/, docs/research/RESEARCH-2026-11-03-azure.md section 7): every
step of a release that touches a cloud goes through the cloud its environment names, so another cloud plugs in without
the release logic knowing it."""
from fleet_fakes import SECRET_VALUE, V1, World
from test_delivery_service import service_release

from appdelivery.clouds import Cloud
from appdelivery.clouds.gcp import GcpCloud
from appdelivery.models import ReleaseRequest


class Recording(Cloud):
    """A cloud that does what Google's does, through it, and writes down each call."""
    name = "recording"

    def __init__(self, inner: Cloud):
        self.inner, self.calls = inner, []

    def _note(self, what, *args):
        self.calls.append((what, *args))

    def machines(self, environment):
        self._note("machines", environment)
        return self.inner.machines(environment)

    def clusters(self, environment):
        self._note("clusters", environment)
        return self.inner.clusters(environment)

    def services(self, environment):
        self._note("services", environment)
        return self.inner.services(environment)

    def jobs(self, environment):
        self._note("jobs", environment)
        return self.inner.jobs(environment)

    def builds(self, environment):
        self._note("builds", environment)
        return self.inner.builds(environment)

    def resolve(self, release, infra):
        self._note("resolve", release.environment)
        return self.inner.resolve(release, infra)

    def references(self, release, infra):
        self._note("references", release.environment)
        return self.inner.references(release, infra)

    def pull_token(self, release):
        self._note("pull_token", release.environment)
        return self.inner.pull_token(release)

    def provenance(self, release, build):
        self._note("provenance", release.environment)
        return self.inner.provenance(release, build)


def test_an_environment_names_google_by_default_and_its_cloud_reads_the_services_own_factories():
    world = World()
    assert world.config.environments["dev"].provider == "gcp"
    assert isinstance(world.delivery.cloud("dev"), GcpCloud)
    assert world.config.environments["dev"].member == f"serviceAccount:{world.config.environments['dev'].service_account}"


def test_a_machine_release_reaches_its_targets_secrets_and_pull_token_only_through_the_environments_cloud():
    world = World()
    recording = Recording(world.delivery.clouds["gcp"])
    world.delivery.clouds["gcp"] = recording
    response = world.deploy()
    assert response.status == "RUNNING"
    kinds = [call[0] for call in recording.calls]
    assert "machines" in kinds and "resolve" in kinds and "pull_token" in kinds and set(kinds) <= {"machines", "resolve", "pull_token"}
    deploys = [call for call in world.fleet.calls if call["action"] == "deploy"]
    assert deploys[0]["env_text"].endswith(f"DB_PASSWORD={SECRET_VALUE}\n") and deploys[0]["token"] == "dev-pull-token", "the same release as before the seam"


def test_a_service_release_reads_references_not_values_through_the_cloud():
    world = World(service=True)
    recording = Recording(world.delivery.clouds["gcp"])
    world.delivery.clouds["gcp"] = recording
    response = world.delivery.deploy(ReleaseRequest.model_validate(service_release(V1)), world.team)
    assert response.status == "RUNNING", response.error
    kinds = {call[0] for call in recording.calls}
    assert {"services", "references"} <= kinds and "resolve" not in kinds and "machines" not in kinds
