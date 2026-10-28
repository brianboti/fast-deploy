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
SQL_POLL_SECONDS = 5
SERVICE_NETWORKING_POLL_SECONDS = 5


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


class GcpApi:
    def __init__(self, project: str, token, client=None, sleep=time.sleep, clock=time.monotonic, operation_timeout: int = 900):
        self.project, self.token, self.sleep, self.clock, self.operation_timeout = project, token, sleep, clock, operation_timeout
        self.client = client or httpx.Client(timeout=60, follow_redirects=False)
        self._project_number = None

    # ---- plain requests
    def request(self, method: str, url: str, json=None, params=None) -> dict:
        try:
            token = self.token()
        except ValueError as exc:
            raise InfrastructureError(ErrorCode.PROVIDER_AUTH_FAILED, str(exc)) from None
        try:
            response = self.client.request(method, url, json=json, params=params, headers={"Authorization": "Bearer " + token})
        except httpx.RequestError as exc:
            raise InfrastructureError(ErrorCode.PROVIDER_UNAVAILABLE, f"Google Cloud could not be reached ({type(exc).__name__})", {"url": url}) from None
        event("provider_call", provider="gcp", method=method, url=url, status=response.status_code)
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

    def patch(self, url, json=None):
        return self.request("PATCH", url, json=json)

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
