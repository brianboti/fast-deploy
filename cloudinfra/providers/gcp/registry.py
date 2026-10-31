"""Artifact Registry Docker repositories behind the generic registry primitives. A repository lives in the request's
region; its address is LOCATION-docker.pkg.dev/PROJECT/REPOSITORY. Deleting it deletes the images it holds."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import RegistryResource, ResourceStatus
from . import translate
from .api import ARTIFACT_REGISTRY

CLEANUP = "untagged-after-90-days"


def address(location: str, project: str, repository_id: str) -> str:
    return f"{location}-docker.pkg.dev/{project}/{repository_id}"


class GcpRegistry:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def create(self, spec, planned, ctx) -> RegistryResource:
        parent = f"projects/{self.api.project}/locations/{ctx.region}"
        name = f"{parent}/repositories/{planned.name}"
        resource = RegistryResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region, provider_id=name,
                                    repository=name, address=address(ctx.region, self.api.project, planned.name), immutable_tags=spec.immutable_tags,
                                    size_gb=spec.size_gb, status=ResourceStatus.CREATING, labels=translate.labels(ctx))
        body = {"format": "DOCKER", "description": f"Images of {ctx.name}"[:256], "labels": translate.labels(ctx), "dockerConfig": {"immutableTags": spec.immutable_tags},
                # untagged images older than ninety days, reported and not deleted: a release may still run one by digest, so
                # the operator turns the dry run off once the report shows nothing in use (review of 2026-10-30, L10)
                "cleanupPolicies": {CLEANUP: {"id": CLEANUP, "action": "DELETE", "condition": {"tagState": "UNTAGGED", "olderThan": f"{90 * 86400}s"}}},
                "cleanupPolicyDryRun": True}
        key = self.settings.kms_keys.get(ctx.region)
        if key:
            body["kmsKeyName"] = key
        operation = self.api.create(f"{ARTIFACT_REGISTRY}/{parent}/repositories", f"{ARTIFACT_REGISTRY}/{name}", json=body, repositoryId=planned.name)
        ctx.accepted(resource)  # known from here: a failed wait still deletes it
        self.api.wait_longrunning(operation, ARTIFACT_REGISTRY, what="Artifact Registry")
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def get(self, resource: RegistryResource) -> RegistryResource:
        try:
            self.api.get(f"{ARTIFACT_REGISTRY}/{resource.repository}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED})
            raise
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def delete(self, resource: RegistryResource, ctx) -> None:
        try:
            operation = self.api.delete(f"{ARTIFACT_REGISTRY}/{resource.repository}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return
            raise
        self.api.wait_longrunning(operation, ARTIFACT_REGISTRY, what="Artifact Registry")
