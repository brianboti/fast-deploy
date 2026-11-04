"""Provider authentication for Azure, isolated from every caller of the service.

The service holds no key and no client secret. It takes a token from the host it runs on, from the first of these the
host offers, and remembers which one answered:

1. a pod's workload identity on AKS: the projected Kubernetes token in AZURE_FEDERATED_TOKEN_FILE, exchanged with
   Entra ID for the identity in AZURE_CLIENT_ID (learn.microsoft.com/azure/aks/workload-identity-overview);
2. the managed identity of a Container Apps or App Service host, through the local endpoint in IDENTITY_ENDPOINT
   (learn.microsoft.com/azure/container-apps/managed-identity);
3. the managed identity of a virtual machine, through the instance metadata service
   (learn.microsoft.com/entra/identity/managed-identities-azure-resources/how-to-use-vm-token);
4. else the operator's own Azure CLI sign-in on a laptop (`az account get-access-token`).

Azure issues a token for one audience at a time: one for Resource Manager is refused by a key vault, by Microsoft Graph
and by a cluster's API server. So tokens are kept per audience, each until a minute before it ends, in memory only;
none is ever written, logged or returned, and no error message carries one.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import httpx

from .clouds import AzureCloud

METADATA_URL = "http://169.254.169.254/metadata/identity/oauth2/token"
METADATA_VERSION = "2018-02-01"
IDENTITY_ENDPOINT_VERSION = "2019-08-01"
JWT_BEARER = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
PROBE_SECONDS = 1.0  # a laptop has no metadata service, and the first try must fail fast there
# Microsoft's advice for the metadata service: try again after a 404 (it is being updated), a 410 (back within 70
# seconds), a 429 and a 5xx, about five times with growing pauses; never after another 4xx.
METADATA_RETRY = frozenset({404, 410, 429, 500, 502, 503, 504})
METADATA_PAUSES = (2, 6, 14, 30)
WORKLOAD, IDENTITY_ENDPOINT, METADATA, CLI = "workload identity", "identity endpoint", "metadata service", "Azure CLI"
TENANT = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def scope_of(audience: str) -> str:
    """The Entra ID v2 scope of a v1 resource: https://management.azure.com/ is asked for as https://management.azure.com/.default."""
    return audience.rstrip("/") + "/.default"


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _error_of(response: httpx.Response) -> str:
    """The short error code an identity service answered with (invalid_request, AADSTS70021…), never its whole body."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    codes = body.get("error_codes")
    return str(body.get("error") or "")[:80] + (f" (AADSTS{codes[0]})" if isinstance(codes, list) and codes else "")


class AzureToken:
    """An access token for one Azure audience as this host itself; called with the audience (Resource Manager's when
    omitted). A caller Azure refuses with 401 invalidates that audience's token and asks again."""

    def __init__(self, cloud: AzureCloud, client=None, runner=None, clock=time.time, sleep=time.sleep, environ=None, client_id: str | None = None,
                 tenant_id: str | None = None, read=None):
        self.cloud = cloud
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.runner = runner or (lambda args: subprocess.run(args, check=True, capture_output=True, text=True, timeout=60).stdout)
        self.clock, self.sleep = clock, sleep
        self.environ = os.environ if environ is None else environ
        self.client_id, self.tenant_id = client_id, tenant_id
        self.read = read or (lambda path: Path(path).read_text().strip())
        self.source: str | None = None
        self.cache: dict[str, tuple[str, float]] = {}

    def __call__(self, audience: str | None = None) -> str:
        audience = audience or self.cloud.arm_audience
        kept = self.cache.get(audience)
        if kept and self.clock() < kept[1]:
            return kept[0]
        token, expires_on = self._fetch(audience)
        self.cache[audience] = (token, expires_on - 60)
        return token

    def invalidate(self, audience: str | None = None) -> None:
        """Forget one audience's token: Azure refused it."""
        self.cache.pop(audience or self.cloud.arm_audience, None)

    # ---- where the token comes from
    def _fetch(self, audience: str) -> tuple[str, float]:
        if self.source is None:
            first = self._discover(audience)
            if first is not None:
                return first
        return {WORKLOAD: self._workload, IDENTITY_ENDPOINT: self._identity_endpoint, METADATA: self._metadata, CLI: self._cli}[self.source](audience)

    def _discover(self, audience: str) -> tuple[str, float] | None:
        """Which of the host's identities answers. The metadata service's answer to the probe is the first token.

        Something answers at the metadata address on other clouds too (Google's metadata server does), so the service is
        taken for Azure's only when it gives a token, or refuses with Azure's own JSON error shape."""
        env = self.environ
        if env.get("AZURE_FEDERATED_TOKEN_FILE") and env.get("AZURE_CLIENT_ID") and env.get("AZURE_TENANT_ID"):
            self.source = WORKLOAD
            return None
        if env.get("IDENTITY_ENDPOINT") and env.get("IDENTITY_HEADER"):
            self.source = IDENTITY_ENDPOINT
            return None
        try:
            response = self.client.get(METADATA_URL, params=self._metadata_params(audience), headers={"Metadata": "true"}, timeout=PROBE_SECONDS)
        except httpx.HTTPError:
            response = None
        if response is not None and response.status_code == 200:
            self.source = METADATA
            return self._token_of(response)
        if response is not None and _error_of(response):
            self.source = METADATA
            return None
        self.source = CLI
        return None

    def _metadata_params(self, audience: str) -> dict:
        return {"api-version": METADATA_VERSION, "resource": audience, **({"client_id": self.client_id} if self.client_id else {})}

    def _metadata(self, audience: str) -> tuple[str, float]:
        answer = "no answer"
        for attempt in range(len(METADATA_PAUSES) + 1):
            try:
                response = self.client.get(METADATA_URL, params=self._metadata_params(audience), headers={"Metadata": "true"}, timeout=10)
            except httpx.HTTPError as exc:
                response, answer = None, type(exc).__name__
            if response is not None:
                if response.status_code == 200:
                    return self._token_of(response)
                answer = f"{response.status_code} {_error_of(response)}".strip()
                if response.status_code not in METADATA_RETRY:
                    raise ValueError(f"This machine's metadata service refused a token for {audience} ({answer}): give the machine a managed identity, "
                                     "and name it in providers.azure.identity_client_id when the machine has several")
            if attempt < len(METADATA_PAUSES):
                self.sleep(METADATA_PAUSES[attempt])
        raise ValueError(f"This machine's metadata service gave no token for {audience} after {len(METADATA_PAUSES) + 1} tries ({answer})")

    def _identity_endpoint(self, audience: str) -> tuple[str, float]:
        env = self.environ
        params = {"api-version": IDENTITY_ENDPOINT_VERSION, "resource": audience, **({"client_id": self.client_id} if self.client_id else {})}
        try:
            response = self.client.get(env["IDENTITY_ENDPOINT"], params=params, headers={"X-IDENTITY-HEADER": env["IDENTITY_HEADER"]}, timeout=10)
        except httpx.HTTPError as exc:
            raise ValueError(f"The host's managed identity endpoint could not be reached ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise ValueError(f"The host's managed identity endpoint refused a token for {audience} ({response.status_code} {_error_of(response)})".rstrip())
        return self._token_of(response)

    def _workload(self, audience: str) -> tuple[str, float]:
        env = self.environ
        tenant = env["AZURE_TENANT_ID"]
        if not TENANT.fullmatch(tenant):
            raise ValueError("AZURE_TENANT_ID is not a directory id")
        authority = (env.get("AZURE_AUTHORITY_HOST") or self.cloud.login).rstrip("/")
        try:
            assertion = self.read(env["AZURE_FEDERATED_TOKEN_FILE"])  # read every time: Kubernetes rotates it
        except OSError:
            raise ValueError("The pod's federated token file could not be read") from None
        form = {"grant_type": "client_credentials", "client_id": env["AZURE_CLIENT_ID"], "scope": scope_of(audience),
                "client_assertion_type": JWT_BEARER, "client_assertion": assertion}
        try:
            response = self.client.post(f"{authority}/{tenant}/oauth2/v2.0/token", data=form, timeout=30)
        except httpx.HTTPError as exc:
            raise ValueError(f"Entra ID could not be reached to exchange the pod's token ({type(exc).__name__})") from None
        if response.status_code != 200:
            # AADSTS70021 just after the federated credential is made: Entra ID knows it seconds later
            raise ValueError(f"Entra ID refused the pod's workload identity a token for {audience} ({response.status_code} {_error_of(response)})".rstrip())
        return self._token_of(response)

    def _cli(self, audience: str) -> tuple[str, float]:
        command = ["az", "account", "get-access-token", "--resource", audience, "--output", "json"]
        if self.tenant_id:
            command += ["--tenant", self.tenant_id]
        try:
            data = json.loads(self.runner(command))
            token = data["accessToken"]
        except (subprocess.CalledProcessError, subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
            raise ValueError("Could not get an Azure token on this host: on a laptop, sign in with az login; on Azure, give the host a managed identity") from None
        expires_on = _number(data.get("expires_on"))  # POSIX seconds, since Azure CLI 2.54.0
        return token, expires_on if expires_on is not None else self.clock() + 300

    def _token_of(self, response: httpx.Response) -> tuple[str, float]:
        try:
            data = response.json()
            token = data["access_token"]
        except (ValueError, KeyError, TypeError):
            raise ValueError("An Azure identity service answered without a token") from None
        expires_on = _number(data.get("expires_on"))
        if expires_on is None:
            expires_in = _number(data.get("expires_in"))
            expires_on = self.clock() + (expires_in if expires_in is not None else 300)
        return token, expires_on
