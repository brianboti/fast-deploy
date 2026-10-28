"""A VPC network, its subnet, firewall rules and Private Services Access behind the generic network primitives."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import FirewallRule, NetworkResource, ResourceStatus
from . import translate
from .api import COMPUTE, SERVICE_NETWORKING

SERVICE = "servicenetworking.googleapis.com"


class GcpNetwork:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def _global(self, collection):
        return f"{COMPUTE}/projects/{self.api.project}/global/{collection}"

    def _rule(self, network_link, name, ctx, sources, allowed) -> dict:
        return {"name": name, "description": translate.description(ctx), "network": network_link, "direction": "INGRESS", "sourceRanges": sources, "allowed": allowed}

    def _add_rule(self, network_link, name, ctx, ports, sources) -> FirewallRule:
        allowed = [{"IPProtocol": "tcp", "ports": [str(port) for port in ports]}] if ports else [{"IPProtocol": "tcp"}, {"IPProtocol": "udp"}, {"IPProtocol": "icmp"}]
        self.api.wait_compute(self.api.post(self._global("firewalls"), json=self._rule(network_link, name, ctx, sources, allowed)))
        return FirewallRule(name=name, ports=ports, sources=sources, provider_id=f"{self._global('firewalls')}/{name}")

    def create(self, spec, planned, ctx) -> NetworkResource:
        name, region = planned.name, ctx.region
        self.api.wait_compute(self.api.post(self._global("networks"), json={"name": name, "description": translate.description(ctx), "autoCreateSubnetworks": False, "routingMode": "REGIONAL"}))
        network_link = f"{self._global('networks')}/{name}"
        subnet = planned.spec["subnet"]
        subnet_link = f"{COMPUTE}/projects/{self.api.project}/regions/{region}/subnetworks/{subnet}"
        self.api.wait_compute(self.api.post(f"{COMPUTE}/projects/{self.api.project}/regions/{region}/subnetworks",
                                            json={"name": subnet, "description": translate.description(ctx), "network": network_link, "ipCidrRange": spec.cidr, "privateIpGoogleAccess": True}))
        rules = [self._add_rule(network_link, f"{name}-allow-internal", ctx, [], [spec.cidr])]
        if spec.iap_ssh:
            rules.append(self._add_rule(network_link, f"{name}-allow-iap-ssh", ctx, [22], [translate.IAP_RANGE]))
        if spec.public_access:
            rules.append(self._add_rule(network_link, f"{name}-allow-public", ctx, spec.allowed_ports, spec.allowed_sources))
        resource = NetworkResource(id=planned.spec["resource_id"], name=name, request_id=ctx.request_id, provider="gcp", region=region, provider_id=network_link,
                                   subnet=subnet_link, cidr=spec.cidr, public_access=spec.public_access, firewall_rules=rules, status=ResourceStatus.READY, labels=translate.labels(ctx))
        return self.ensure_private_services(resource, ctx) if planned.spec.get("private_services") else resource

    def ensure_private_services(self, network: NetworkResource, ctx) -> NetworkResource:
        """Private Services Access: a reserved range and a peering with Google's service producers, so a Cloud SQL instance
        can take a private address on this network. Done once per network; a network that has it is returned as it is."""
        if network.private_services_range:
            return network
        services_range = f"{network.name}-services"
        self.api.wait_compute(self.api.post(self._global("addresses"), json={"name": services_range, "description": translate.description(ctx), "purpose": "VPC_PEERING",
                                                                             "addressType": "INTERNAL", "prefixLength": 20, "network": network.provider_id}))
        consumer = f"projects/{self.api.project_number()}/global/networks/{network.name}"
        operation = self.api.post(f"{SERVICE_NETWORKING}/services/{SERVICE}/connections", json={"network": consumer, "reservedPeeringRanges": [services_range]})
        self.api.wait_service_networking(operation)
        return network.model_copy(update={"private_services_range": services_range})

    def configure_firewall(self, network: NetworkResource, ports: list[int], sources: list[str], ctx) -> FirewallRule:
        name = f"{network.name}-allow-{len(network.firewall_rules) + 1}"
        return self._add_rule(network.provider_id, name, ctx, ports, sources)

    def get(self, resource: NetworkResource) -> NetworkResource:
        try:
            network = self.api.get(f"{self._global('networks')}/{resource.name}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED})
            raise
        return resource.model_copy(update={"status": ResourceStatus.READY, "provider_id": network.get("selfLink", resource.provider_id),
                                           "created_at": resource.created_at or network.get("creationTimestamp")})

    def _delete_quietly(self, url, wait=True):
        try:
            operation = self.api.delete(url)
            if wait:
                self.api.wait_compute(operation)
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise

    def delete(self, resource: NetworkResource, ctx) -> None:
        for rule in resource.firewall_rules:
            self._delete_quietly(f"{self._global('firewalls')}/{rule.name}")
        if resource.private_services_range:
            consumer = f"projects/{self.api.project_number()}/global/networks/{resource.name}"
            try:
                operation = self.api.post(f"{SERVICE_NETWORKING}/services/{SERVICE}/connections/{SERVICE.replace('.', '-')}:deleteConnection", json={"consumerNetwork": consumer})
                self.api.wait_service_networking(operation)
            except InfrastructureError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
            self._delete_quietly(f"{self._global('addresses')}/{resource.private_services_range}")
        if resource.subnet:
            self._delete_quietly(resource.subnet)
        self._delete_quietly(f"{self._global('networks')}/{resource.name}")
