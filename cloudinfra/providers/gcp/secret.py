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
        resource = SecretResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region, provider_id=resource_name,
                                  resource_name=resource_name, versions=0, status=ResourceStatus.READY, labels=translate.labels(ctx), purpose=planned.spec.get("purpose"))
        key = self.settings.kms_keys.get(ctx.region)  # with the operator's key the secret is kept in the request's region only, under it
        replication = {"userManaged": {"replicas": [{"location": ctx.region, "customerManagedEncryption": {"kmsKeyName": key}}]}} if key else {"automatic": {}}
        self.api.post(f"{SECRETS}/projects/{self.api.project}/secrets", json={"replication": replication, "labels": translate.labels(ctx)}, secretId=planned.name)
        if not spec.generate_value:
            return resource
        ctx.accepted(resource.model_copy(update={"status": ResourceStatus.CREATING}))  # known from here: a failed first version still deletes it
        payload = base64.b64encode(secrets.token_urlsafe(32).encode()).decode()
        self.api.post(f"{SECRETS}/{resource_name}:addVersion", json={"payload": {"data": payload}})
        return resource.model_copy(update={"versions": 1})

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
