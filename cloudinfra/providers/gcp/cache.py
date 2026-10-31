"""Memorystore for Redis behind the generic cache primitives: private, authenticated, encrypted in transit."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import CacheResource, ResourceStatus
from . import translate
from .api import REDIS

STATE = {"CREATING": ResourceStatus.CREATING, "READY": ResourceStatus.READY, "UPDATING": ResourceStatus.UPDATING, "DELETING": ResourceStatus.DELETING,
         "REPAIRING": ResourceStatus.UPDATING, "MAINTENANCE": ResourceStatus.UPDATING, "IMPORTING": ResourceStatus.UPDATING, "FAILING_OVER": ResourceStatus.UPDATING}


class GcpCache:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def _instances(self, region):
        return f"{REDIS}/projects/{self.api.project}/locations/{region}/instances"

    def create(self, spec, planned, ctx, network) -> CacheResource:
        if network is None:
            raise InfrastructureError(ErrorCode.INVALID_CACHE_CONFIGURATION, "A cache needs a network in the same request, or network_id naming one this service created",
                                      resource=planned.name)
        tier = "STANDARD_HA" if spec.high_availability else "BASIC"
        body = {"displayName": planned.name, "tier": tier, "memorySizeGb": spec.memory_gb, "redisVersion": planned.spec["redis_version"], "authorizedNetwork": network.provider_id,
                "connectMode": "DIRECT_PEERING", "authEnabled": True, "transitEncryptionMode": "SERVER_AUTHENTICATION", "labels": translate.labels(ctx)}
        resource = CacheResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region,
                                 provider_id=f"{self._instances(ctx.region)}/{planned.name}", version=planned.spec["version"], tier=tier, memory_gb=spec.memory_gb,
                                 high_availability=spec.high_availability, network=network.id, labels=body["labels"], status=ResourceStatus.CREATING)
        operation = self.api.create(self._instances(ctx.region), f"{self._instances(ctx.region)}/{planned.name}", json=body, instanceId=planned.name)
        ctx.accepted(resource)
        self.api.wait_longrunning(operation, REDIS, what="Memorystore")
        return self.get(resource)

    def get(self, resource: CacheResource) -> CacheResource:
        try:
            instance = self.api.get(f"{self._instances(resource.region)}/{resource.name}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED, "host": None})
            raise
        certificates = instance.get("serverCaCerts") or [{}]
        return resource.model_copy(update={"status": STATE.get(instance.get("state", ""), ResourceStatus.UNKNOWN), "host": instance.get("host"), "port": int(instance.get("port") or 6379),
                                           "server_ca_cert": certificates[0].get("cert") or resource.server_ca_cert,
                                           "memory_gb": int(instance.get("memorySizeGb") or resource.memory_gb), "created_at": resource.created_at or instance.get("createTime")})

    def read_auth(self, resource: CacheResource) -> str:
        """The instance's AUTH string (redis.instances.getAuthString), to be copied into a secret the request owns."""
        return self.api.get(f"{resource.provider_id}/authString").get("authString", "")

    def delete(self, resource: CacheResource, ctx) -> None:
        try:
            self.api.wait_longrunning(self.api.delete(f"{self._instances(resource.region)}/{resource.name}"), REDIS, what="Memorystore")
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
