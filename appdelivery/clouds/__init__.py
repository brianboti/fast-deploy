"""Where an environment's work runs: one Cloud per provider an environment names (config.Environment.provider). The
release and build logic above names no cloud; each Cloud reaches its own targets, reads secrets, mints the token a
machine pulls with, checks an image's provenance and runs builds (docs/research/RESEARCH-2026-11-03-azure.md, section 7:
the delivery service's provider seam).

Every method acts as the environment's own identity, never the service's, as on Google: a release can do only what the
team named that identity for on its request."""
from abc import ABC, abstractmethod


class Cloud(ABC):
    name = "abstract"

    # ---- what reaches each kind of target, for one environment
    @abstractmethod
    def machines(self, environment: str):
        """The session that runs the host script on the environment's machines (machine.MachineSession's interface)."""

    @abstractmethod
    def clusters(self, environment: str):
        """The deployer of a cluster workload (kube.ClusterDeployer's interface)."""

    @abstractmethod
    def services(self, environment: str):
        """The deployer of a managed container service (run.RunDeployer's interface)."""

    @abstractmethod
    def jobs(self, environment: str):
        """The deployer of a managed job (run.JobDeployer's interface)."""

    @abstractmethod
    def builds(self, environment: str):
        """The builder client of the environment (cloudbuild.CloudBuildClient's interface), as Builder drives it."""

    # ---- what a release reads on its way
    @abstractmethod
    def resolve(self, release, infra) -> dict[str, str]:
        """NAME -> value of every binding, secrets read now: for a container started on a machine or a cluster."""

    @abstractmethod
    def references(self, release, infra) -> dict:
        """NAME -> value or a reference the container reads its secret by at start: for a managed service or job."""

    @abstractmethod
    def pull_token(self, release) -> str | None:
        """A short-lived token that reads the release's image and nothing else, for a machine; None where the machine pulls
        without one."""

    @abstractmethod
    def provenance(self, release, build: dict) -> dict:
        """The verified provenance of the release's image as the build this service recorded made it (a summary for the
        event), or a DeliveryError."""
