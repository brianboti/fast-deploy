"""One thin REST client for Azure Resource Manager and the data planes the adapter calls: a bearer token per audience,
structured errors, retries Azure asks for, and the waits of long-running operations.

Every provider failure becomes an InfrastructureError with a code; the caller never sees an httpx exception or a raw
Azure error body. The client is the single seam the unit tests replace with a fake (tests/cloudinfra/azure_fakes.py).

What Azure's documents say, and this client does (docs/research/RESEARCH-2026-11-03-azure.md, section 3):
- Every write is idempotent by the name the caller chose: a PUT or a DELETE whose answer was lost is sent again, and a
  create that may have happened is found again with a GET of its own name.
- A long-running operation answers 201 or 202; it is followed through its Azure-AsyncOperation address, else its
  Location address, else the resource's own provisioningState, which ends Succeeded, Failed or Canceled
  (learn.microsoft.com/azure/azure-resource-manager/management/async-operations). Those addresses are followed only
  on the host the call went to, so a token never leaves for another.
- A 429 was refused before it ran and is tried again after Retry-After; a Retry-After longer than a worker should
  sleep (Compute may ask for 1,200 seconds) is reported instead, since a call sent earlier is not processed
  (learn.microsoft.com/azure/azure-resource-manager/management/request-limits-and-throttling).
- Another operation holding the resource (AnotherOperationInProgress, RetryableError…) refused the call, which is
  tried again after a longer pause.
"""
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from ...models.errors import ErrorCode, InfrastructureError
from ...observability.logging import event
from .clouds import CLOUDS, AzureCloud

# api-versions by resource type, the latest stable on each one's template reference page
# (learn.microsoft.com/azure/templates/<provider>/<type>) when read on 2026-11-04; each kind adds its own when it lands.
API_VERSIONS = {
    "Microsoft.Compute": "2026-04-01",  # virtual machines, their extensions, usages
    "Microsoft.Compute/disks": "2026-03-02",  # managed disks version apart from machines
    "Microsoft.Network": "2026-03-01",  # virtual networks, security groups, NAT gateways, public addresses, interfaces, flow logs, usages
    "Microsoft.Storage": "2026-09-01",  # storage accounts, their blob service and containers
    "Microsoft.ManagedIdentity": "2024-11-30",  # user-assigned identities
    "Microsoft.Resources": "2025-04-01",  # resource groups and the resources in one
    "Microsoft.Authorization": "2022-04-01",  # role assignments
    "Microsoft.Insights": "2023-10-01",  # metrics (learn.microsoft.com/rest/api/monitor/metrics/list)
    # step 4, read on each template reference page on 2026-11-05
    "Microsoft.KeyVault": "2026-05-15",  # vaults, and deletedVaults' purge
    "Microsoft.ServiceBus": "2026-01-01",  # namespaces, topics, subscriptions
    "Microsoft.ContainerRegistry": "2025-11-01",  # the operator's registry, read only
    "Microsoft.DBforPostgreSQL": "2026-10-01",  # flexible servers and their databases
    "Microsoft.DBforMySQL": "2024-12-30",  # flexible servers (8.0; 8.4 is only in a preview version) and their databases
    "Microsoft.Cache": "2026-09-01",  # Azure Managed Redis (redisEnterprise) and its database
    "Microsoft.Network/privateDnsZones": "2024-06-01",  # private DNS zones and their network links version apart from Microsoft.Network
    "Microsoft.Authorization/locks": "2020-05-01",  # management locks
}
KEY_VAULT_DATA_VERSION = "7.5"  # a vault's own API, for secret values (learn.microsoft.com/rest/api/keyvault)
STORAGE_DATA_VERSION = "2025-01-05"  # x-ms-version of a blob service's data plane
RETRIES = 5  # attempts of a call Azure refused for now, or of an idempotent call that met a 5xx or lost its answer
RETRY_PAUSE_SECONDS = 1.0  # doubled after each attempt, unless Azure says how long in Retry-After
BUSY_PAUSE_SECONDS = 10.0  # at least this long when another operation holds the resource
MAX_RETRY_PAUSE_SECONDS = 60  # a longer Retry-After is reported, not slept through
POLL_SECONDS = 10  # between two looks at a long-running operation, unless Azure says how long
MAX_POLL_SECONDS = 60
MAX_PAGES = 100
IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "DELETE"})
BUSY = frozenset({"AnotherOperationInProgress", "RetryableError", "RetryableErrorDueToAnotherOperation"})
DONE, FAILED = "succeeded", frozenset({"failed", "canceled"})
CAPACITY = frozenset({"AllocationFailed", "ZonalAllocationFailed", "OverconstrainedAllocationRequest", "OverconstrainedZonalAllocationRequest", "SkuNotAvailable"})
CONFLICT = frozenset({"Conflict", "NameInUse", "ScopeLocked", "StorageAccountAlreadyTaken", "VaultAlreadyExists", "RoleAssignmentExists"})
DENIED = frozenset({"AuthorizationFailed", "LinkedAuthorizationFailed", "RequestDisallowedByPolicy", "MarketplacePurchaseEligibilityFailed",
                    "VMMarketplaceInvalidInput"})


@dataclass
class AzureResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # names in lowercase
    body: dict | None = None
    text: str = ""  # an answer that is not JSON (a blob service lists in XML)


def _error(body) -> dict:
    """The error object of an Azure answer: {"error": {...}} for Resource Manager, the body itself for some data planes."""
    if isinstance(body, dict):
        if isinstance(body.get("error"), dict):
            return body["error"]
        if "code" in body and "message" in body:
            return body
    return {}


def _policies(error: dict) -> dict:
    """The policy assignment and definition behind a RequestDisallowedByPolicy, from the error's additionalInfo."""
    for item in error.get("additionalInfo") or []:
        info = item.get("info") if isinstance(item, dict) else None
        if isinstance(info, dict) and (info.get("policyAssignmentId") or info.get("policyDefinitionId")):
            return {key: info[key] for key in ("policyAssignmentId", "policyDefinitionId", "policyAssignmentName", "policyDefinitionName") if info.get(key)}
    return {}


def code_for(status: int | None, reason: str, message: str) -> ErrorCode:
    """The service's error code for an Azure refusal, by its HTTP status and Azure's own error code."""
    lowered = f"{reason} {message}".lower()
    if status == 401 or reason in ("AuthenticationFailed", "InvalidAuthenticationToken", "ExpiredAuthenticationToken"):
        return ErrorCode.PROVIDER_AUTH_FAILED
    if reason in DENIED:
        return ErrorCode.PROVIDER_PERMISSION_DENIED
    if "quota" in lowered or reason == "RoleAssignmentLimitExceeded":
        return ErrorCode.QUOTA_EXCEEDED
    if reason in CAPACITY:
        return ErrorCode.PROVIDER_UNAVAILABLE
    if status == 403:
        return ErrorCode.PROVIDER_PERMISSION_DENIED
    if status == 404 or reason in ("ResourceNotFound", "ResourceGroupNotFound", "NotFound"):
        return ErrorCode.NOT_FOUND
    if status in (409, 412) or reason in CONFLICT or reason.endswith("AlreadyExists") or reason.endswith("AlreadyTaken"):
        return ErrorCode.PROVIDER_CONFLICT
    if status == 429 or (status is not None and status >= 500):
        return ErrorCode.PROVIDER_UNAVAILABLE
    return ErrorCode.PROVIDER_ERROR


SENTENCES = {
    ErrorCode.PROVIDER_AUTH_FAILED: "Azure refused the service's credential; check the host's managed identity or the Azure CLI sign-in",
    ErrorCode.PROVIDER_PERMISSION_DENIED: "The service's identity is not allowed to do this in Azure",
    ErrorCode.QUOTA_EXCEEDED: "Azure quota exceeded",
    ErrorCode.NOT_FOUND: "Azure has no such resource",
    ErrorCode.PROVIDER_CONFLICT: "Azure reports a conflict",
    ErrorCode.PROVIDER_UNAVAILABLE: "Azure cannot do this now; retry later",
    ErrorCode.PROVIDER_ERROR: "Azure refused the request",
}


def translate_error(status: int | None, body, text: str = "") -> InfrastructureError:
    """An Azure refusal as a coded error; Azure's own code and sentence are kept in details."""
    error = _error(body)
    reason = str(error.get("code") or "")
    message = str(error.get("message") or text[:300])
    details = {"http_status": status, "reason": reason, "provider_message": message[:500]}
    if error.get("target"):
        details["target"] = str(error["target"])[:200]
    code = code_for(status, reason, message)
    if reason == "RequestDisallowedByPolicy":
        details.update(_policies(error))
        return InfrastructureError(code, f"An Azure Policy of the operator's refused this: {message[:200]}", details)
    if reason in CAPACITY:
        return InfrastructureError(code, f"Azure has no capacity for this size in this region or zone for this subscription now: {message[:200]}", details)
    return InfrastructureError(code, f"{SENTENCES[code]}: {message[:200]}", details)


def retry_after(headers, default: float | None = None) -> float | None:
    """Azure's Retry-After, in seconds, when it gives one as a number."""
    value = headers.get("retry-after") if isinstance(headers, dict) else headers.get("Retry-After")
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return default


def _json(response: httpx.Response):
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _state(body) -> str:
    properties = body.get("properties") if isinstance(body, dict) else None
    return str((properties or {}).get("provisioningState") or "").lower()


class AzureApi:
    def __init__(self, subscription_id: str, token, cloud: AzureCloud | None = None, client=None, sleep=time.sleep, clock=time.monotonic, operation_timeout: int = 900):
        self.subscription_id, self.token, self.sleep, self.clock, self.operation_timeout = subscription_id, token, sleep, clock, operation_timeout
        self.cloud = cloud or CLOUDS["public"]
        self.client = client or httpx.Client(timeout=60, follow_redirects=False)

    # ---- addresses
    def arm(self, path: str) -> str:
        return f"{self.cloud.resource_manager}{path}"

    def subscription(self, path: str = "") -> str:
        """An address under the operator's subscription: subscription("/resourceGroups/rg") ."""
        return self.arm(f"/subscriptions/{self.subscription_id}{path}")

    @staticmethod
    def _follow(url: str, origin: str) -> str:
        """An address Azure handed back (an operation to watch, the next page): only on the host the call went to, over https."""
        target, source = urlparse(url), urlparse(origin)
        if target.scheme != "https" or not target.hostname or target.hostname != source.hostname:
            raise InfrastructureError(ErrorCode.PROVIDER_ERROR, "Azure pointed the service at another host; it does not follow",
                                      {"url": origin, "host": target.hostname or ""})
        return url

    # ---- one call
    def call(self, method: str, url: str, json=None, params=None, audience: str | None = None, headers: dict | None = None, data: dict | None = None,
             bearer: str | None = None) -> AzureResponse:
        """One call. Tried again: a 429 and a call another operation refused (neither ran), any call that could not
        connect, and an idempotent call that met a 5xx or lost its answer; a 401 once with a fresh token. Each retry is
        a provider_retry event. Any other refusal raises. `data` sends a form instead of JSON; `bearer` is a token the
        caller holds instead of the host's for an audience (a registry's own token), "" for none."""
        idempotent = method in IDEMPOTENT
        refreshed = False
        attempt = 0
        while True:
            try:
                token = self.token(audience) if bearer is None else bearer
            except ValueError as exc:
                raise InfrastructureError(ErrorCode.PROVIDER_AUTH_FAILED, str(exc)) from None
            sent = {**(headers or {}), **({"Authorization": "Bearer " + token} if token else {})}
            try:
                response = self.client.request(method, url, json=json, data=data, params=params, headers=sent)
            except httpx.RequestError as exc:
                unsent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                if (unsent or idempotent) and attempt < RETRIES - 1:
                    pause = min(RETRY_PAUSE_SECONDS * 2 ** attempt, MAX_RETRY_PAUSE_SECONDS)
                    event("provider_retry", provider="azure", method=method, url=url, reason=type(exc).__name__, attempt=attempt + 1, pause_seconds=pause)
                    self.sleep(pause)
                    attempt += 1
                    continue
                raise InfrastructureError(ErrorCode.PROVIDER_UNAVAILABLE, f"Azure could not be reached ({type(exc).__name__})",
                                          {"url": url, **({} if unsent or idempotent else {"maybe_done": True})}) from None
            event("provider_call", provider="azure", method=method, url=url, status=response.status_code)
            answered = {name.lower(): value for name, value in response.headers.items()}
            if response.status_code < 400:
                body = _json(response)
                return AzureResponse(response.status_code, answered, body, "" if body is not None else response.text[:65536])
            if response.status_code == 401 and not refreshed and bearer is None and hasattr(self.token, "invalidate"):
                refreshed = True
                self.token.invalidate(audience)
                event("provider_retry", provider="azure", method=method, url=url, status=401, reason="token refreshed")
                continue
            body = _json(response)
            reason = str(_error(body).get("code") or "")
            busy = reason in BUSY
            if (response.status_code == 429 or busy or (idempotent and response.status_code in (500, 502, 503, 504))) and attempt < RETRIES - 1:
                asked = retry_after(answered)
                if asked is not None and asked > MAX_RETRY_PAUSE_SECONDS:
                    error = translate_error(response.status_code, body, response.text)
                    error.details["retry_after_seconds"] = asked
                    raise InfrastructureError(ErrorCode.PROVIDER_UNAVAILABLE, f"Azure asks the service to wait {asked:.0f} seconds before calling again; retry later",
                                              error.details)
                pause = asked if asked is not None else min(RETRY_PAUSE_SECONDS * 2 ** attempt, MAX_RETRY_PAUSE_SECONDS)
                pause = max(pause, BUSY_PAUSE_SECONDS) if busy else pause
                event("provider_retry", provider="azure", method=method, url=url, status=response.status_code, reason=reason, attempt=attempt + 1, pause_seconds=pause)
                self.sleep(pause)
                attempt += 1
                continue
            raise translate_error(response.status_code, body, response.text)

    # ---- reads
    def get(self, url: str, api_version: str, params=None, audience: str | None = None) -> dict:
        return self.call("GET", url, params={**(params or {}), "api-version": api_version}, audience=audience).body or {}

    def find(self, url: str, api_version: str) -> dict | None:
        """The resource, or None when Azure has none by that name: how a create whose answer was lost is found again."""
        try:
            return self.get(url, api_version)
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return None
            raise

    def list(self, url: str, api_version: str, params=None, audience: str | None = None) -> list[dict]:
        """Every item of a collection, page after page through nextLink."""
        body = self.get(url, api_version, params, audience)
        items = list(body.get("value") or [])
        for _ in range(MAX_PAGES):
            following = body.get("nextLink")
            if not following:
                return items
            body = self.call("GET", self._follow(following, url), audience=audience).body or {}
            items.extend(body.get("value") or [])
        raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Azure's list did not end after {MAX_PAGES} pages", {"url": url})

    # ---- writes, each waited on until Azure says it ended
    def put(self, url: str, body: dict, api_version: str, timeout: int | None = None) -> dict:
        """Create or update the resource named by url, and return it as it ended."""
        response = self.call("PUT", url, json=body, params={"api-version": api_version})
        return self._settle("PUT", url, api_version, response, timeout) or {}

    def patch(self, url: str, body: dict, api_version: str, timeout: int | None = None) -> dict:
        response = self.call("PATCH", url, json=body, params={"api-version": api_version})
        return self._settle("PATCH", url, api_version, response, timeout) or {}

    def post(self, url: str, api_version: str, body: dict | None = None, timeout: int | None = None) -> dict | None:
        """An action on a resource (start, deallocate, listKeys); its result when Azure gives one."""
        response = self.call("POST", url, json=body, params={"api-version": api_version})
        return self._settle("POST", url, api_version, response, timeout)

    def delete(self, url: str, api_version: str, timeout: int | None = None) -> None:
        """Delete the resource; one already gone is not an error (Azure answers 204, or 404 when its group is gone too)."""
        try:
            response = self.call("DELETE", url, params={"api-version": api_version})
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return
            raise
        self._settle("DELETE", url, api_version, response, timeout)

    def _settle(self, method: str, url: str, api_version: str, response: AzureResponse, timeout: int | None) -> dict | None:
        """Wait for a write to end: the resource as it ended (PUT, PATCH), an action's result (POST), nothing (DELETE)."""
        if response.status not in (201, 202) and _state(response.body) in ("", DONE, *FAILED):
            self._check(response.body, url)
            return None if method == "DELETE" else response.body
        operation, location = response.headers.get("azure-asyncoperation"), response.headers.get("location")
        deadline = self.clock() + (timeout or self.operation_timeout)
        pause = retry_after(response.headers, POLL_SECONDS)
        while True:
            if self.clock() >= deadline:
                raise InfrastructureError(ErrorCode.OPERATION_TIMEOUT, f"Azure's operation did not end within {timeout or self.operation_timeout} seconds",
                                          {"url": url, "method": method})
            self.sleep(min(max(pause, 1.0), MAX_POLL_SECONDS))
            if operation:
                polled = self.call("GET", self._follow(operation, url))
                status = str((polled.body or {}).get("status") or "").lower()
                if status == DONE:
                    break
                if status in FAILED:
                    raise self._failed(polled.body, url)
            elif location:
                polled = self.call("GET", self._follow(location, url))
                if polled.status != 202:
                    if method in ("POST", "DELETE"):
                        return polled.body if method == "POST" else None
                    break
            elif method == "DELETE":
                if self.find(url, api_version) is None:
                    return None
                polled = AzureResponse(200)
            else:
                polled = self.call("GET", url, params={"api-version": api_version})
                if _state(polled.body) not in ("", DONE, *FAILED):
                    pause = retry_after(polled.headers, POLL_SECONDS)
                    continue
                self._check(polled.body, url)
                return polled.body
            pause = retry_after(polled.headers, POLL_SECONDS)
        if method == "DELETE":
            return None
        if method == "POST":
            return self.call("GET", self._follow(location, url)).body if location else None
        final = self.get(url, api_version)
        self._check(final, url)
        return final

    def _check(self, body, url: str) -> None:
        """A resource that ended Failed or Canceled is an error, whatever the status of the answer that carried it."""
        if _state(body) in FAILED:
            raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Azure reports the resource {_state(body)}", {"url": url, "provisioning_state": _state(body)})

    @staticmethod
    def _failed(body, url: str) -> InfrastructureError:
        error = _error(body)
        reason, message = str(error.get("code") or ""), str(error.get("message") or "")
        code = code_for(None, reason, message)
        code = ErrorCode.PROVIDER_ERROR if code == ErrorCode.NOT_FOUND else code
        return InfrastructureError(code, f"Azure reports the operation failed: {message[:200] or str((body or {}).get('status'))}",
                                   {"url": url, "reason": reason, "provider_message": message[:500]})
