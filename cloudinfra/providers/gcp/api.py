"""One thin REST client for every Google API the adapter uses: bearer token, structured errors, operation waits.

Every provider failure becomes an InfrastructureError with a code; the caller never sees an httpx exception or a raw
Google error body. The client is the single seam the unit tests replace with a fake.
"""
import time

import httpx

from ...models.errors import ErrorCode, InfrastructureError
from ...observability.logging import event

COMPUTE = "https://compute.googleapis.com/compute/v1"
SQL = "https://sqladmin.googleapis.com/v1"
STORAGE = "https://storage.googleapis.com/storage/v1"
MONITORING = "https://monitoring.googleapis.com/v3"
SERVICE_NETWORKING = "https://servicenetworking.googleapis.com/v1"
RESOURCE_MANAGER = "https://cloudresourcemanager.googleapis.com/v1"
PUBSUB = "https://pubsub.googleapis.com/v1"
SECRETS = "https://secretmanager.googleapis.com/v1"
CONTAINER = "https://container.googleapis.com/v1"
REDIS = "https://redis.googleapis.com/v1"
IAM = "https://iam.googleapis.com/v1"
ARTIFACT_REGISTRY = "https://artifactregistry.googleapis.com/v1"
RUN = "https://run.googleapis.com/v2"
SCHEDULER = "https://cloudscheduler.googleapis.com/v1"
DNS = "https://dns.googleapis.com/dns/v1"
LONG_POLL_SECONDS = 10
RETRIES = 4  # attempts of a call Google rate limited, or of a read it answered 5xx
RETRY_PAUSE_SECONDS = 1.0  # doubled after each attempt, unless Google says how long in Retry-After
MAX_RETRY_PAUSE_SECONDS = 30
SQL_POLL_SECONDS = 5
SERVICE_NETWORKING_POLL_SECONDS = 5
def settled(field: str, moving: tuple[str, ...]):
    """A `ready` for GcpApi.create: the resource's field no longer names a state it is still being made in."""
    return lambda found: found.get(field) not in moving


# A create whose answer was lost is looked for this many times, this many seconds apart: Google may show it a moment late.
LOOKUPS = (4, 5)


def translate_http_error(response: httpx.Response) -> InfrastructureError:
    """A Google error response as a coded error; the provider's own sentence is kept in details, never as the message alone."""
    status = response.status_code
    message, reason, status_text = response.text[:300], "", ""
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            message = str(error.get("message") or message)
            status_text = str(error.get("status") or "")
            errors = error.get("errors") or []
            reason = str((errors[0] or {}).get("reason") or "") if errors else ""
        elif error:
            message = str(error)
    except ValueError:
        pass
    details = {"http_status": status, "reason": reason, "provider_message": message[:500]}
    lowered = (reason + " " + message + " " + status_text).lower()
    if status == 401:
        return InfrastructureError(ErrorCode.PROVIDER_AUTH_FAILED, "Google Cloud refused the service's credential; check the host's service account or gcloud sign-in", details)
    if status == 403 and ("quota" in lowered or "resource_exhausted" in lowered):
        return InfrastructureError(ErrorCode.QUOTA_EXCEEDED, f"Google Cloud quota exceeded: {message[:200]}", details)
    if status == 403:
        return InfrastructureError(ErrorCode.PROVIDER_PERMISSION_DENIED, f"The service's identity is not allowed to do this in Google Cloud: {message[:200]}", details)
    if status == 404:
        return InfrastructureError(ErrorCode.NOT_FOUND, f"Google Cloud has no such resource: {message[:200]}", details)
    if status in (409, 412) or "alreadyexists" in lowered:
        return InfrastructureError(ErrorCode.PROVIDER_CONFLICT, f"Google Cloud reports a conflict: {message[:200]}", details)
    if status == 429:
        return InfrastructureError(ErrorCode.PROVIDER_UNAVAILABLE, "Google Cloud is rate limiting the service; retry later", details)
    if status >= 500:
        return InfrastructureError(ErrorCode.PROVIDER_UNAVAILABLE, f"Google Cloud answered {status}; retry later", details)
    return InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Google Cloud refused the request: {message[:200]}", details)


def retry_pause(response: httpx.Response, attempt: int) -> float:
    """How long to wait before the next attempt: Google's Retry-After when it gives one in seconds, else doubling."""
    try:
        asked = float(response.headers.get("Retry-After", ""))
    except ValueError:
        asked = RETRY_PAUSE_SECONDS * 2 ** attempt
    return min(max(asked, 0.0), MAX_RETRY_PAUSE_SECONDS)


class GcpApi:
    def __init__(self, project: str, token, client=None, sleep=time.sleep, clock=time.monotonic, operation_timeout: int = 900, quota_project: str | None = None):
        self.project, self.token, self.sleep, self.clock, self.operation_timeout = project, token, sleep, clock, operation_timeout
        self.client = client or httpx.Client(timeout=60, follow_redirects=False)
        self.quota_project = quota_project
        self._project_number = None

    # ---- plain requests
    def request(self, method: str, url: str, json=None, params=None) -> dict:
        """One call. A call Google rate limited (429: refused before it ran) is tried again, and so is a read Google answered
        with a 5xx; a write that met a 5xx is not, since it may have been done. A 401 (the token expired: Google refused it
        before anything ran) is tried once more with a fresh token. Each retry is a provider_retry event."""
        refreshed = False
        attempt = 0
        while True:
            try:
                token = self.token()
            except ValueError as exc:
                raise InfrastructureError(ErrorCode.PROVIDER_AUTH_FAILED, str(exc)) from None
            headers = {"Authorization": "Bearer " + token}
            if self.quota_project:
                headers["X-Goog-User-Project"] = self.quota_project
            try:
                response = self.client.request(method, url, json=json, params=params, headers=headers)
            except httpx.RequestError as exc:
                # a write whose request left but whose answer never came may have been done (review of 2026-10-30, L8)
                unsent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                raise InfrastructureError(ErrorCode.PROVIDER_UNAVAILABLE, f"Google Cloud could not be reached ({type(exc).__name__})",
                                          {"url": url, **({} if unsent or method == "GET" else {"maybe_done": True})}) from None
            event("provider_call", provider="gcp", method=method, url=url, status=response.status_code)
            if response.status_code == 401 and not refreshed and hasattr(self.token, "invalidate"):
                refreshed = True
                self.token.invalidate()
                event("provider_retry", provider="gcp", method=method, url=url, status=401, reason="token refreshed")
                continue
            retryable = response.status_code == 429 or (method == "GET" and response.status_code in (500, 502, 503, 504))
            if not retryable or attempt == RETRIES - 1:
                break
            pause = retry_pause(response, attempt)
            event("provider_retry", provider="gcp", method=method, url=url, status=response.status_code, attempt=attempt + 1, pause_seconds=pause)
            self.sleep(pause)
            attempt += 1
        if response.status_code >= 400:
            raise translate_http_error(response)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def get(self, url, **params):
        return self.request("GET", url, params=params or None)

    def post(self, url, json=None, **params):
        return self.request("POST", url, json=json, params=params or None)

    def create(self, url, lookup: str, json=None, ready=None, **params):
        """POST a create whose answer may be lost: a write that timed out or met a 5xx may have been done, and an adapter
        records a resource only once Google answered, so such a create would be an orphan nothing deletes (review of
        2026-10-30, L8). The planned name is looked up a few times, since Google may show a create a moment after it took
        it. Found, the create counts as taken, and the resource is read until `ready` says it has settled (or the operation
        timeout passes), since the adapter's own wait then has no operation to wait on. Not found, the first error stands."""
        try:
            return self.post(url, json=json, **params)
        except InfrastructureError as exc:
            if not (exc.details.get("maybe_done") or exc.details.get("http_status") in (500, 502, 503, 504)):
                raise
            found = self._look_up(lookup)
            if found is None:
                raise exc from None
            event("provider_create_found", provider="gcp", url=lookup, after=exc.code.value)
            deadline = self.clock() + self.operation_timeout
            while ready is not None and not ready(found):
                if self.clock() > deadline:  # recorded all the same: the adapter's next step fails, and the request's cleanup deletes it
                    event("provider_create_unsettled", provider="gcp", url=lookup)
                    break
                self.sleep(LONG_POLL_SECONDS)
                found = self.get(lookup)
            return {"status": "DONE", "done": True, "found_after": exc.code.value}

    def _look_up(self, url: str) -> dict | None:
        for attempt in range(LOOKUPS[0]):
            try:
                return self.get(url)
            except InfrastructureError:
                if attempt == LOOKUPS[0] - 1:
                    return None
                self.sleep(LOOKUPS[1])
        return None

    def patch(self, url, json=None):
        return self.request("PATCH", url, json=json)

    def put(self, url, json=None):
        return self.request("PUT", url, json=json)

    def delete(self, url):
        return self.request("DELETE", url)

    def project_number(self) -> str:
        if self._project_number is None:
            self._project_number = str(self.get(f"{RESOURCE_MANAGER}/projects/{self.project}")["projectNumber"])
        return self._project_number

    # ---- operations
    def wait_compute(self, operation: dict, timeout: int | None = None) -> dict:
        """Block until a Compute Engine operation is done; its `wait` endpoint holds the request up to two minutes at a time."""
        deadline = self.clock() + (timeout or self.operation_timeout)
        while True:
            if operation.get("status") == "DONE":
                if operation.get("error"):
                    errors = operation["error"].get("errors") or [{}]
                    raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Google Cloud operation failed: {errors[0].get('message', 'no detail')[:300]}",
                                              {"operation": operation.get("name"), "code": errors[0].get("code"), "errors": errors[:5]})
                return operation
            if self.clock() > deadline:
                raise InfrastructureError(ErrorCode.OPERATION_TIMEOUT, "A Google Cloud operation did not finish in time", {"operation": operation.get("name")})
            operation = self.post(operation["selfLink"] + "/wait")

    def wait_sql(self, operation: dict, timeout: int | None = None) -> dict:
        deadline = self.clock() + (timeout or self.operation_timeout)
        while True:
            if operation.get("status") == "DONE":
                if operation.get("error"):
                    errors = operation["error"].get("errors") or [{}]
                    raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Cloud SQL operation failed: {errors[0].get('message', 'no detail')[:300]}",
                                              {"operation": operation.get("name"), "code": errors[0].get("code"), "errors": errors[:5]})
                return operation
            if self.clock() > deadline:
                raise InfrastructureError(ErrorCode.OPERATION_TIMEOUT, "A Cloud SQL operation did not finish in time", {"operation": operation.get("name")})
            self.sleep(SQL_POLL_SECONDS)
            operation = self.get(f"{SQL}/projects/{self.project}/operations/{operation['name']}")

    def wait_longrunning(self, operation: dict, base: str, timeout: int | None = None, what: str = "Google Cloud") -> dict:
        """A google.longrunning operation (Memorystore): GET <base>/<name> until done."""
        deadline = self.clock() + (timeout or self.operation_timeout)
        while True:
            if operation.get("done"):
                if operation.get("error"):
                    raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"{what} operation failed: {str(operation['error'].get('message', 'no detail'))[:300]}",
                                              {"operation": operation.get("name")})
                return operation
            if self.clock() > deadline:
                raise InfrastructureError(ErrorCode.OPERATION_TIMEOUT, f"A {what} operation did not finish in time", {"operation": operation.get("name")})
            self.sleep(LONG_POLL_SECONDS)
            operation = self.get(f"{base}/{operation['name']}")

    def wait_container(self, operation: dict, timeout: int | None = None) -> dict:
        """A Kubernetes Engine operation: GET its self link until its status is DONE."""
        deadline = self.clock() + (timeout or self.operation_timeout)
        while True:
            if operation.get("status") == "DONE":
                if operation.get("error"):
                    raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Kubernetes Engine operation failed: {str(operation['error'].get('message', 'no detail'))[:300]}",
                                              {"operation": operation.get("name")})
                return operation
            if self.clock() > deadline:
                raise InfrastructureError(ErrorCode.OPERATION_TIMEOUT, "A Kubernetes Engine operation did not finish in time", {"operation": operation.get("name")})
            self.sleep(LONG_POLL_SECONDS)
            operation = self.get(operation["selfLink"])

    def wait_service_networking(self, operation: dict, timeout: int | None = None) -> dict:
        deadline = self.clock() + (timeout or self.operation_timeout)
        while True:
            if operation.get("done"):
                if operation.get("error"):
                    raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Service Networking operation failed: {str(operation['error'].get('message', 'no detail'))[:300]}",
                                              {"operation": operation.get("name")})
                return operation
            if self.clock() > deadline:
                raise InfrastructureError(ErrorCode.OPERATION_TIMEOUT, "A Service Networking operation did not finish in time", {"operation": operation.get("name")})
            self.sleep(SERVICE_NETWORKING_POLL_SECONDS)
            operation = self.get(f"{SERVICE_NETWORKING}/{operation['name']}")
