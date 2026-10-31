"""A VPC network, its subnet, firewall rules and Private Services Access behind the generic network primitives.

What the CIS benchmark and Google's foundation blueprint expect of a network (review of 2026-10-30, M10): flow logs on
the subnet and logging on every rule, as the operator's `network_logs` allows; a Cloud Router with Cloud NAT, so machines
and private nodes reach the internet without public addresses of their own; and, when the operator sets `deny_egress`,
egress refused except to private ranges and to Google's APIs at their private addresses (private.googleapis.com, with a
DNS response policy that sends *.googleapis.com, *.pkg.dev and *.gcr.io there), so nothing on the network reaches the
internet at all."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import FirewallRule, NetworkResource, ResourceStatus
from . import translate
from .api import COMPUTE, DNS, SERVICE_NETWORKING

SERVICE = "servicenetworking.googleapis.com"
# https://cloud.google.com/vpc/docs/flow-logs: a five-minute interval, half the flows, every metadata field
FLOW_LOGS = {"enable": True, "aggregationInterval": "INTERVAL_5_MIN", "flowSampling": 0.5, "metadata": "INCLUDE_ALL_METADATA"}
PRIVATE_RANGES = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
# https://cloud.google.com/vpc/docs/configure-private-google-access#domain-options: private.googleapis.com
PRIVATE_GOOGLE_RANGE = "199.36.153.8/30"
PRIVATE_GOOGLE_ADDRESSES = ["199.36.153.8", "199.36.153.9", "199.36.153.10", "199.36.153.11"]
GOOGLE_DOMAINS = {"googleapis": "*.googleapis.com.", "pkg-dev": "*.pkg.dev.", "gcr-io": "*.gcr.io."}


class GcpNetwork:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def _global(self, collection):
        return f"{COMPUTE}/projects/{self.api.project}/global/{collection}"

    def _rule(self, network_link, name, ctx, sources, allowed, direction="ingress", deny=False) -> dict:
        body = {"name": name, "description": translate.description(ctx), "network": network_link, "direction": direction.upper()}
        if direction == "egress":
            body.update({"destinationRanges": sources, "priority": 65000 if deny else 1000})
        else:
            body["sourceRanges"] = sources
        body["denied" if deny else "allowed"] = allowed
        if self.settings.network_logs:
            body["logConfig"] = {"enable": True}
        return body

    def _firewall(self, name, ports, sources, direction="ingress", deny=False) -> FirewallRule:
        return FirewallRule(name=name, ports=ports, sources=sources, direction=direction, deny=deny, provider_id=f"{self._global('firewalls')}/{name}")

    def _add_rule(self, network_link, name, ctx, ports, sources, accepted=None, direction="ingress", deny=False) -> FirewallRule:
        """One rule, ingress unless said; `accepted` is told of it once Google has taken the create, before the wait."""
        if deny:
            allowed = [{"IPProtocol": "all"}]
        elif ports:
            allowed = [{"IPProtocol": "tcp", "ports": [str(port) for port in ports]}]
        else:
            allowed = [{"IPProtocol": "tcp"}, {"IPProtocol": "udp"}, {"IPProtocol": "icmp"}]
        operation = self.api.post(self._global("firewalls"), json=self._rule(network_link, name, ctx, sources, allowed, direction, deny))
        rule = self._firewall(name, ports, sources, direction, deny)
        if accepted is not None:
            accepted(rule)
        self.api.wait_compute(operation)
        return rule

    def _routers(self, region):
        return f"{COMPUTE}/projects/{self.api.project}/regions/{region}/routers"

    def _policies(self):
        return f"{DNS}/projects/{self.api.project}/responsePolicies"

    def create(self, spec, planned, ctx) -> NetworkResource:
        name, region = planned.name, ctx.region
        network_link = f"{self._global('networks')}/{name}"
        subnet = planned.spec["subnet"]
        subnet_link = f"{COMPUTE}/projects/{self.api.project}/regions/{region}/subnetworks/{subnet}"
        rules = [self._firewall(f"{name}-allow-internal", [], [spec.cidr])]
        if spec.iap_ssh:
            rules.append(self._firewall(f"{name}-allow-iap-ssh", [22], [translate.IAP_RANGE]))
        if spec.public_access:
            rules.append(self._firewall(f"{name}-allow-public", spec.allowed_ports, spec.allowed_sources))
        deny_egress = self.settings.deny_egress
        if deny_egress:  # the allow outranks the deny (priority 1000 against 65000)
            rules.append(self._firewall(f"{name}-allow-egress-private", [], [*PRIVATE_RANGES, PRIVATE_GOOGLE_RANGE], "egress"))
            rules.append(self._firewall(f"{name}-deny-egress", [], ["0.0.0.0/0"], "egress", deny=True))
        nat = spec.nat and not deny_egress  # with egress denied there is nowhere for a NAT to send anything
        resource = NetworkResource(id=planned.spec["resource_id"], name=name, request_id=ctx.request_id, provider="gcp", region=region, provider_id=network_link,
                                   subnet=subnet_link, cidr=spec.cidr, public_access=spec.public_access, firewall_rules=rules, status=ResourceStatus.READY,
                                   router=f"{name}-router" if nat else None, nat=nat, dns_policy=f"{name}-google" if deny_egress else None, labels=translate.labels(ctx))
        operation = self.api.create(self._global("networks"), network_link,
                                    json={"name": name, "description": translate.description(ctx), "autoCreateSubnetworks": False, "routingMode": "REGIONAL"})
        # Google took the network's name as new, so its subnet, rules, router and DNS policy, named after it, are this request's too: all known from here
        ctx.accepted(resource.model_copy(update={"status": ResourceStatus.CREATING}))
        self.api.wait_compute(operation)
        subnet_body = {"name": subnet, "description": translate.description(ctx), "network": network_link, "ipCidrRange": spec.cidr, "privateIpGoogleAccess": True}
        if self.settings.network_logs:
            subnet_body["logConfig"] = dict(FLOW_LOGS)
        self.api.wait_compute(self.api.post(f"{COMPUTE}/projects/{self.api.project}/regions/{region}/subnetworks", json=subnet_body))
        for rule in rules:
            self._add_rule(network_link, rule.name, ctx, rule.ports, rule.sources, direction=rule.direction, deny=rule.deny)
        if nat:
            self.api.wait_compute(self.api.post(self._routers(region), json={
                "name": resource.router, "description": translate.description(ctx), "network": network_link,
                "nats": [{"name": f"{name}-nat", "natIpAllocateOption": "AUTO_ONLY", "sourceSubnetworkIpRangesToNat": "ALL_SUBNETWORKS_ALL_IP_RANGES",
                          "logConfig": {"enable": True, "filter": "ERRORS_ONLY"}}]}))
        if deny_egress:
            self._private_google(resource, ctx)
        return self.ensure_private_services(resource, ctx) if planned.spec.get("private_services") else resource

    def _private_google(self, network: NetworkResource, ctx) -> None:
        """A DNS response policy on the network that answers Google's API names with private.googleapis.com, the one
        Google range the egress rules leave open."""
        self.api.post(self._policies(), json={"responsePolicyName": network.dns_policy, "description": translate.description(ctx)[:1024],
                                              "networks": [{"networkUrl": network.provider_id}]})
        rules = [("private", "private.googleapis.com.", "A", PRIVATE_GOOGLE_ADDRESSES)]
        rules += [(rule, domain, "CNAME", ["private.googleapis.com."]) for rule, domain in GOOGLE_DOMAINS.items()]
        for rule, domain, kind, data in rules:
            self.api.post(f"{self._policies()}/{network.dns_policy}/rules", json={
                "ruleName": rule, "dnsName": domain, "localData": {"localDatas": [{"name": domain, "type": kind, "ttl": 300, "rrdatas": data}]}})

    def ensure_private_services(self, network: NetworkResource, ctx) -> NetworkResource:
        """Private Services Access: a reserved range and a peering with Google's service producers, so a Cloud SQL instance
        can take a private address on this network. Done once per network; a network that has it is returned as it is."""
        if network.private_services_range:
            return network
        services_range = f"{network.name}-services"
        operation = self.api.post(self._global("addresses"), json={"name": services_range, "description": translate.description(ctx), "purpose": "VPC_PEERING",
                                                                   "addressType": "INTERNAL", "prefixLength": 20, "network": network.provider_id})
        ctx.accepted(network.model_copy(update={"private_services_range": services_range}))  # the range, and the peering made on it, go with the network from here
        self.api.wait_compute(operation)
        consumer = f"projects/{self.api.project_number()}/global/networks/{network.name}"
        operation = self.api.post(f"{SERVICE_NETWORKING}/services/{SERVICE}/connections", json={"network": consumer, "reservedPeeringRanges": [services_range]})
        self.api.wait_service_networking(operation)
        return network.model_copy(update={"private_services_range": services_range})

    def configure_firewall(self, network: NetworkResource, ports: list[int], sources: list[str], ctx) -> FirewallRule:
        name = f"{network.name}-allow-{len([rule for rule in network.firewall_rules if rule.direction == 'ingress']) + 1}"
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
        if resource.router:
            self._delete_quietly(f"{self._routers(resource.region)}/{resource.router}")
        if resource.dns_policy:  # its rules, then the network taken off it, then the policy
            policy = f"{self._policies()}/{resource.dns_policy}"
            for rule in ("private", *GOOGLE_DOMAINS):
                self._delete_quietly(f"{policy}/rules/{rule}", wait=False)
            try:
                self.api.patch(policy, json={"networks": []})
            except InfrastructureError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
            self._delete_quietly(policy, wait=False)
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
