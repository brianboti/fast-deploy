"""Azure credentials and reads (docs/research/RESEARCH-2026-11-03-azure.md, section 7). Every action a release takes on
Azure is done as the environment's own user-assigned managed identity, attached to this service's host: its tokens come
from the host's managed identity endpoint (the instance metadata service on a machine, IDENTITY_ENDPOINT on Container
Apps or App Service), asked for by the identity's client id. No key, secret or certificate is read from a file. Tokens
live in memory until two minutes before they end and are never written, logged or returned.

The public Azure cloud only, for now: the audiences and hosts below are its own."""
import os
import re
import time
from urllib.parse import urlparse

import httpx

from .errors import DeliveryError, ErrorCode

IMDS = "http://169.254.169.254/metadata/identity/oauth2/token"
IMDS_VERSION = "2018-02-01"
APP_IDENTITY_VERSION = "2019-08-01"  # IDENTITY_ENDPOINT on Container Apps, App Service and Functions
# The audiences a token is asked for
ARM = "https://management.azure.com/"
KEY_VAULT = "https://vault.azure.net"
REGISTRY = "https://containerregistry.azure.net"  # a registry token is exchanged for one of these (ACR's own audience)
AKS = "6dae42f8-4368-4678-94ff-3960e28e3630"  # AKS's Entra server application, the audience of a cluster's API (Azure RBAC)
ARM_HOST = "management.azure.com"
KEY_VAULT_VERSION = "7.5"  # a vault's own API, as cloudinfra calls it
# A secret as cloudinfra's Azure adapter names it (its resource_name), and an Azure registry's login server: checked before
# a token is sent to either, so an answer of another shape never carries one elsewhere
SECRET_URL = re.compile(r"^https://(?P<vault>[a-z0-9](?:[a-z0-9-]{1,22}[a-z0-9]))\.vault\.azure\.net/secrets/(?P<name>[A-Za-z0-9-]{1,127})$")
LOGIN_SERVER = re.compile(r"^[a-z0-9]{5,50}\.azurecr\.io$")
REGISTRY_USER = "00000000-0000-0000-0000-000000000000"  # an Azure registry's refresh token logs in with the nil GUID
EARLY = 120  # a cached token is dropped this many seconds before it ends
LOOPBACK = ("localhost", "127.0.0.1", "::1")


class ManagedIdentityTokens:
    """Tokens of the managed identities attached to this host, by client id and audience."""

    def __init__(self, client: httpx.Client, environ=None, clock=time.time):
        self.client, self.environ, self.clock = client, os.environ if environ is None else environ, clock
        self.cache: dict[tuple[str, str], tuple[str, float]] = {}

    def __call__(self, client_id: str, resource: str) -> str:
        cached = self.cache.get((client_id, resource))
        if cached and self.clock() < cached[1]:
            return cached[0]
        token, until = self.mint(client_id, resource)
        self.cache[(client_id, resource)] = (token, until)
        return token

    def _endpoint(self) -> tuple[str, dict[str, str], str]:
        """(address, headers, api-version) of this host's managed identity endpoint. IDENTITY_ENDPOINT is honoured only on the
        host's own loopback: the header that goes with it is a credential of the host."""
        endpoint, header = self.environ.get("IDENTITY_ENDPOINT"), self.environ.get("IDENTITY_HEADER")
        if endpoint and header:
            parsed = urlparse(endpoint)
            if parsed.scheme != "http" or parsed.hostname not in LOOPBACK:
                raise DeliveryError(ErrorCode.AZURE_AUTH_FAILED, "IDENTITY_ENDPOINT is not on this host's loopback; this service asks only the host's own "
                                    "managed identity endpoint for tokens")
            return endpoint, {"X-IDENTITY-HEADER": header}, APP_IDENTITY_VERSION
        return IMDS, {"Metadata": "true"}, IMDS_VERSION

    def mint(self, client_id: str, resource: str) -> tuple[str, float]:
        address, headers, version = self._endpoint()
        try:
            response = self.client.get(address, params={"api-version": version, "resource": resource, "client_id": client_id}, headers=headers, timeout=10)
        except httpx.HTTPError as exc:
            raise DeliveryError(ErrorCode.AZURE_AUTH_FAILED, f"This host's managed identity endpoint did not answer ({type(exc).__name__}): run the service on "
                                "Azure with the environment's managed identity attached", {"client_id": client_id}) from None
        if response.status_code != 200:
            raise DeliveryError(ErrorCode.AZURE_AUTH_FAILED, f"This host may not act as the managed identity {client_id} (HTTP {response.status_code}): attach "
                                "it to the host the service runs on", {"client_id": client_id, "status": response.status_code})
        try:
            data = response.json()
            token, ends = data["access_token"], float(data.get("expires_on") or self.clock() + float(data.get("expires_in") or 300))
        except (ValueError, KeyError, TypeError):
            raise DeliveryError(ErrorCode.AZURE_AUTH_FAILED, "This host's managed identity endpoint returned no token", {"client_id": client_id}) from None
        return token, max(self.clock() + 30, ends - EARLY)


def secret_url(resource_name: str) -> str:
    """The secret's address, refused unless it is a Key Vault secret of the public cloud, without a version."""
    if not SECRET_URL.fullmatch(resource_name or ""):
        raise DeliveryError(ErrorCode.SECRET_UNAVAILABLE, "The infrastructure layer named a secret that is not a Key Vault secret's address; nothing was sent to it",
                            {"secret": (resource_name or "")[:200]})
    return resource_name


def _vault(client: httpx.Client, token: str, url: str, what: str) -> httpx.Response:
    try:
        response = client.get(url, params={"api-version": KEY_VAULT_VERSION}, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise DeliveryError(ErrorCode.SECRET_UNAVAILABLE, f"Key Vault did not answer: {type(exc).__name__}", {"secret": what}) from None
    if response.status_code in (401, 403, 404):
        raise DeliveryError(ErrorCode.SECRET_UNAVAILABLE, f"{what} cannot be read as this environment's managed identity (HTTP {response.status_code}); it needs "
                            "the request's access grant", {"secret": what, "status": response.status_code})
    if response.status_code != 200:
        raise DeliveryError(ErrorCode.SECRET_UNAVAILABLE, f"Key Vault answered HTTP {response.status_code}", {"secret": what, "status": response.status_code})
    return response


def read_secret(client: httpx.Client, token: str, resource_name: str) -> str:
    """The current version of a Key Vault secret, read as the identity whose token this is; returned to the caller only."""
    url = secret_url(resource_name)
    response = _vault(client, token, url, resource_name)
    try:
        value = response.json()["value"]
    except (ValueError, KeyError):
        raise DeliveryError(ErrorCode.SECRET_UNAVAILABLE, f"{resource_name} has no value", {"secret": resource_name}) from None
    if not isinstance(value, str):
        raise DeliveryError(ErrorCode.SECRET_UNAVAILABLE, f"{resource_name}'s value is not text", {"secret": resource_name})
    return value


def secret_version(client: httpx.Client, token: str, resource_name: str) -> str:
    """The newest enabled version of a Key Vault secret, from its version list, which carries no values: a reference is
    pinned to it. A secret cloudinfra made without a value holds only a disabled version, which no container could read."""
    url = secret_url(resource_name)
    versions, page, pages = [], f"{url}/versions", 0
    while page and pages < 20:
        pages += 1
        data = _vault(client, token, page, resource_name).json()
        versions += [item for item in data.get("value", []) if (item.get("attributes") or {}).get("enabled")]
        following = data.get("nextLink")
        page = following if following and urlparse(following).hostname == urlparse(url).hostname and urlparse(following).scheme == "https" else None
    if not versions:
        raise DeliveryError(ErrorCode.SECRET_UNAVAILABLE, f"{resource_name} has no enabled version: give it a value first", {"secret": resource_name})
    newest = max(versions, key=lambda item: (item.get("attributes") or {}).get("created") or 0)
    return str(newest.get("id", "")).rstrip("/").rsplit("/", 1)[-1]


def registry_token(client: httpx.Client, entra_token: str, login_server: str) -> str:
    """A refresh token of an Azure registry for the identity whose Entra token this is: what `docker login` takes as the
    password of the nil GUID. It carries that identity's rights on the registry and nothing else, and ends within hours."""
    if not LOGIN_SERVER.fullmatch(login_server):
        raise DeliveryError(ErrorCode.REGISTRY_NOT_ALLOWED, f"{login_server} is not an Azure registry's login server", {"registry": login_server})
    try:
        response = client.post(f"https://{login_server}/oauth2/exchange", data={"grant_type": "access_token", "service": login_server, "access_token": entra_token})
    except httpx.HTTPError as exc:
        raise DeliveryError(ErrorCode.AZURE_AUTH_FAILED, f"{login_server} did not answer the token exchange: {type(exc).__name__}", {"registry": login_server}) from None
    if response.status_code != 200:
        raise DeliveryError(ErrorCode.AZURE_AUTH_FAILED, f"{login_server} refused the pull identity's token (HTTP {response.status_code}): it needs AcrPull or "
                            "Repository Reader on the registry", {"registry": login_server, "status": response.status_code})
    try:
        return response.json()["refresh_token"]
    except (ValueError, KeyError):
        raise DeliveryError(ErrorCode.AZURE_AUTH_FAILED, f"{login_server} returned no registry token", {"registry": login_server}) from None
