"""Public DNS zones behind the generic dns primitives (Cloud DNS): one managed zone for a domain the operator allows, and
record sets written in one change, so a zone never holds half of them. A record may name a resource of the same request
(its load balancer, or a cluster's front door) instead of an address; it is filled in here, as the zone is made after
everything it can point to. Deleting takes this service's record sets out first, as Cloud DNS deletes only an empty
zone (https://docs.cloud.google.com/dns/docs/zones)."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import DnsResource, ResourceStatus
from . import translate
from .api import DNS


def fqdn(name: str, zone: str) -> str:
    return f"{zone}." if name == "@" else f"{name}.{zone}."


def rrdata(kind: str, value: str) -> str:
    """A value as Cloud DNS stores it: names end with a dot, text is quoted."""
    if kind == "CNAME":
        return value if value.endswith(".") else f"{value}."
    if kind == "MX":
        priority, _, host = value.partition(" ")
        return f"{priority} {host if host.endswith('.') else host + '.'}"
    if kind == "TXT" and not value.startswith('"'):
        return '"' + value.replace('"', '\\"') + '"'
    return value


class GcpDns:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def _zones(self) -> str:
        return f"{DNS}/projects/{self.api.project}/managedZones"

    @staticmethod
    def _target(record, created) -> str:
        """The address of the resource a record names."""
        if record.to == "load_balancer":
            found = next((item for item in created if item.kind.value == "load_balancer"), None)
            address = found.address if found else None
        else:
            name = record.to.partition(":")[2]
            clusters = [item for item in created if item.kind.value == "cluster" and item.public is not None]
            found = next((item for item in clusters if not name or item.name.endswith(f"-{name}")), None)
            address = found.public.address if found else None
        if not address:
            raise InfrastructureError(ErrorCode.INVALID_STATE, f"dns record {record.name}: {record.to} has no address yet", {"record": record.name})
        return address

    def create(self, spec, planned, ctx, created) -> DnsResource:
        zone = planned.name
        records = []
        for record in spec.records:
            values = [self._target(record, created)] if record.to else [rrdata(record.type, value) for value in record.values]
            records.append({"name": fqdn(record.name, spec.zone), "type": record.type, "ttl": record.ttl, "rrdatas": values})
        resource = DnsResource(id=planned.spec["resource_id"], name=zone, request_id=ctx.request_id, provider="gcp", region=ctx.region,
                               provider_id=f"projects/{self.api.project}/managedZones/{zone}", status=ResourceStatus.CREATING, dns_name=spec.zone,
                               records=[{"name": item["name"], "type": item["type"], "ttl": item["ttl"], "values": item["rrdatas"]} for item in records],
                               labels=translate.labels(ctx))
        try:
            made = self.api.post(self._zones(), json={"name": zone, "dnsName": f"{spec.zone}.", "description": translate.description(ctx)[:1024], "visibility": "public",
                                                      "labels": translate.labels(ctx)})
        except InfrastructureError as exc:
            if exc.code != ErrorCode.PROVIDER_CONFLICT:
                raise
            made = self.api.get(f"{self._zones()}/{zone}")
        ctx.accepted(resource)  # known from here: failed records still delete the zone
        if records:
            self.api.post(f"{self._zones()}/{zone}/changes", json={"additions": records})
        return resource.model_copy(update={"status": ResourceStatus.READY, "name_servers": made.get("nameServers", [])})

    def get(self, resource: DnsResource) -> DnsResource:
        try:
            found = self.api.get(f"{self._zones()}/{resource.name}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED})
            raise
        return resource.model_copy(update={"status": ResourceStatus.READY, "name_servers": found.get("nameServers", resource.name_servers)})

    def delete(self, resource: DnsResource, ctx) -> None:
        url = f"{self._zones()}/{resource.name}"
        try:
            present = self.api.get(f"{url}/rrsets").get("rrsets", [])
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return
            raise
        ours = [item for item in present if item.get("type") not in ("NS", "SOA") or item.get("name") != f"{resource.dns_name}."]
        if ours:
            self.api.post(f"{url}/changes", json={"deletions": ours})
        try:
            self.api.delete(url)
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
