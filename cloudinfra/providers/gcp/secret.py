"""Secret Manager containers behind the generic secret primitives. A value exists only when this service generated it, and
then it is written straight to the provider: never returned, never logged, never kept."""
import base64
import secrets

from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import ResourceStatus, SecretResource
from . import translate
from .api import SECRETS


class GcpSecret:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def create(self, spec, planned, ctx) -> SecretResource:
        resource_name = f"projects/{self.api.project}/secrets/{planned.name}"
        self.api.post(f"{SECRETS}/projects/{self.api.project}/secrets", json={"replication": {"automatic": {}}, "labels": translate.labels(ctx)}, secretId=planned.name)
        versions = 0
        if spec.generate_value:
            payload = base64.b64encode(secrets.token_urlsafe(32).encode()).decode()
            self.api.post(f"{SECRETS}/{resource_name}:addVersion", json={"payload": {"data": payload}})
            versions = 1
        return SecretResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region, provider_id=resource_name,
                              resource_name=resource_name, versions=versions, status=ResourceStatus.READY, labels=translate.labels(ctx), purpose=planned.spec.get("purpose"))

    def put_value(self, resource: SecretResource, value: str, ctx) -> SecretResource:
        payload = base64.b64encode(value.encode()).decode()
        self.api.post(f"{SECRETS}/{resource.resource_name}:addVersion", json={"payload": {"data": payload}})
        return resource.model_copy(update={"versions": resource.versions + 1})

    def get(self, resource: SecretResource) -> SecretResource:
        try:
            self.api.get(f"{SECRETS}/{resource.resource_name}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED})
            raise
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def delete(self, resource: SecretResource, ctx) -> None:
        try:
            self.api.delete(f"{SECRETS}/{resource.resource_name}")
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
