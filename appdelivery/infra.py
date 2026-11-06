"""The infrastructure layer as this service sees it: one read, `GET /infrastructure/{request_id}`, over HTTP with the
environment's read-only token. Only the fields a release needs are kept; fields the layer adds later are ignored, so a
newer `cloudinfra` never breaks this service. This module never imports `cloudinfra`."""
import os

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import DeliveryError, ErrorCode

IAP_RANGE = "35.235.240.0/20"  # Google's Identity-Aware Proxy TCP forwarding range
# What a value read from cloudinfra's answer may look like before it reaches gcloud's arguments, a TLS endpoint or a
# Google URL (review of 2026-10-30, L11): an answer is data, and a value of another shape is refused, not passed on.
RESOURCE_NAME = r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
# a Google zone (us-central1-a), or an Azure availability zone (1, 2, 3)
ZONE = r"^(?:[a-z]+-[a-z]+[0-9]+-[a-z]|[1-9])$"
# a Google region (us-central1), or an Azure one (canadacentral, westus2)
REGION = r"^(?:[a-z]+-[a-z]+[0-9]+|[a-z][a-z0-9]{2,40})?$"
ENDPOINT = r"^(?:[0-9]{1,3}(?:\.[0-9]{1,3}){3}|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})$"
BASE64 = r"^[A-Za-z0-9+/]+={0,2}$"
# Azure Resource Manager ids as cloudinfra's Azure adapter writes them
ARM_GROUP = r"/subscriptions/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/resourceGroups/[A-Za-z0-9._()-]{1,90}"
AZURE_VM = rf"^{ARM_GROUP}/providers/Microsoft\.Compute/virtualMachines/[A-Za-z0-9][A-Za-z0-9-]{{0,63}}$"
AZURE_APP = rf"{ARM_GROUP}/providers/Microsoft\.App/(?:containerApps|jobs)/[a-z](?:[a-z0-9-]{{0,30}}[a-z0-9])?"
AZURE_IDENTITY = rf"^{ARM_GROUP}/providers/Microsoft\.ManagedIdentity/userAssignedIdentities/[A-Za-z0-9][A-Za-z0-9_-]{{1,127}}$"
# a Cloud Run service or job, or an Azure Container App or Container Apps job
RUN_NAME = rf"^(?:projects/[a-z][a-z0-9-]{{4,28}}[a-z0-9]/locations/[a-z0-9-]+/(?:services|jobs)/[a-z](?:[a-z0-9-]{{0,61}}[a-z0-9])?|{AZURE_APP})$"


class View(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Grant(View):
    member: str
    level: str
    status: str
    resources: list[str] = Field(default_factory=list)


class FirewallRule(View):
    direction: str = "ingress"
    deny: bool = False
    ports: list[int] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class Network(View):
    id: str
    name: str
    status: str
    cidr: str | None = None
    nat: bool = False
    firewall_rules: list[FirewallRule] = Field(default_factory=list)

    def _ingress(self) -> list[FirewallRule]:
        return [rule for rule in self.firewall_rules if rule.direction == "ingress" and not rule.deny]

    def admits(self, port: int, source: str) -> bool:
        return any(source in rule.sources and (not rule.ports or port in rule.ports) for rule in self._ingress())

    def opens_beyond_subnet(self, port: int, reach: str = IAP_RANGE) -> bool:
        """Some rule opens the port to sources other than the network's own subnet and the range sessions come from (IAP's,
        or the Bastion's)."""
        return any((not rule.ports or port in rule.ports) and set(rule.sources) - {self.cidr, reach} for rule in self._ingress())


class Machine(View):
    id: str
    name: str = Field(pattern=RESOURCE_NAME)
    status: str
    provider: str | None = None
    provider_id: str | None = Field(default=None, description="The machine's name at its cloud; on Azure its resource id, which reaches it through the "
                                                              "Bastion (checked against AZURE_VM before it does).")
    group: str = "vm"
    zone: str | None = Field(default=None, pattern=ZONE)
    os_image: str = ""
    profile: str | None = None
    network: str | None = None
    public_ip: str | None = None
    internal_ip: str | None = None
    instance_id: str | None = None
    autostop_idle_minutes: int | None = None
    max_run_hours: int | None = None


# the secrets cloudinfra makes for a request's own credentials, by purpose, and the short names a release binds them by
OWN_SECRETS = {"database_password": "db-password", "cache_auth": "cache-auth"}


class Secret(View):
    id: str
    name: str
    status: str
    resource_name: str
    provider_id: str | None = None
    purpose: str | None = None


class FrontDoor(View):
    address: str | None = None
    hostnames: list[str] = Field(default_factory=list)
    gateway: str
    certificate: str | None = None


class Cluster(View):
    id: str
    name: str = Field(pattern=RESOURCE_NAME)
    status: str
    provider: str | None = None
    location: str = Field(default="", pattern=r"^(?:[a-z]+-[a-z]+[0-9]+(?:-[a-z])?|[a-z][a-z0-9]{2,40})?$")
    endpoint: str | None = Field(default=None, pattern=ENDPOINT)
    ca_certificate: str | None = Field(default=None, pattern=BASE64)
    namespace: str | None = Field(default=None, pattern=RESOURCE_NAME)
    public: FrontDoor | None = None


class Service(View):
    """A managed container service (Cloud Run, Container Apps): where it is, its address, the account it runs as, and what serves now."""
    id: str
    name: str = Field(pattern=RESOURCE_NAME)
    status: str
    provider: str | None = None
    provider_id: str | None = Field(default=None, pattern=RUN_NAME)
    region: str = Field(default="", pattern=REGION)
    url: str | None = None
    runtime_service_account: str | None = None
    revision: str | None = None
    secrets: list[str] = Field(default_factory=list, description="The secrets its runtime account may read: resource names on Google, resource ids on Azure.")


class Job(View):
    """A managed job (Cloud Run job, Container Apps job): where it is, and the account its tasks run as."""
    id: str
    name: str = Field(pattern=RESOURCE_NAME)
    status: str
    provider: str | None = None
    provider_id: str | None = Field(default=None, pattern=RUN_NAME)
    region: str = Field(default="", pattern=REGION)
    runtime_service_account: str | None = None
    schedule: dict[str, str] | None = None
    secrets: list[str] = Field(default_factory=list, description="The secrets its runtime account may read: resource names on Google, resource ids on Azure.")


class Resources(View):
    network: list[Network] = Field(default_factory=list)
    compute: list[Machine] = Field(default_factory=list)
    secret: list[Secret] = Field(default_factory=list)
    cluster: list[Cluster] = Field(default_factory=list)
    service: list[Service] = Field(default_factory=list)
    job: list[Job] = Field(default_factory=list)


class InfraRequest(View):
    """A cloudinfra request: who owns it, its state, who may act on it, and the resources a release can use."""
    request_id: str
    name: str
    status: str
    owner: str | None = None
    access: list[Grant] = Field(default_factory=list)
    endpoints: dict = Field(default_factory=dict)
    resources: Resources = Field(default_factory=Resources)

    def group(self, name: str) -> list[Machine]:
        return [machine for machine in self.resources.compute if machine.group == name and machine.status != "DELETED"]

    def groups(self) -> list[str]:
        return sorted({machine.group for machine in self.resources.compute if machine.status != "DELETED"})

    def clusters(self) -> list[Cluster]:
        return [cluster for cluster in self.resources.cluster if cluster.status != "DELETED"]

    def cluster_name(self, resource) -> str:
        """The name the request's cluster or service block gave it: cloudinfra names it <request name>-<name>."""
        return resource.name.removeprefix(f"{self.name}-")

    def cluster(self, name: bool | str = True) -> Cluster | None:
        """A cluster by its name in the request, or (true) the request's only one."""
        found = self.clusters()
        if name is True:
            return found[0] if len(found) == 1 else None
        return next((cluster for cluster in found if self.cluster_name(cluster) == name), None)

    def jobs(self) -> list[Job]:
        return [job for job in self.resources.job if job.status != "DELETED"]

    def job(self, name: bool | str = True) -> Job | None:
        found = self.jobs()
        if name is True:
            return found[0] if len(found) == 1 else None
        return next((job for job in found if self.cluster_name(job) == name), None)

    def services(self) -> list[Service]:
        return [service for service in self.resources.service if service.status != "DELETED"]

    def service(self, name: bool | str = True) -> Service | None:
        """A service by the name its block gave it (cloudinfra names it <request name>-<name>), or (true) the only one."""
        found = self.services()
        if name is True:
            return found[0] if len(found) == 1 else None
        return next((service for service in found if self.cluster_name(service) == name), None)

    def network(self, network_id: str | None) -> Network | None:
        return next((network for network in self.resources.network if network.id == network_id), None)

    def _short_name(self, secret: Secret) -> str:
        """The name a release uses: cloudinfra names a secret <request name>-<its block's name>, and the database's and cache's
        own <request name>-db-password-<6 hex> and <request name>-cache-auth-<6 hex> (since 1.37.0), known by their purpose;
        the delivery service still looked for <request name>-db-password, so the documented binding failed (2026-11-02)."""
        return OWN_SECRETS.get(secret.purpose or "") or secret.name.removeprefix(f"{self.name}-")

    def secret(self, short_name: str) -> Secret | None:
        """The request's secret by the short name a release uses (db-password, cache-auth, or one the request named)."""
        return next((secret for secret in self.resources.secret if self._short_name(secret) == short_name and secret.status != "DELETED"), None)

    def secret_names(self) -> list[str]:
        return sorted(self._short_name(secret) for secret in self.resources.secret if secret.status != "DELETED")

    def grant_of(self, member: str) -> Grant | None:
        return next((grant for grant in self.access if grant.member.lower() == member.lower()), None)

    def readable_by(self, runtime, secret: Secret) -> bool:
        """A service's or job's runtime identity may read the secret: cloudinfra names it by resource name on Google and by
        resource id on Azure."""
        return secret.resource_name in runtime.secrets or (secret.provider_id is not None and secret.provider_id in runtime.secrets)


class InfraClient:
    """Reads requests from the infrastructure layer. A missing request, a refusal and an unreachable layer each have a code."""

    def __init__(self, url: str, token: str, client: httpx.Client | None = None, timeout: int = 30):
        self.url, self.token = url.rstrip("/"), token
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)

    @classmethod
    def for_environment(cls, config, environment: str, client: httpx.Client | None = None) -> "InfraClient":
        name = config.environments[environment].token_env
        token = os.environ.get(name)
        if not token:
            raise DeliveryError(ErrorCode.INFRA_AUTH_FAILED, f"No cloudinfra token for environment {environment}: set {name}", {"environment": environment})
        return cls(config.cloudinfra.url, token, client, config.cloudinfra.timeout_seconds)

    def get_request(self, request_id: str) -> InfraRequest:
        try:
            response = self.client.get(f"{self.url}/infrastructure/{request_id}", headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError as exc:
            raise DeliveryError(ErrorCode.INFRA_UNAVAILABLE, f"The infrastructure layer did not answer: {type(exc).__name__}", {"url": self.url}) from None
        if response.status_code in (401, 403):
            raise DeliveryError(ErrorCode.INFRA_AUTH_FAILED, "The infrastructure layer refused this service's token for the environment", {"status": response.status_code})
        if response.status_code == 404:
            # cloudinfra answers NOT_FOUND for a request this principal may not see, too
            raise DeliveryError(ErrorCode.NOT_FOUND, f"Request {request_id} does not exist in the infrastructure layer", {"request_id": request_id})
        if response.status_code >= 400:
            raise DeliveryError(ErrorCode.INFRA_UNAVAILABLE, f"The infrastructure layer answered HTTP {response.status_code}", {"status": response.status_code})
        try:
            return InfraRequest.model_validate(response.json())
        except (ValueError, ValidationError):
            raise DeliveryError(ErrorCode.INFRA_UNAVAILABLE, "The infrastructure layer's answer was not a request this service understands") from None
