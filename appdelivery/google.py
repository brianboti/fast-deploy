"""Google credentials. This host has an identity of its own (the metadata server on Google Cloud, else the operator's
gcloud sign-in); every action a release takes is done as the environment's service account, through a short-lived token
the IAM Credentials API issues to this host's identity, which needs Token Creator on each environment's account. Tokens
live in memory until a minute before they end and are never written, logged or returned."""
import base64
import time

import httpx

from . import remote
from .errors import DeliveryError, ErrorCode

METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
IAM_CREDENTIALS = "https://iamcredentials.googleapis.com/v1"
SECRET_MANAGER = "https://secretmanager.googleapis.com/v1"  # noqa: S105 - an API address, not a credential
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
LIFETIME_SECONDS = 900  # long enough for one machine's pull, short enough to be worthless soon after


class HostToken:
    """An access token for this host's own identity."""

    def __init__(self, client: httpx.Client, runner=remote.run, clock=time.time):
        self.client, self.runner, self.clock = client, runner, clock
        self.cached, self.until = None, 0.0

    def __call__(self) -> str:
        if self.cached and self.clock() < self.until:
            return self.cached
        try:
            response = self.client.get(METADATA, headers={"Metadata-Flavor": "Google"}, timeout=0.5)
            if response.status_code == 200:
                data = response.json()
                self.cached, self.until = data["access_token"], self.clock() + max(60, int(data.get("expires_in", 300)) - 60)
                return self.cached
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        done = remote.gcloud_access_token(self.runner)
        token = done.stdout.strip()
        if done.returncode != 0 or not token:
            raise DeliveryError(ErrorCode.GOOGLE_AUTH_FAILED, "This host has no Google identity: on Google Cloud, run with a service account; elsewhere, "
                                "sign in with gcloud auth login")
        # gcloud may hand back a token it already holds, with only minutes left (found on 2026-10-29): kept five minutes,
        # and dropped at once when Google refuses it
        self.cached, self.until = token, self.clock() + 300
        return token

    def invalidate(self) -> None:
        self.cached, self.until = None, 0.0


class ServiceAccountTokens:
    """Short-lived tokens of each environment's service account, issued to this host's identity."""

    def __init__(self, client: httpx.Client, host_token, clock=time.time):
        self.client, self.host_token, self.clock = client, host_token, clock
        self.cache: dict[str, tuple[str, float]] = {}

    def __call__(self, service_account: str) -> str:
        cached = self.cache.get(service_account)
        if cached and self.clock() < cached[1]:
            return cached[0]
        for refreshed in (False, True):
            try:
                response = self.client.post(f"{IAM_CREDENTIALS}/projects/-/serviceAccounts/{service_account}:generateAccessToken",
                                            headers={"Authorization": f"Bearer {self.host_token()}"}, json={"scope": [SCOPE], "lifetime": f"{LIFETIME_SECONDS}s"})
            except httpx.HTTPError as exc:
                raise DeliveryError(ErrorCode.GOOGLE_AUTH_FAILED, f"Google's IAM Credentials API did not answer: {type(exc).__name__}") from None
            if response.status_code != 401 or refreshed or not hasattr(self.host_token, "invalidate"):
                break
            self.host_token.invalidate()  # this host's own token expired; one more try with a fresh one
        if response.status_code != 200:
            raise DeliveryError(ErrorCode.GOOGLE_AUTH_FAILED, f"This host may not act as {service_account} (HTTP {response.status_code}): it needs "
                                "roles/iam.serviceAccountTokenCreator on that account", {"service_account": service_account, "status": response.status_code})
        token = response.json().get("accessToken")
        if not token:
            raise DeliveryError(ErrorCode.GOOGLE_AUTH_FAILED, "Google's IAM Credentials API returned no token")
        self.cache[service_account] = (token, self.clock() + LIFETIME_SECONDS - 60)
        return token


def read_secret(client: httpx.Client, token: str, resource_name: str) -> str:
    """The latest version of a secret, read as the identity whose token this is. The value is returned to the caller only."""
    try:
        response = client.get(f"{SECRET_MANAGER}/{resource_name}/versions/latest:access", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise DeliveryError(ErrorCode.SECRET_UNAVAILABLE, f"Secret Manager did not answer: {type(exc).__name__}", {"secret": resource_name}) from None
    if response.status_code in (403, 404):
        raise DeliveryError(ErrorCode.SECRET_UNAVAILABLE, f"{resource_name} cannot be read as this environment's service account (HTTP {response.status_code}); "
                            "it needs the request's access grant", {"secret": resource_name, "status": response.status_code})
    if response.status_code != 200:
        raise DeliveryError(ErrorCode.SECRET_UNAVAILABLE, f"Secret Manager answered HTTP {response.status_code}", {"secret": resource_name, "status": response.status_code})
    try:
        return base64.b64decode(response.json()["payload"]["data"]).decode()
    except (ValueError, KeyError, UnicodeDecodeError):
        raise DeliveryError(ErrorCode.SECRET_UNAVAILABLE, f"{resource_name}'s latest version is not text", {"secret": resource_name}) from None
