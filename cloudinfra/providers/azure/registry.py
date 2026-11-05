"""Image repositories behind the generic registry primitives (docs/research/RESEARCH-2026-11-03-azure.md, section 5.4).

A request's registry is a repository prefix in the operator's registry (providers.azure.registry), not a registry of its
own: <name>-<registry>-<8 hex of the request id>, so that two requests of one name never share one. The registry's role
assignment mode is ABAC repository permissions, and every grant is conditioned to the prefix: members pull (Container
Registry Repository Reader), and only the operator's registry writers push (Repository Writer), as on Google. Nothing is
made at Resource Manager: a repository exists once an image is pushed under the prefix. Deleting the registry deletes
every repository under the prefix through the registry's own API, with a registry token the service gets by exchanging
its own (learn.microsoft.com/azure/container-registry/container-registry-authentication). Tags are made immutable one by
one after a push, which is the delivery service's to do on Azure; until it does, a request asking for immutable tags is
refused rather than given mutable ones.
"""
from urllib.parse import parse_qs, quote, urlparse

from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import RegistryResource, ResourceStatus
from ...observability.logging import event
from . import translate
from .api import API_VERSIONS

VERSION = API_VERSIONS["Microsoft.ContainerRegistry"]
PAGE = 1000
MAX_PAGES = 100


def prefix_of(planned_name: str, request_id: str) -> str:
    return f"{planned_name}-{request_id[:8]}".lower()


def registry_of(resource: RegistryResource) -> str:
    """The registry a request's prefix was made in, from the resource itself: the operator may have named another since."""
    return resource.provider_id.split("/repositories/", 1)[0]


def _next_last(link: str | None) -> str | None:
    """The `last` of the registry's next page, from its Link header (</v2/_catalog?last=x&n=100>; rel="next")."""
    if not link or 'rel="next"' not in link:
        return None
    query = parse_qs(urlparse(link.split(";", 1)[0].strip().strip("<>")).query)
    return (query.get("last") or [None])[0]


class AzureRegistry:
    def __init__(self, api, settings, cloud, tenant):
        self.api, self.settings, self.cloud, self.tenant = api, settings, cloud, tenant

    def _registry(self, registry_id: str) -> dict | None:
        return self.api.find(self.api.arm(registry_id), VERSION)

    @staticmethod
    def login_server(registry: dict) -> str:
        """The registry's own address, as Azure answers it (a name with a label Azure made has more than the name)."""
        server = (registry.get("properties") or {}).get("loginServer")
        if not server:
            raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Azure gave no login server for the registry {registry.get('name')}")
        return server.lower()

    def create(self, spec, planned, ctx) -> RegistryResource:
        if not self.settings.registry:
            raise InfrastructureError(ErrorCode.INVALID_REQUEST, "This operator names no container registry (providers.azure.registry)")
        prefix = prefix_of(planned.name, ctx.request_id)
        resource = RegistryResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="azure", region=ctx.region,
                                    provider_id=f"{self.settings.registry}/repositories/{prefix}", repository=prefix, address=f"pending/{prefix}",
                                    immutable_tags=spec.immutable_tags, size_gb=spec.size_gb, status=ResourceStatus.CREATING, labels=translate.tags(ctx))
        ctx.accepted(resource)
        registry = self._registry(self.settings.registry)
        if registry is None:
            raise InfrastructureError(ErrorCode.NOT_FOUND, f"The operator's registry {self.settings.registry.rsplit('/', 1)[-1]} does not exist", resource=planned.name)
        return resource.model_copy(update={"status": ResourceStatus.READY, "address": f"{self.login_server(registry)}/{prefix}"})

    def get(self, resource: RegistryResource) -> RegistryResource:
        found = self._registry(registry_of(resource))
        return resource.model_copy(update={"status": ResourceStatus.READY if found is not None else ResourceStatus.DELETED})

    # ---- the registry's own API
    def _token(self, login: str, scope: str) -> str:
        """A registry access token for one scope: the service's own token, for the registry's audience, exchanged for a
        registry refresh token, then for an access token (the OAuth2 flow of github.com/Azure/acr/blob/main/docs/AAD-OAuth.md).
        The registry's audience, not Resource Manager's: a registry may refuse a Resource Manager token by policy."""
        own = self.api.token(self.cloud.container_registry or self.cloud.arm_audience)
        refresh = (self.api.call("POST", f"https://{login}/oauth2/exchange", data={"grant_type": "access_token", "service": login, "tenant": self.tenant(),
                                                                                  "access_token": own}, bearer="").body or {}).get("refresh_token")
        if not refresh:
            raise InfrastructureError(ErrorCode.PROVIDER_AUTH_FAILED, f"The registry {login} gave the service no refresh token")
        access = (self.api.call("POST", f"https://{login}/oauth2/token", data={"grant_type": "refresh_token", "service": login, "scope": scope,
                                                                               "refresh_token": refresh}, bearer="").body or {}).get("access_token")
        if not access:
            raise InfrastructureError(ErrorCode.PROVIDER_AUTH_FAILED, f"The registry {login} gave the service no access token for {scope}")
        return access

    def repositories(self, login: str, prefix: str) -> list[str]:
        """The repositories under a prefix, from the registry's catalog: listed from the prefix on, page after page as the
        registry's Link header says, until a name past the prefix."""
        token, found, last = self._token(login, "registry:catalog:*"), [], prefix
        for _ in range(MAX_PAGES):
            answer = self.api.call("GET", f"https://{login}/v2/_catalog", params={"n": str(PAGE), "last": last}, bearer=token)
            page = (answer.body or {}).get("repositories") or []
            found.extend(name for name in page if name.lower().startswith(prefix + "/"))
            following = _next_last(answer.headers.get("link"))
            if not page or following is None or page[-1].lower() > prefix + "/~":  # sorted: nothing of the prefix after a name past it
                return found
            last = following
        raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"The registry's catalog did not end after {MAX_PAGES} pages")

    def delete(self, resource: RegistryResource, ctx) -> None:
        """Delete every repository under the prefix, and every image in each: deleting a registry deletes its images."""
        registry = self._registry(registry_of(resource))
        if registry is None:
            return
        login = self.login_server(registry)
        for repository in self.repositories(login, resource.repository):
            token = self._token(login, f"repository:{repository}:delete")
            try:
                self.api.call("DELETE", f"https://{login}/acr/v1/{quote(repository, safe='/')}", bearer=token)
                event("repository_deleted", registry=login, repository=repository)
            except InfrastructureError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
