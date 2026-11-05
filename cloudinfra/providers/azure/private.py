"""Private endpoints for managed services, and the private DNS zones they are found by (docs/research/RESEARCH-2026-11-03-azure.md,
section 5.1; learn.microsoft.com/azure/private-link/private-endpoint-dns).

A private database or cache takes a private endpoint in the request network's subnet, with public network access off.
Its name resolves to the endpoint's address through the service's private DNS zone (privatelink.postgres.database.azure.com,
privatelink.mysql.database.azure.com, privatelink.redis.azure.net, each cloud's own for the databases), linked to the network. A network may be linked to
only one zone of a name, so the zone lives with the network, in the network's own resource group, tagged as the network's
request: made by the first service that needs it (an idempotent PUT), and deleted with the network (network.py). The
endpoint is the service's own, in the service's request group, and goes with the service.
"""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import NetworkResource
from .api import API_VERSIONS
from .groups import group_of, name_of

NETWORK_VERSION = API_VERSIONS["Microsoft.Network"]
ZONE_VERSION = API_VERSIONS["Microsoft.Network/privateDnsZones"]
# the endpoint's group id by service
GROUP_IDS = {"postgresql": "postgresqlServer", "mysql": "mysqlServer", "redis": "redisEnterprise"}


def zone_of(cloud, service: str) -> str | None:
    """A service's private DNS zone in a cloud: privatelink. and the cloud's own suffix for the databases (from its
    metadata document); Azure Managed Redis's is named for the public cloud only, and None elsewhere."""
    if service == "postgresql":
        return f"privatelink.{cloud.postgresql_suffix}"
    if service == "mysql":
        return f"privatelink.{cloud.mysql_suffix}"
    return "privatelink.redis.azure.net" if cloud.name == "AzureCloud" else None
ZONE_TAGS = 15  # a private DNS zone takes at most fifteen tags
# A zone whose network link was just deleted may still be refused for a moment as holding it (CannotDeleteResource,
# "nested resources exist"; the real run of 2026-11-05 met it a second after the link's delete had ended): tried again.
NESTED_NOT_YET = (6, 10)


def zone_id(api, network: NetworkResource, zone: str) -> str:
    return f"/subscriptions/{api.subscription_id}/resourceGroups/{group_of(network.provider_id)}/providers/Microsoft.Network/privateDnsZones/{zone}"


def ensure_zone(api, network: NetworkResource, zone: str, ceiling: int) -> str:
    """The private DNS zone of a service, with the network's link to it; its id."""
    found = zone_id(api, network, zone)
    tags = dict(list(network.labels.items())[:ZONE_TAGS])  # the network's request owns the zone (its tags first): its destroy removes it
    api.put(api.arm(found), {"location": "global", "tags": tags}, ZONE_VERSION, timeout=ceiling)
    api.put(api.arm(f"{found}/virtualNetworkLinks/{network.name}"), {"location": "global", "tags": tags, "properties": {
        "virtualNetwork": {"id": network.provider_id}, "registrationEnabled": False}}, ZONE_VERSION, timeout=ceiling)
    return found


def endpoint_id(groups, ctx, name: str) -> str:
    return f"{groups.id(name_of(ctx))}/providers/Microsoft.Network/privateEndpoints/{name}-pe"


def make_endpoint(api, endpoint: str, ctx, tags: dict, network: NetworkResource, target: str, service: str, ceiling: int) -> None:
    """A private endpoint for the target in the network's subnet, and its DNS zone group, so the target's own name
    resolves to the endpoint inside the network."""
    group_id, zone = GROUP_IDS[service], zone_of(api.cloud, service)
    if zone is None:
        raise InfrastructureError(ErrorCode.UNSUPPORTED_RESOURCE_KIND, f"No private DNS zone is documented for {service} in the {api.cloud.name} cloud")
    zone_found = ensure_zone(api, network, zone, ceiling)
    api.put(api.arm(endpoint), {"location": ctx.region, "tags": tags, "properties": {
        "subnet": {"id": network.subnet},
        "privateLinkServiceConnections": [{"name": endpoint.rsplit("/", 1)[-1], "properties": {"privateLinkServiceId": target, "groupIds": [group_id]}}]}},
        NETWORK_VERSION, timeout=ceiling)
    api.put(api.arm(f"{endpoint}/privateDnsZoneGroups/default"), {"properties": {"privateDnsZoneConfigs": [
        {"name": zone.replace(".", "-"), "properties": {"privateDnsZoneId": zone_found}}]}}, NETWORK_VERSION, timeout=ceiling)


def remove_endpoint(api, endpoint: str, ceiling: int) -> None:
    api.delete(api.arm(endpoint), NETWORK_VERSION, timeout=ceiling)


def remove_zones(api, network: NetworkResource, ceiling: int) -> None:
    """Every private DNS zone in the network's group that is linked to it: its links, then the zone."""
    group = f"/subscriptions/{api.subscription_id}/resourceGroups/{group_of(network.provider_id)}"
    for zone in filter(None, (zone_of(api.cloud, service) for service in GROUP_IDS)):
        found = f"{group}/providers/Microsoft.Network/privateDnsZones/{zone}"
        if api.find(api.arm(found), ZONE_VERSION) is None:
            continue
        for link in api.list(api.arm(f"{found}/virtualNetworkLinks"), ZONE_VERSION):
            api.delete(api.arm(link["id"]), ZONE_VERSION, timeout=ceiling)
        for attempt in range(NESTED_NOT_YET[0]):
            try:
                api.delete(api.arm(found), ZONE_VERSION, timeout=ceiling)
                break
            except InfrastructureError as exc:
                if exc.details.get("reason") != "CannotDeleteResource" or attempt == NESTED_NOT_YET[0] - 1:
                    raise
                api.sleep(NESTED_NOT_YET[1])
