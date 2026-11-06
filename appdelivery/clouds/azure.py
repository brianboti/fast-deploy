"""Azure behind the delivery service's provider seam (docs/research/RESEARCH-2026-11-03-azure.md, section 7): machines
through the operator's Bastion with Entra sign-in, AKS clusters, Container Apps services and jobs, Key Vault secrets, and a
registry token for a machine's pull. Every call is made as the environment's managed identity, attached to this service's
host; a machine's pull uses the environment's pull identity, whose registry token is the only credential a machine gets.

Builds and their provenance on Azure come with step 6c (an ACR task, and an in-toto statement this service signs); an
Azure environment cannot ask for either until then (config.Environment)."""
import os
import tempfile

from .. import apps, azure, bindings
from ..bastion import BastionSession
from ..errors import DeliveryError, ErrorCode
from ..kube import ClusterDeployer
from . import Cloud


class AzureCloud(Cloud):
    name = "azure"
    pull_user = azure.REGISTRY_USER

    def __init__(self, delivery, tokens=None, runner=None, sleep=None, clock=None):
        self.delivery = delivery
        self._tokens, self.runner = tokens, runner
        self.sleep, self.clock = sleep, clock

    @property
    def tokens(self):
        """The host's managed identity endpoint, made on first use (a Google-only service never asks it)."""
        if self._tokens is None:
            self._tokens = azure.ManagedIdentityTokens(self.delivery.http)
        return self._tokens

    def _environment(self, name: str):
        return self.delivery.config.environments[name]

    def _token(self, environment: str, audience: str) -> str:
        return self.tokens(self._environment(environment).managed_identity.client_id, audience)

    def _timing(self) -> dict:
        return {key: value for key, value in (("sleep", self.sleep), ("clock", self.clock)) if value is not None}

    def cli_directory(self, environment: str) -> str:
        """Where the Azure CLI keeps the environment's sign-in: one directory per environment, so no environment's session
        ever runs as another's identity."""
        settings, database = self.delivery.config.azure, self.delivery.config.database
        base = settings.cli_directory or os.path.join(tempfile.gettempdir() if database == ":memory:" else os.path.dirname(os.path.abspath(database)), "azure")
        return os.path.join(base, environment)

    # ---- targets
    def machines(self, environment: str):
        settings = self.delivery.config.azure
        return BastionSession(settings.bastion_parts, self._environment(environment).managed_identity.client_id, self.cli_directory(environment),
                              **({"runner": self.runner} if self.runner else {}), **self._timing())

    def clusters(self, environment: str):
        """AKS with Entra sign-in and Azure RBAC: the environment's token for AKS's server application, which the cluster's API
        takes as the managed identity; cloudinfra reports the address and CA, so no kubeconfig is fetched."""
        return ClusterDeployer(lambda: self._token(environment, azure.AKS), limits=self._environment(environment).workload, **self._timing())

    def services(self, environment: str):
        return apps.AppsDeployer(lambda: self._token(environment, azure.ARM), client=self.delivery.http, **self._timing())

    def jobs(self, environment: str):
        return apps.AppsJobDeployer(lambda: self._token(environment, azure.ARM), client=self.delivery.http, **self._timing())

    def builds(self, environment: str):
        raise DeliveryError(ErrorCode.BUILD_UNAVAILABLE, f"Environment {environment} is on Azure, where builds come with the delivery service's step 6c")

    # ---- reads
    def resolve(self, release, infra) -> dict[str, str]:
        token = self._token(release.environment, azure.KEY_VAULT)
        return bindings.values(release, infra, lambda secret: azure.read_secret(self.delivery.http, token, secret.resource_name))

    def references(self, release, infra) -> dict:
        """A Key Vault reference per secret, pinned to its newest enabled version, read from the version list (no value)."""
        token = self._token(release.environment, azure.KEY_VAULT)
        return bindings.pinned(release, infra, lambda secret: bindings.SecretRef(secret.resource_name,
                                                                                 azure.secret_version(self.delivery.http, token, secret.resource_name)))

    def pull_token(self, release) -> str | None:
        """A registry token of the environment's pull identity, for one machine's pull: the environment's own identity never
        leaves this service. A registry that is not an Azure one gets none."""
        registry = release.registry()
        if not azure.LOGIN_SERVER.fullmatch(registry):
            return None
        entra = self.tokens(self._environment(release.environment).pull_identity.client_id, azure.REGISTRY)
        return azure.registry_token(self.delivery.http, entra, registry)

    def provenance(self, release, build: dict) -> dict:
        raise DeliveryError(ErrorCode.PROVENANCE_FAILED, f"Environment {release.environment} is on Azure, where provenance comes with the delivery service's step 6c")
