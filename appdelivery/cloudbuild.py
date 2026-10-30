"""Building an image in the trusted project with Cloud Build (docs/research/RESEARCH-2026-10-30-trusted-build.md).

This service composes the whole build itself: the source is a repository the platform team linked to Cloud Build, fetched
by Cloud Build at one commit; the only step is `docker build` with the team's Dockerfile and context; the image is pushed
by Cloud Build through the `images` field, which is the only way it records signed SLSA provenance for it, and
`requestedVerifyOption: VERIFIED` fails the build when that provenance is not made. It runs as the environment's build
account, never a default one, so the build can push to the team's registry and write logs and do nothing else. Nothing a
caller sends becomes a step, an argument beyond two paths and a name, a substitution, a secret or an account.

Every call to Cloud Build is made as the environment's own service account."""
import re

import httpx

from .config import BuildSettings, BuildSource
from .errors import DeliveryError, ErrorCode
from .models import BuildRequest

CLOUD_BUILD = "https://cloudbuild.googleapis.com/v1"
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
# Cloud Build's statuses, as this service reports them
STATUS = {"STATUS_UNKNOWN": "QUEUED", "PENDING": "QUEUED", "QUEUED": "QUEUED", "WORKING": "WORKING", "SUCCESS": "SUCCEEDED",
          "FAILURE": "FAILED", "INTERNAL_ERROR": "FAILED", "TIMEOUT": "FAILED", "CANCELLED": "FAILED", "EXPIRED": "FAILED"}
FINAL = ("SUCCEEDED", "FAILED")


def image_path(source: BuildSource, request: BuildRequest) -> str:
    """<registry>/<name>, without a tag or a digest."""
    return f"{source.registry}/{request.image}"


def tagged(source: BuildSource, request: BuildRequest, build_id: str) -> str:
    """The tag is the build id, so it is never moved: a repository with immutable tags keeps it on this build's digest."""
    return f"{image_path(source, request)}:{build_id}"


def compose(request: BuildRequest, source: BuildSource, settings: BuildSettings, build_id: str, infra_owner: str) -> dict:
    """The Build resource Cloud Build is sent: one docker build step, the image pushed by Cloud Build, provenance required."""
    tag = tagged(source, request, build_id)
    # TARGETARCH: Cloud Build's docker step has no BuildKit to set it (found 2026-10-29); Dockerfiles that read it get amd64,
    # the workers' architecture, and others ignore it
    args = ["build", "--build-arg", "TARGETARCH=amd64", "-t", tag, "-f", request.dockerfile, request.context]
    return {
        "source": {"connectedRepository": {"repository": source.repository, "revision": request.revision}},
        "steps": [{"name": settings.builder, "args": args}],
        "images": [tag],
        "serviceAccount": f"projects/{source.project}/serviceAccounts/{settings.service_account}",
        "options": {"logging": "CLOUD_LOGGING_ONLY", "requestedVerifyOption": "VERIFIED", "machineType": settings.machine_type},
        "timeout": f"{settings.timeout_seconds}s",
        "tags": ["appdelivery", infra_owner, request.environment, build_id],
    }


def outcome(found: dict, tag: str) -> tuple[str, str | None, dict | None]:
    """(status, digest, error) of a Build resource as Cloud Build reports it. A success with no digest for the image this
    service asked for is a failure: there is nothing to release."""
    status = STATUS.get(found.get("status", "STATUS_UNKNOWN"), "QUEUED")
    if status == "FAILED":
        detail = found.get("statusDetail") or found.get("failureInfo", {}).get("detail") or ""
        error = DeliveryError(ErrorCode.BUILD_FAILED, f"Cloud Build ended the build {found.get('status')}{': ' + detail if detail else ''}; its log says why",
                              {"cloud_status": found.get("status"), "log_url": found.get("logUrl")})
        return "FAILED", None, error.detail().model_dump(mode="json")
    if status != "SUCCEEDED":
        return status, None, None
    digest = next((item.get("digest") for item in found.get("results", {}).get("images", []) if item.get("name") == tag), None)
    if not digest or not DIGEST.match(digest):
        error = DeliveryError(ErrorCode.BUILD_FAILED, f"Cloud Build reported success but no digest for {tag}", {"cloud_status": "SUCCESS"})
        return "FAILED", None, error.detail().model_dump(mode="json")
    return "SUCCEEDED", digest, None


class CloudBuildClient:
    """Cloud Build's builds.create and builds.get in a region, with the environment's Google token."""

    def __init__(self, token, client: httpx.Client | None = None):
        self.token, self.client = token, client or httpx.Client(timeout=60, follow_redirects=False)

    def _request(self, method: str, path: str, json=None, params=None) -> dict:
        try:
            response = self.client.request(method, f"{CLOUD_BUILD}/{path}", json=json, params=params, headers={"Authorization": f"Bearer {self.token()}"})
        except httpx.HTTPError as exc:
            raise DeliveryError(ErrorCode.BUILD_UNAVAILABLE, f"Cloud Build could not be reached ({type(exc).__name__})", {"path": path}) from None
        if response.status_code in (401, 403):
            raise DeliveryError(ErrorCode.BUILD_UNAVAILABLE, f"Cloud Build refused this environment's service account ({response.status_code}): it needs "
                                "roles/cloudbuild.builds.editor on the project and roles/iam.serviceAccountUser on the build account",
                                {"path": path, "status": response.status_code, "said": response.text[:300]})
        if response.status_code >= 400:
            raise DeliveryError(ErrorCode.BUILD_FAILED, f"Cloud Build answered {response.status_code} to {method} {path}",
                                {"path": path, "status": response.status_code, "said": response.text[:300]})
        try:
            return response.json()
        except ValueError:
            raise DeliveryError(ErrorCode.BUILD_UNAVAILABLE, "Cloud Build answered something that is not JSON", {"path": path}) from None

    def create(self, project: str, region: str, body: dict) -> dict:
        """Start a build; the Build resource as created (its id, status and log address), from the operation's metadata."""
        operation = self._request("POST", f"projects/{project}/locations/{region}/builds", json=body, params={"projectId": project})
        build = (operation.get("metadata") or {}).get("build") or {}
        if not build.get("id"):
            raise DeliveryError(ErrorCode.BUILD_UNAVAILABLE, "Cloud Build accepted the build but named no build id", {"operation": operation.get("name")})
        return build

    def get(self, project: str, region: str, cloud_build_id: str) -> dict:
        return self._request("GET", f"projects/{project}/locations/{region}/builds/{cloud_build_id}")
