"""Analytics datasets (BigQuery) and document databases (Firestore) behind the generic warehouse and nosql primitives.

A dataset is made empty in the request's region; its tables are the Application Layer's. Access is granted on the
dataset itself, through its access list (use: WRITER, the dataEditor role; admin: OWNER, dataOwner). Running queries
also needs bigquery.jobs.create on a project, the platform team's one-time grant, named in the connect block. Like a
bucket, a dataset is deleted only when empty, so no table goes without the team's deleting it.

A Firestore database is made empty, in native mode, in the request's region; deleting it deletes its documents, as a
SQL database's do. Firestore has no policy per database: access is project-level, which the platform team grants once,
limited by an IAM condition on the database's name (https://docs.cloud.google.com/firestore/docs/security/iam), so this
service grants nothing on it."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import NoSqlResource, ResourceStatus, WarehouseResource
from . import translate
from .api import BIGQUERY, FIRESTORE
from .iam import same

DATASET_ROLES = {"use": ("WRITER", "roles/bigquery.dataEditor"), "admin": ("OWNER", "roles/bigquery.dataOwner")}
OURS = {"WRITER", "OWNER"}


def dataset_id(prefix: str, name: str) -> str:
    """BigQuery takes letters, digits and underscores."""
    return f"{prefix}_{name}".replace("-", "_")


class GcpWarehouse:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def _url(self, dataset: str) -> str:
        return f"{BIGQUERY}/projects/{self.api.project}/datasets/{dataset}"

    def create(self, spec, planned, ctx) -> WarehouseResource:
        dataset = dataset_id(ctx.name, planned.spec["warehouse"])
        body = {"datasetReference": {"projectId": self.api.project, "datasetId": dataset}, "location": ctx.region, "labels": translate.labels(ctx),
                "description": translate.description(ctx)}
        if spec.table_expiration_days:
            body["defaultTableExpirationMs"] = str(spec.table_expiration_days * 86_400_000)
        try:
            self.api.post(f"{BIGQUERY}/projects/{self.api.project}/datasets", json=body)
        except InfrastructureError as exc:
            if exc.code != ErrorCode.PROVIDER_CONFLICT:
                raise
        return WarehouseResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region,
                                 provider_id=f"projects/{self.api.project}/datasets/{dataset}", status=ResourceStatus.READY, dataset=dataset, location=ctx.region,
                                 table_expiration_days=spec.table_expiration_days, labels=translate.labels(ctx))

    def get(self, resource: WarehouseResource) -> WarehouseResource:
        try:
            self.api.get(self._url(resource.dataset))
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED})
            raise
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def delete(self, resource: WarehouseResource, ctx) -> None:
        try:
            self.api.delete(self._url(resource.dataset))
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return
            if "still in use" in exc.message or exc.code == ErrorCode.PROVIDER_CONFLICT:
                raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, f"Dataset {resource.dataset} still holds tables; the service deletes only empty datasets, so "
                                          "the team deletes its tables first", {"dataset": resource.dataset}) from None
            raise

    def _access(self, resource, mutate) -> None:
        found = self.api.get(self._url(resource.dataset))
        access = mutate(list(found.get("access", [])))
        self.api.patch(self._url(resource.dataset), json={"access": access})

    @staticmethod
    def _without(access: list, member: str) -> list:
        kind, email = member.split(":", 1)
        key = "groupByEmail" if kind == "group" else "userByEmail"
        return [entry for entry in access if not (entry.get("role") in OURS and same(entry.get(key, ""), email))]

    def grant(self, resource, member: str, level: str) -> list[str]:
        role, named = DATASET_ROLES[level]
        kind, email = member.split(":", 1)
        key = "groupByEmail" if kind == "group" else "userByEmail"
        self._access(resource, lambda access: [*self._without(access, member), {"role": role, key: email}])
        return [named]

    def revoke(self, resource, member: str) -> None:
        try:
            self._access(resource, lambda access: self._without(access, member))
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise


class GcpNoSql:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def _url(self, database: str) -> str:
        return f"{FIRESTORE}/projects/{self.api.project}/databases/{database}"

    def create(self, spec, planned, ctx) -> NoSqlResource:
        database = planned.name
        resource = NoSqlResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region,
                                 provider_id=f"projects/{self.api.project}/databases/{database}", status=ResourceStatus.CREATING, database=database, location=ctx.region,
                                 labels=translate.labels(ctx))
        operation = self.api.post(f"{FIRESTORE}/projects/{self.api.project}/databases", json={"type": "FIRESTORE_NATIVE", "locationId": ctx.region,
                                                                                             "deleteProtectionState": "DELETE_PROTECTION_DISABLED"}, databaseId=database)
        ctx.accepted(resource)  # known from here: a failed wait still deletes it
        self.api.wait_longrunning(operation, FIRESTORE, what="Firestore")
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def get(self, resource: NoSqlResource) -> NoSqlResource:
        try:
            self.api.get(self._url(resource.database))
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED})
            raise
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def delete(self, resource: NoSqlResource, ctx) -> None:
        try:
            operation = self.api.delete(self._url(resource.database))
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return
            raise
        if operation and operation.get("name"):
            self.api.wait_longrunning(operation, FIRESTORE, what="Firestore")
