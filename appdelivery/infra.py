"""The infrastructure layer as this service sees it: one read, `GET /infrastructure/{request_id}`, over HTTP with the
environment's read-only token. Only the fields a release needs are kept; fields the layer adds later are ignored, so a
newer `cloudinfra` never breaks this service. This module never imports `cloudinfra`."""
import os

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import DeliveryError, ErrorCode

IAP_RANGE = "35.235.240.0/20"  # Google's Identity-Aware Proxy TCP forwarding range


class View(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Grant(View):
    member: str
    level: str
    status: str
    resources: list[str] = Field(default_factory=list)


class FirewallRule(View):
    ports: list[int] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class Network(View):
    id: str
    name: str
    status: str
    cidr: str | None = None
    firewall_rules: list[FirewallRule] = Field(default_factory=list)
    nat_router: str | None = None  # outbound internet for machines without a public address

    def admits(self, port: int, source: str) -> bool:
        return any(source in rule.sources and (not rule.ports or port in rule.ports) for rule in self.firewall_rules)

    def opens_beyond_subnet(self, port: int) -> bool:
        """Some rule opens the port to sources other than the network's own subnet and the IAP range."""
        return any((not rule.ports or port in rule.ports) and set(rule.sources) - {self.cidr, IAP_RANGE} for rule in self.firewall_rules)


class Machine(View):
    id: str
    name: str
    status: str
    group: str = "vm"
    zone: str | None = None
    os_image: str = ""
    profile: str | None = None
    network: str | None = None
    public_ip: str | None = None
    internal_ip: str | None = None
    instance_id: str | None = None
    autostop_idle_minutes: int | None = None
    max_run_hours: int | None = None


class Secret(View):
    id: str
    name: str
    status: str
    resource_name: str
    purpose: str | None = None


class FrontDoor(View):
    address: str | None = None
    hostnames: list[str] = Field(default_factory=list)
    gateway: str
    certificate: str | None = None
    security_policy: str | None = None


class Cluster(View):
    id: str
    name: str
    status: str
    location: str = ""
    endpoint: str | None = None
    ca_certificate: str | None = None
    namespace: str | None = None
    public: FrontDoor | None = None


class Service(View):
    """A managed container service (Cloud Run): where it is, its address, the account it runs as, and what serves now."""
    id: str
    name: str
    status: str
    provider_id: str | None = None
    region: str = ""
    url: str | None = None
    runtime_service_account: str | None = None
    revision: str | None = None


class Job(View):
    """A managed job (Cloud Run job): where it is, and the account its tasks run as."""
    id: str
    name: str
    status: str
    provider_id: str | None = None
    region: str = ""
    runtime_service_account: str | None = None
    schedule: dict[str, str] | None = None


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

    def secret(self, short_name: str) -> Secret | None:
        """The request's secret by the short name a release uses: cloudinfra names it <request name>-<short name>."""
        full = f"{self.name}-{short_name}"
        return next((secret for secret in self.resources.secret if secret.name == full and secret.status != "DELETED"), None)

    def secret_names(self) -> list[str]:
        prefix = f"{self.name}-"
        return sorted(secret.name.removeprefix(prefix) for secret in self.resources.secret if secret.status != "DELETED")

    def grant_of(self, member: str) -> Grant | None:
        return next((grant for grant in self.access if grant.member.lower() == member.lower()), None)


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
