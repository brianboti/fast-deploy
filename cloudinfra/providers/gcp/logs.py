"""Log export behind the generic log_export primitives (Cloud Logging sinks): the logs of the request's machines, clusters,
services, jobs and databases, routed as they arrive to the request's own bucket or dataset
(https://docs.cloud.google.com/logging/docs/export/configure_export_v2). The sink writes as an identity of its own, which
Google makes for it; that identity is granted on the destination only (objectCreator on a bucket, WRITER on a dataset)
and the grant is removed with the sink. A bucket that holds logs is deleted only when empty, as any bucket is."""
from types import SimpleNamespace

from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import LogExportResource, ResourceStatus
from . import translate
from .api import LOGGING
from .iam import update_policy, with_member, without_member
from .monitor import delete_quietly


def clause(resource, project: str) -> str | None:
    """The log filter that selects one resource's own entries, or None for a kind that writes none worth exporting."""
    kind, name = resource.kind.value, resource.name
    if kind == "compute":
        return f'(resource.type="gce_instance" AND labels."compute.googleapis.com/resource_name"="{name}")'
    if kind == "cluster":
        return f'resource.labels.cluster_name="{name}"'
    if kind == "service":
        return f'(resource.type="cloud_run_revision" AND resource.labels.service_name="{name}")'
    if kind == "job":
        return f'(resource.type="cloud_run_job" AND resource.labels.job_name="{name}")'
    if kind == "database":
        return f'(resource.type="cloudsql_database" AND resource.labels.database_id="{project}:{name}")'
    return None


def log_filter(created, project: str, min_severity: str | None) -> str:
    found = " OR ".join(item for item in (clause(resource, project) for resource in created) if item)
    return f"severity>={min_severity} AND ({found})" if min_severity else found


class GcpLogExport:
    def __init__(self, api, settings, iam, warehouse):
        self.api, self.settings, self.iam, self.warehouse = api, settings, iam, warehouse

    def _destination(self, to: str, created):
        kind, _, name = to.partition(":")
        if kind == "storage":
            found = next((item for item in created if item.kind.value == "storage" and item.type == "object"), None)
            return found, (f"storage.googleapis.com/{found.name}" if found else None)
        found = next((item for item in created if item.kind.value == "warehouse" and (not name or item.name.endswith(f"-{name}"))), None)
        return found, (f"bigquery.googleapis.com/projects/{self.api.project}/datasets/{found.dataset}" if found else None)

    def _allow(self, destination, writer: str) -> None:
        if destination.kind.value == "storage":
            update_policy(self.iam._bucket(destination), lambda policy: with_member(policy, "roles/storage.objectCreator", writer))
        else:
            self.warehouse.grant(destination, writer, "use")

    def _disallow(self, resource: LogExportResource) -> None:
        """The writer's grant, found by the destination's name: the destination may be gone already."""
        target = SimpleNamespace(name=resource.destination_name, dataset=resource.destination_name)
        try:
            if resource.to == "storage":
                update_policy(self.iam._bucket(target), lambda policy: without_member(policy, resource.writer_identity))
            else:
                self.warehouse.revoke(target, resource.writer_identity)
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise

    def create(self, spec, planned, ctx, created) -> LogExportResource:
        destination, target = self._destination(spec.to, created)
        if destination is None:
            raise InfrastructureError(ErrorCode.INVALID_STATE, f"log_export.to: {spec.to} was not made", {"to": spec.to})
        selected = log_filter(created, self.api.project, spec.min_severity)
        resource = LogExportResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region,
                                     provider_id=f"projects/{self.api.project}/sinks/{planned.name}", status=ResourceStatus.CREATING, to=spec.to, destination=target,
                                     destination_name=destination.name if destination.kind.value == "storage" else destination.dataset, filter=selected,
                                     min_severity=spec.min_severity, labels=translate.labels(ctx))
        body = {"name": planned.name, "destination": target, "filter": selected, "description": translate.description(ctx)}
        if destination.kind.value == "warehouse":
            body["bigqueryOptions"] = {"usePartitionedTables": True}
        made = self.api.post(f"{LOGGING}/projects/{self.api.project}/sinks", json=body, uniqueWriterIdentity="true")
        resource = resource.model_copy(update={"writer_identity": made["writerIdentity"]})
        ctx.accepted(resource)  # the sink exists: a failure from here deletes it and its grant
        self._allow(destination, made["writerIdentity"])
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def get(self, resource: LogExportResource) -> LogExportResource:
        try:
            self.api.get(f"{LOGGING}/{resource.provider_id}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED})
            raise
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def delete(self, resource: LogExportResource, ctx) -> None:
        delete_quietly(self.api, f"{LOGGING}/{resource.provider_id}")
        if resource.writer_identity:
            self._disallow(resource)
