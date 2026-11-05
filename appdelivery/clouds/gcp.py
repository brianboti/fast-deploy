"""Google Cloud behind the delivery service's provider seam: machines through IAP as the environment's service account,
GKE clusters, Cloud Run services and jobs, Cloud Build, Secret Manager, and Cloud Build's SLSA provenance read from
Container Analysis. The pieces are the modules that were the whole service before Azure (google.py, machine.py, kube.py,
run.py, cloudbuild.py, provenance.py, bindings.py); this class only composes them.

The Delivery that owns it keeps the factories and the client as its own attributes, which tests replace after
construction; this view reads them each time."""
from .. import bindings, cloudbuild, planner, provenance
from ..config import BuildSettings
from . import Cloud


class GcpCloud(Cloud):
    name = "gcp"

    def __init__(self, delivery):
        self.delivery = delivery

    def _environment(self, name: str):
        return self.delivery.config.environments[name]

    def machines(self, environment: str):
        return self.delivery.machines(environment)

    def clusters(self, environment: str):
        return self.delivery.clusters(environment)

    def services(self, environment: str):
        return self.delivery.services(environment)

    def jobs(self, environment: str):
        return self.delivery.jobs(environment)

    def builds(self, environment: str):
        return self.delivery.builds(environment)

    def _token(self, environment: str) -> str:
        return self.delivery.tokens(self._environment(environment).service_account)

    def resolve(self, release, infra) -> dict[str, str]:
        return bindings.resolve(release, infra, self.delivery.http, self._token(release.environment))

    def references(self, release, infra) -> dict:
        return bindings.references(release, infra, self.delivery.http, self._token(release.environment))

    def pull_token(self, release) -> str | None:
        """The environment's pull account, minted fresh for this one use. The environment's own token never leaves this
        service (the review of 2026-10-30: root on a team's machine could read it)."""
        if not release.registry().endswith(planner.ARTIFACT_REGISTRY_SUFFIX):
            return None
        mint = getattr(self.delivery.tokens, "fresh", self.delivery.tokens)
        return mint(self._environment(release.environment).pull_service_account)

    def provenance(self, release, build: dict) -> dict:
        found = provenance.occurrences(self.delivery.http, self._token(release.environment), release.image)
        built_in = self.delivery.config.environments.get(build["environment"])  # an environment since removed keeps its builds verifiable
        settings = built_in.build if built_in is not None else None
        builder = settings.builder if settings is not None else BuildSettings.model_fields["builder"].default
        return provenance.verify(release.image, build, found, expected=cloudbuild.expected(build, builder))
