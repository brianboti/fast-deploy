"""Managed disks and buckets behind the generic storage primitives (docs/research/RESEARCH-2026-11-03-azure.md, section 5.3).

A disk is zonal, in the request's zone where its machines are, and attaches to a machine there, hot, at the machine's
first free LUN, with deleteOption Detach: the disk is a resource of its own and outlives a machine it was attached to.

A bucket is a storage account of its own (a name unique across Azure, salted with the request id) with one container,
data: no public blob access and no account keys (Entra sign-in only), TLS 1.2, HTTPS only, deleted blobs and containers
kept seven days, zone-redundant where the region has zones. Azure deletes an account with everything in it, so the
service first lists the container through the blob service's own API and refuses PROVIDER_CONFLICT when it holds a blob:
an infrastructure call never destroys data. That read needs the Storage audience and a data role on the account (Storage
Blob Data Reader), which REQUIREMENTS.md asks the operator for.
"""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import ComputeResource, ResourceStatus, StorageResource
from . import translate
from .api import API_VERSIONS, STORAGE_DATA_VERSION

DISK_VERSION = API_VERSIONS["Microsoft.Compute/disks"]
STORAGE_VERSION = API_VERSIONS["Microsoft.Storage"]
CONTAINER = "data"
SOFT_DELETE_DAYS = 7
STATES = {"succeeded": ResourceStatus.READY, "creating": ResourceStatus.CREATING, "updating": ResourceStatus.UPDATING, "deleting": ResourceStatus.DELETING,
          "failed": ResourceStatus.FAILED, "resolvingdns": ResourceStatus.CREATING}


class AzureStorage:
    def __init__(self, api, settings, cloud, groups, compute, access, ceiling):
        self.api, self.settings, self.cloud, self.groups, self.compute, self.access, self.ceiling = api, settings, cloud, groups, compute, access, ceiling

    def _id(self, group: str, kind: str, name: str, namespace: str) -> str:
        return f"{self.groups.id(group)}/providers/{namespace}/{kind}/{name}"

    def container_scope(self, resource: StorageResource) -> str:
        return f"{resource.provider_id}/blobServices/default/containers/{CONTAINER}"

    def create(self, spec, planned, ctx) -> StorageResource:
        group = self.groups.ensure(ctx)
        tags = translate.tags(ctx)
        if planned.spec["type"] == "persistent":
            zone = planned.spec.get("zone")
            disk_id = self._id(group, "disks", planned.name, "Microsoft.Compute")
            body = {"location": ctx.region, "tags": tags, "sku": {"name": planned.spec["disk_sku"]},
                    "properties": {"creationData": {"createOption": "Empty"}, "diskSizeGB": spec.size_gb}}
            if zone:
                body["zones"] = [zone]
            resource = StorageResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="azure", region=ctx.region, zone=zone,
                                       type="persistent", size_gb=spec.size_gb, performance=spec.performance, labels=tags, provider_id=disk_id,
                                       status=ResourceStatus.CREATING)
            ctx.accepted(resource)
            self.api.put(self.api.arm(disk_id), body, DISK_VERSION, timeout=self.ceiling("disk"))
            return self.get(resource)
        account = planned.name
        account_id = self._id(group, "storageAccounts", account, "Microsoft.Storage")
        resource = StorageResource(id=planned.spec["resource_id"], name=account, request_id=ctx.request_id, provider="azure", region=ctx.region, type="object",
                                   size_gb=spec.size_gb, labels=tags, provider_id=account_id, url=f"https://{account}.blob.{self.cloud.storage_suffix}/{CONTAINER}",
                                   status=ResourceStatus.CREATING)
        ctx.accepted(resource)
        self.api.put(self.api.arm(account_id), {
            "location": ctx.region, "tags": tags, "kind": "StorageV2", "sku": {"name": planned.spec["redundancy"]},
            "properties": {"accessTier": "Hot", "allowBlobPublicAccess": False, "allowSharedKeyAccess": False, "defaultToOAuthAuthentication": True,
                           "minimumTlsVersion": "TLS1_2", "supportsHttpsTrafficOnly": True, "allowCrossTenantReplication": False, "publicNetworkAccess": "Enabled",
                           "encryption": {"keySource": "Microsoft.Storage", "services": {"blob": {"enabled": True}}}}}, STORAGE_VERSION, timeout=self.ceiling("storage_account"))
        self.api.put(self.api.arm(f"{account_id}/blobServices/default"), {"properties": {
            "deleteRetentionPolicy": {"enabled": True, "days": SOFT_DELETE_DAYS}, "containerDeleteRetentionPolicy": {"enabled": True, "days": SOFT_DELETE_DAYS}}},
            STORAGE_VERSION, timeout=self.ceiling("storage_account"))
        self.api.put(self.api.arm(self.container_scope(resource)), {"properties": {"publicAccess": "None"}}, STORAGE_VERSION, timeout=self.ceiling("storage_account"))
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def get(self, resource: StorageResource) -> StorageResource:
        version = DISK_VERSION if resource.type == "persistent" else STORAGE_VERSION
        found = self.api.find(self.api.arm(resource.provider_id), version)
        if found is None:
            return resource.model_copy(update={"status": ResourceStatus.DELETED})
        properties = found.get("properties") or {}
        status = STATES.get(str(properties.get("provisioningState", "")).lower(), ResourceStatus.UNKNOWN)
        update = {"status": status, "created_at": resource.created_at or properties.get("timeCreated") or properties.get("creationTime")}
        if resource.type == "persistent" and properties.get("diskSizeGB"):
            update["size_gb"] = int(properties["diskSizeGB"])
        return resource.model_copy(update=update)

    def attach(self, storage: StorageResource, compute: ComputeResource, ctx) -> StorageResource:
        if storage.type != "persistent":
            raise InfrastructureError(ErrorCode.INVALID_STORAGE_CONFIGURATION, "Only disks can be attached to a machine; a bucket is reached over the network",
                                      resource=storage.id)
        if (storage.zone or None) != (compute.zone or None):
            raise InfrastructureError(ErrorCode.INVALID_STORAGE_CONFIGURATION, f"The disk is in zone {storage.zone} and the machine in {compute.zone}; a disk attaches "
                                      "within its zone", resource=storage.id)
        self.compute.attach_disk(compute, storage.provider_id, storage.name)
        return storage.model_copy(update={"attached_to": compute.id})

    def _holds_blobs(self, resource: StorageResource) -> bool | None:
        """Whether the bucket's container holds a blob, from the blob service itself; None when the account is gone."""
        if not self.cloud.storage:
            raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"The service cannot check that bucket {resource.name} is empty in the {self.cloud.name} cloud, "
                                      "so it does not delete it: delete it by hand once it is empty", resource=resource.id)
        url = f"https://{resource.name}.blob.{self.cloud.storage_suffix}/{CONTAINER}"
        try:
            answer = self.api.call("GET", url, params={"restype": "container", "comp": "list", "maxresults": "1"}, audience=self.cloud.storage,
                                   headers={"x-ms-version": STORAGE_DATA_VERSION})
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return None
            if exc.code == ErrorCode.PROVIDER_PERMISSION_DENIED:
                raise InfrastructureError(ErrorCode.PROVIDER_PERMISSION_DENIED, f"The service cannot list bucket {resource.name} to check that it is empty: its "
                                          "identity needs Storage Blob Data Reader on the subscription or the request groups (REQUIREMENTS.md)", exc.details,
                                          resource.id) from None
            raise
        return "<Blob>" in answer.text

    def delete(self, resource: StorageResource, ctx) -> None:
        if resource.type == "persistent":
            self.api.delete(self.api.arm(resource.provider_id), DISK_VERSION, timeout=self.ceiling("disk"))
            return
        if self.api.find(self.api.arm(resource.provider_id), STORAGE_VERSION) is None:
            return
        if self._holds_blobs(resource):
            raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, f"Bucket {resource.name} still holds blobs; the service deletes only empty buckets, so that no "
                                      "data is destroyed by an infrastructure call", {"account": resource.name}, resource.id)
        for member in dict.fromkeys(grant.member for grant in resource.access):  # role assignments at its scope are this service's to remove
            principal = ctx.principals.get(member)
            if principal:
                self.access.revoke([(self.container_scope(resource), "bucket")], principal)
        self.api.delete(self.api.arm(resource.provider_id), STORAGE_VERSION, timeout=self.ceiling("storage_account"))
