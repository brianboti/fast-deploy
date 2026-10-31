"""Persistent disks and Cloud Storage buckets behind the generic storage primitives."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import ComputeResource, ResourceStatus, StorageResource
from . import translate
from .api import COMPUTE, STORAGE, settled

SOFT_DELETE_SECONDS = 7 * 86400
DISK_STATUS = {"CREATING": ResourceStatus.CREATING, "RESTORING": ResourceStatus.CREATING, "READY": ResourceStatus.READY, "DELETING": ResourceStatus.DELETING,
               "FAILED": ResourceStatus.FAILED}


class GcpStorage:
    def __init__(self, api, settings, compute):
        self.api, self.settings, self.compute = api, settings, compute

    def _disks(self, zone):
        return f"{COMPUTE}/projects/{self.api.project}/zones/{zone}/disks"

    def create(self, spec, planned, ctx) -> StorageResource:
        if planned.spec["type"] == "persistent":
            zone = planned.spec["zone"]
            body = {"name": planned.name, "description": translate.description(ctx), "sizeGb": str(spec.size_gb),
                    "type": f"zones/{zone}/diskTypes/{planned.spec['disk_type']}", "labels": translate.labels(ctx)}
            key = self.settings.kms_keys.get(ctx.region)
            if key:
                body["diskEncryptionKey"] = {"kmsKeyName": key}
            resource = StorageResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region, zone=zone,
                                       type="persistent", size_gb=spec.size_gb, performance=spec.performance, labels=body["labels"],
                                       provider_id=f"{self._disks(zone)}/{planned.name}", status=ResourceStatus.CREATING)
            operation = self.api.create(self._disks(zone), f"{self._disks(zone)}/{planned.name}", json=body, ready=settled("status", ("CREATING", "RESTORING")))
            ctx.accepted(resource)
            self.api.wait_compute(operation)
            return self.get(resource)
        # Uniform access, public access prevented, deleted objects kept seven days (soft delete, stated rather than left to
        # Google's default), and the operator's KMS key for the region when there is one (review of 2026-10-30, L10).
        body = {"name": planned.name, "location": ctx.region.upper(), "storageClass": "STANDARD", "labels": translate.labels(ctx),
                "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}, "publicAccessPrevention": "enforced"},
                "softDeletePolicy": {"retentionDurationSeconds": str(SOFT_DELETE_SECONDS)}}
        key = self.settings.kms_keys.get(ctx.region)
        if key:
            body["encryption"] = {"defaultKmsKeyName": key}
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

    def delete(self, resource: StorageResource, ctx) -> None:
        try:
            if resource.type == "persistent":
                self.api.wait_compute(self.api.delete(f"{self._disks(resource.zone)}/{resource.name}"))
            else:
                self.api.delete(f"{STORAGE}/b/{resource.name}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return
            if exc.code == ErrorCode.PROVIDER_CONFLICT and resource.type == "object":
                raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, f"Bucket {resource.name} still holds objects; the service deletes only empty buckets, "
                                          "so that no data is destroyed by an infrastructure call", exc.details, resource.id) from None
            raise
