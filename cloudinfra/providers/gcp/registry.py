"""Artifact Registry Docker repositories behind the generic registry primitives. A repository lives in the request's
region; its address is LOCATION-docker.pkg.dev/PROJECT/REPOSITORY. Deleting it deletes the images it holds."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import RegistryResource, ResourceStatus
from . import translate
from .api import ARTIFACT_REGISTRY


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
        operation = self.api.post(f"{ARTIFACT_REGISTRY}/{parent}/repositories", json={"format": "DOCKER", "description": f"Images of {ctx.name}"[:256],
                                                                                         "labels": translate.labels(ctx), "dockerConfig": {"immutableTags": spec.immutable_tags}},
                                  repositoryId=planned.name)
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
