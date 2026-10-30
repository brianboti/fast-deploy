"""Persistent disks and Cloud Storage buckets behind the generic storage primitives.

A disk with snapshots gets a snapshot schedule of its own (a resource policy in the region) before it is made, named in
the disk's create. The snapshots are kept in the region and labelled with the disk's name; deleting the disk deletes its
schedule and its snapshots, as deleting a database deletes its backups
(https://docs.cloud.google.com/compute/docs/disks/scheduled-snapshots)."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import ComputeResource, ResourceStatus, StorageResource
from . import translate
from .api import COMPUTE, STORAGE

DISK_LABEL = "cloudinfra-disk"
DISK_STATUS = {"CREATING": ResourceStatus.CREATING, "RESTORING": ResourceStatus.CREATING, "READY": ResourceStatus.READY, "DELETING": ResourceStatus.DELETING,
               "FAILED": ResourceStatus.FAILED}


class GcpStorage:
    def __init__(self, api, settings, compute):
        self.api, self.settings, self.compute = api, settings, compute

    def _disks(self, zone):
        return f"{COMPUTE}/projects/{self.api.project}/zones/{zone}/disks"

    def _policies(self, region, name=""):
        return f"{COMPUTE}/projects/{self.api.project}/regions/{region}/resourcePolicies" + (f"/{name}" if name else "")

    def _schedule(self, resource: StorageResource, snapshots: dict, ctx) -> str:
        """The disk's own daily snapshot schedule; its snapshots stay in the region and carry the disk's name."""
        body = {"name": resource.snapshot_schedule, "description": translate.description(ctx), "region": ctx.region,
                "snapshotSchedulePolicy": {"schedule": {"dailySchedule": {"daysInCycle": 1, "startTime": f"{snapshots['hour_utc']:02d}:00"}},
                                           "retentionPolicy": {"maxRetentionDays": snapshots["keep_days"], "onSourceDiskDelete": "APPLY_RETENTION_POLICY"},
                                           "snapshotProperties": {"labels": translate.labels(ctx, {DISK_LABEL: resource.name}), "storageLocations": [ctx.region]}}}
        operation = self.api.post(self._policies(ctx.region), json=body)
        ctx.accepted(resource)
        self.api.wait_compute(operation)
        return self._policies(ctx.region, resource.snapshot_schedule)

    def create(self, spec, planned, ctx) -> StorageResource:
        if planned.spec["type"] == "persistent":
            zone, snapshots = planned.spec["zone"], planned.spec.get("snapshots")
            body = {"name": planned.name, "description": translate.description(ctx), "sizeGb": str(spec.size_gb),
                    "type": f"zones/{zone}/diskTypes/{planned.spec['disk_type']}", "labels": translate.labels(ctx)}
            resource = StorageResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region, zone=zone,
                                       type="persistent", size_gb=spec.size_gb, performance=spec.performance, labels=body["labels"],
                                       provider_id=f"{self._disks(zone)}/{planned.name}", status=ResourceStatus.CREATING,
                                       snapshot_schedule=f"{planned.name}-snapshots" if snapshots else None, snapshot_keep_days=snapshots["keep_days"] if snapshots else None)
            if snapshots:
                body["resourcePolicies"] = [self._schedule(resource, snapshots, ctx)]
            operation = self.api.post(self._disks(zone), json=body)
            ctx.accepted(resource)
            self.api.wait_compute(operation)
            return self.get(resource)
        body = {"name": planned.name, "location": ctx.region.upper(), "storageClass": "STANDARD", "labels": translate.labels(ctx),
                "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}, "publicAccessPrevention": "enforced"}}
        bucket = self.api.post(f"{STORAGE}/b", json=body, project=self.api.project)
        return StorageResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region, type="object",
                               size_gb=spec.size_gb, labels=body["labels"], provider_id=bucket.get("id", planned.name), url=f"gs://{planned.name}",
                               status=ResourceStatus.READY, created_at=bucket.get("timeCreated"))

    def get(self, resource: StorageResource) -> StorageResource:
        try:
            if resource.type == "persistent":
                disk = self.api.get(f"{self._disks(resource.zone)}/{resource.name}")
                return resource.model_copy(update={"status": DISK_STATUS.get(disk.get("status", ""), ResourceStatus.UNKNOWN), "size_gb": int(disk.get("sizeGb", resource.size_gb)),
                                                   "provider_id": disk.get("selfLink", resource.provider_id), "created_at": resource.created_at or disk.get("creationTimestamp")})
            bucket = self.api.get(f"{STORAGE}/b/{resource.name}")
            return resource.model_copy(update={"status": ResourceStatus.READY, "created_at": resource.created_at or bucket.get("timeCreated")})
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED})
            raise

    def attach(self, storage: StorageResource, compute: ComputeResource, ctx) -> StorageResource:
        if storage.type != "persistent":
            raise InfrastructureError(ErrorCode.INVALID_STORAGE_CONFIGURATION, "Only persistent disks can be attached to a machine; a bucket is reached over the network",
                                      resource=storage.id)
        if storage.zone != compute.zone:
            raise InfrastructureError(ErrorCode.INVALID_STORAGE_CONFIGURATION, f"The disk is in {storage.zone} and the machine in {compute.zone}; a disk attaches within its zone",
                                      resource=storage.id)
        self.compute.attach_disk(compute, storage.provider_id, storage.name)
        return storage.model_copy(update={"attached_to": compute.id})

    def _delete_quietly(self, url) -> None:
        try:
            self.api.wait_compute(self.api.delete(url))
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise

    def _delete_snapshots(self, resource: StorageResource) -> None:
        """The schedule, once its disk is gone (a schedule in use cannot be deleted), then the snapshots it took."""
        self._delete_quietly(self._policies(resource.region, resource.snapshot_schedule))
        found = self.api.get(f"{COMPUTE}/projects/{self.api.project}/global/snapshots",
                             filter=f'labels.{DISK_LABEL}="{translate.label_value(resource.name)}" AND labels.cloudinfra-request="{translate.label_value(resource.request_id)}"')
        for snapshot in found.get("items", []):
            self._delete_quietly(f"{COMPUTE}/projects/{self.api.project}/global/snapshots/{snapshot['name']}")

    def delete(self, resource: StorageResource, ctx) -> None:
        if resource.type == "persistent":
            self._delete_quietly(f"{self._disks(resource.zone)}/{resource.name}")
            if resource.snapshot_schedule:
                self._delete_snapshots(resource)
            return
        try:
            self.api.delete(f"{STORAGE}/b/{resource.name}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return
            if exc.code == ErrorCode.PROVIDER_CONFLICT:
                raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, f"Bucket {resource.name} still holds objects; the service deletes only empty buckets, "
                                          "so that no data is destroyed by an infrastructure call", exc.details, resource.id) from None
            raise
