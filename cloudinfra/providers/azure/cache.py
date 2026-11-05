"""Azure Managed Redis behind the generic cache primitives (docs/research/RESEARCH-2026-11-03-azure.md, section 5.4).

Not Azure Cache for Redis, closed to new customers since 2026-04-01 and retired 2028-09-30. A cache is a
redisEnterprise cluster of the smallest Balanced size holding the memory asked, with its one database, default: TLS 1.2
and encrypted connections only on port 10000, OSS clustering, high availability only when asked (it cannot be turned
off later), public network access off and a private endpoint in the request network's subnet (private.py). Access keys
stay on beside Entra sign-in so that the service can copy the primary key into the request's cache-auth secret
(read_auth); the key is never returned or logged.
"""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import CacheResource, ResourceStatus
from . import private, translate
from .api import API_VERSIONS

VERSION = API_VERSIONS["Microsoft.Cache"]
STATES = {"running": ResourceStatus.READY, "creating": ResourceStatus.CREATING, "updating": ResourceStatus.UPDATING, "scaling": ResourceStatus.UPDATING,
          "deleting": ResourceStatus.DELETING, "disabled": ResourceStatus.STOPPED, "createfailed": ResourceStatus.FAILED, "updatefailed": ResourceStatus.FAILED}


class AzureCache:
    def __init__(self, api, groups, ceiling):
        self.api, self.groups, self.ceiling = api, groups, ceiling

    def create(self, spec, planned, ctx, network) -> CacheResource:
        if network is None:
            raise InfrastructureError(ErrorCode.INVALID_CACHE_CONFIGURATION, "A cache needs a network in the same request, or network_id naming one this service created",
                                      resource=planned.name)
        group = self.groups.ensure(ctx)
        cluster = f"{self.groups.id(group)}/providers/Microsoft.Cache/redisEnterprise/{planned.name}"
        tags = translate.tags(ctx)
        resource = CacheResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="azure", region=ctx.region,
                                 provider_id=cluster, version=planned.spec["version"], tier=planned.spec["tier"], memory_gb=planned.spec["memory_gb"],
                                 high_availability=spec.high_availability, port=translate.CACHE_PORT, network=network.id, labels=tags, status=ResourceStatus.CREATING)
        ctx.accepted(resource)
        self.api.put(self.api.arm(cluster), {"location": ctx.region, "tags": tags, "sku": {"name": planned.spec["tier"]}, "properties": {
            "minimumTlsVersion": "1.2", "highAvailability": "Enabled" if spec.high_availability else "Disabled", "publicNetworkAccess": "Disabled"}},
            VERSION, timeout=self.ceiling("cache"))
        self.api.put(self.api.arm(f"{cluster}/databases/default"), {"properties": {
            "clientProtocol": "Encrypted", "port": translate.CACHE_PORT, "clusteringPolicy": "OSSCluster", "evictionPolicy": "VolatileLRU",
            "accessKeysAuthentication": "Enabled"}}, VERSION, timeout=self.ceiling("cache"))
        private.make_endpoint(self.api, private.endpoint_id(self.groups, ctx, planned.name), ctx, tags, network, cluster, "redis", self.ceiling("network"))
        return self.get(resource)

    def get(self, resource: CacheResource) -> CacheResource:
        found = self.api.find(self.api.arm(resource.provider_id), VERSION)
        if found is None:
            return resource.model_copy(update={"status": ResourceStatus.DELETED, "host": None})
        properties = found.get("properties") or {}
        state = str(properties.get("resourceState") or properties.get("provisioningState") or "").lower()
        status = STATES.get(state, ResourceStatus.READY if state == "succeeded" else ResourceStatus.UNKNOWN)
        return resource.model_copy(update={"status": status, "host": properties.get("hostName") or resource.host})

    def read_auth(self, resource: CacheResource) -> str:
        """The database's primary access key (listKeys), to be copied into a secret the request owns."""
        keys = self.api.post(self.api.arm(f"{resource.provider_id}/databases/default/listKeys"), VERSION) or {}
        if not keys.get("primaryKey"):
            raise InfrastructureError(ErrorCode.PROVIDER_ERROR, "Azure gave no access key for the cache", resource=resource.id)
        return keys["primaryKey"]

    def delete(self, resource: CacheResource, ctx) -> None:
        endpoint = f"{resource.provider_id.split('/providers/', 1)[0]}/providers/Microsoft.Network/privateEndpoints/{resource.name}-pe"
        private.remove_endpoint(self.api, endpoint, self.ceiling("network"))
        self.api.delete(self.api.arm(resource.provider_id), VERSION, timeout=self.ceiling("cache"))
