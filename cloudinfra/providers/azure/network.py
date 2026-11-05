"""A virtual network, its one subnet, its network security group, a NAT gateway and a flow log behind the generic network
primitives (docs/research/RESEARCH-2026-11-03-azure.md, section 5.1).

- The subnet has no default outbound access (learn.microsoft.com/azure/virtual-network/ip-services/default-outbound-access):
  a machine reaches the internet only through the request's NAT gateway, a public address of its own, or not at all.
  The property is set either way, since an older api-version gives the old implicit egress.
- The security group on the subnet admits the subnet itself (allow-internal), port 22 from the operator's Bastion subnet
  when iap_ssh is on (allow-bastion-ssh: the identity-aware part is Entra's sign-in on the machine), the request's public
  ports from its sources when public_access is on, and Azure's load-balancer probes; then it refuses every other inbound
  connection, the rest of a peered network included, where Azure's own rules would admit it.
- With the operator's deny_egress, outbound traffic is refused except within the network and to Entra ID for sign-in,
  and no NAT gateway is made.
- The NAT gateway and its address are in the request's zone, where the machines are; a region without zones has a
  regional one.
- A flow log goes to the operator's storage account through the region's network watcher when the operator names one.

Parallel writes to one network fail with AnotherOperationInProgress, which the client tries again; a network's parts are
made one after another, and the subnet is made with the network.
"""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import FirewallRule, NetworkResource, ResourceStatus
from . import translate
from .api import API_VERSIONS
from .groups import group_of

VERSION = API_VERSIONS["Microsoft.Network"]
NETWORK = "Microsoft.Network"
STATES = {"succeeded": ResourceStatus.READY, "updating": ResourceStatus.UPDATING, "creating": ResourceStatus.CREATING, "deleting": ResourceStatus.DELETING,
          "failed": ResourceStatus.FAILED}
INTERNAL, BASTION, PUBLIC, PROBES, DENY = 100, 110, 120, 3900, 4000  # rule priorities; configure_firewall's rules take 200 and up
FIRST_ADDED = 200
WATCHER_NOT_YET = (6, 10)  # Azure enables a region's network watcher when the first network there is made, a moment later


def _rule(name: str, priority: int, sources, ports, direction="Inbound", access="Allow", protocol="Tcp", destination="*") -> dict:
    sources = [sources] if isinstance(sources, str) else list(sources)
    properties = {"priority": priority, "direction": direction, "access": access, "protocol": protocol, "sourcePortRange": "*"}
    if len(sources) == 1:
        properties["sourceAddressPrefix"] = sources[0]
    else:
        properties["sourceAddressPrefixes"] = sources
    destinations = [destination] if isinstance(destination, str) else list(destination)
    if len(destinations) == 1:
        properties["destinationAddressPrefix"] = destinations[0]
    else:
        properties["destinationAddressPrefixes"] = destinations
    if ports:
        properties["destinationPortRanges"] = [str(port) for port in ports]
    else:
        properties["destinationPortRange"] = "*"
    return {"name": name, "properties": properties}


def reaches_internet(network: NetworkResource | None, public_ip: bool) -> bool:
    """Whether a machine on this network reaches the internet: through the network's NAT or an address of its own, on a
    network whose rules do not refuse egress. A machine on the operator's subnet (network None) is taken to: its egress
    is the operator's."""
    if network is None:
        return True
    denied = any(rule.deny and rule.direction == "egress" for rule in network.firewall_rules)
    return not denied and (network.nat or public_ip)


class AzureNetwork:
    def __init__(self, api, settings, groups, ceiling):
        self.api, self.settings, self.groups, self.ceiling = api, settings, groups, ceiling

    def _id(self, group: str, kind: str, name: str) -> str:
        return f"{self.groups.id(group)}/providers/{NETWORK}/{kind}/{name}"

    def _url(self, resource_id: str) -> str:
        return self.api.arm(resource_id)

    def _watcher(self, region: str) -> str:
        return f"/subscriptions/{self.api.subscription_id}/resourceGroups/{self.settings.network_watcher_group}/providers/{NETWORK}/networkWatchers/NetworkWatcher_{region}"

    def _rules(self, spec, name: str, nsg: str) -> tuple[list[dict], list[FirewallRule]]:
        """The security group's rules as Azure takes them, and as the network resource reports them."""
        bodies, rules = [], []

        def add(body, ports, sources, direction="ingress", deny=False):
            bodies.append(body)
            rules.append(FirewallRule(name=body["name"], ports=list(ports), sources=list(sources), direction=direction, deny=deny,
                                      provider_id=f"{nsg}/securityRules/{body['name']}"))

        add(_rule(f"{name}-allow-internal", INTERNAL, spec.cidr, [], protocol="*"), [], [spec.cidr])
        if spec.iap_ssh and self.settings.bastion_subnet:
            add(_rule(f"{name}-allow-bastion-ssh", BASTION, self.settings.bastion_subnet, [22]), [22], [self.settings.bastion_subnet])
        if spec.public_access:
            add(_rule(f"{name}-allow-public", PUBLIC, spec.allowed_sources, spec.allowed_ports), spec.allowed_ports, spec.allowed_sources)
        add(_rule(f"{name}-allow-probes", PROBES, "AzureLoadBalancer", [], protocol="*"), [], ["AzureLoadBalancer"])
        add(_rule(f"{name}-deny-inbound", DENY, "*", [], access="Deny", protocol="*"), [], ["*"], deny=True)
        if self.settings.deny_egress:
            add(_rule(f"{name}-allow-egress-internal", INTERNAL, "VirtualNetwork", [], "Outbound", protocol="*", destination="VirtualNetwork"),
                [], ["VirtualNetwork"], "egress")
            add(_rule(f"{name}-allow-egress-entra", BASTION, "*", [443], "Outbound", destination="AzureActiveDirectory"), [443], ["AzureActiveDirectory"], "egress")
            add(_rule(f"{name}-deny-egress", DENY, "*", [], "Outbound", access="Deny", protocol="*", destination="Internet"), [], ["Internet"], "egress", deny=True)
        return bodies, rules

    def create(self, spec, planned, ctx) -> NetworkResource:
        name, region, zones = planned.name, ctx.region, ([ctx.zone] if ctx.zone else [])
        group = self.groups.ensure(ctx)
        network_id = self._id(group, "virtualNetworks", name)
        subnet_id = f"{network_id}/subnets/{planned.spec['subnet']}"
        nsg = self._id(group, "networkSecurityGroups", f"{name}-nsg")
        nat = planned.spec["nat"]
        gateway = self._id(group, "natGateways", f"{name}-nat") if nat else None
        address = self._id(group, "publicIPAddresses", f"{name}-nat-ip") if nat else None
        # the watcher's group is shared by every request and request names are not unique: the flow log carries the request id
        flow_log = f"{self._watcher(region)}/flowLogs/{name}-{ctx.request_id[:8]}-flowlog" if planned.spec["flow_logs"] else None
        bodies, rules = self._rules(spec, name, nsg)
        tags = translate.tags(ctx)
        resource = NetworkResource(id=planned.spec["resource_id"], name=name, request_id=ctx.request_id, provider="azure", region=region, zone=ctx.zone,
                                   provider_id=network_id, subnet=subnet_id, cidr=spec.cidr, public_access=spec.public_access, firewall_rules=rules,
                                   router=gateway, nat=nat, flow_log=flow_log, labels=tags, status=ResourceStatus.CREATING)
        # every part is named after the network in the request's own group: all known from here, before any is made
        ctx.accepted(resource)
        self.api.put(self._url(nsg), {"location": region, "tags": tags, "properties": {"securityRules": bodies}}, VERSION, timeout=self.ceiling("network"))
        subnet = {"addressPrefix": spec.cidr, "defaultOutboundAccess": False, "networkSecurityGroup": {"id": nsg}, "privateEndpointNetworkPolicies": "Enabled"}
        if nat:
            ip = {"location": region, "tags": tags, "sku": {"name": "Standard", "tier": "Regional"},
                  "properties": {"publicIPAllocationMethod": "Static", "publicIPAddressVersion": "IPv4"}}
            body = {"location": region, "tags": tags, "sku": {"name": "Standard"}, "properties": {"idleTimeoutInMinutes": 4, "publicIpAddresses": [{"id": address}]}}
            if zones:
                ip["zones"], body["zones"] = zones, zones
            self.api.put(self._url(address), ip, VERSION, timeout=self.ceiling("network"))
            self.api.put(self._url(gateway), body, VERSION, timeout=self.ceiling("network"))
            subnet["natGateway"] = {"id": gateway}
        created = self.api.put(self._url(network_id), {"location": region, "tags": tags, "properties": {
            "addressSpace": {"addressPrefixes": [spec.cidr]}, "subnets": [{"name": planned.spec["subnet"], "properties": subnet}]}}, VERSION,
            timeout=self.ceiling("network"))
        if flow_log:
            self._flow_log(flow_log, network_id, region, tags)
        return resource.model_copy(update={"status": STATES.get(str((created.get("properties") or {}).get("provisioningState", "")).lower(), ResourceStatus.READY)})

    def _flow_log(self, flow_log: str, network_id: str, region: str, tags: dict) -> None:
        body = {"location": region, "tags": tags, "properties": {"targetResourceId": network_id, "storageId": self.settings.flow_logs_storage_account, "enabled": True,
                                                                  "retentionPolicy": {"days": 30, "enabled": True}, "format": {"type": "JSON", "version": 2}}}
        for attempt in range(WATCHER_NOT_YET[0]):
            try:
                self.api.put(self._url(flow_log), body, VERSION, timeout=self.ceiling("flow_log"))
                return
            except InfrastructureError as exc:
                if exc.code != ErrorCode.NOT_FOUND or attempt == WATCHER_NOT_YET[0] - 1:
                    raise
                self.api.sleep(WATCHER_NOT_YET[1])

    def configure_firewall(self, network: NetworkResource, ports: list[int], sources: list[str], ctx) -> FirewallRule:
        added = [rule for rule in network.firewall_rules if rule.name.rsplit("-", 1)[-1].isdigit()]  # the rules a caller added before: <network>-allow-<n>
        name = f"{network.name}-allow-{len(added) + 1}"
        nsg = self._id(group_of(network.provider_id), "networkSecurityGroups", f"{network.name}-nsg")
        priority = FIRST_ADDED + len(added)
        body = _rule(name, priority, sources, ports)
        self.api.put(self._url(f"{nsg}/securityRules/{name}"), {"properties": body["properties"]}, VERSION, timeout=self.ceiling("network"))
        return FirewallRule(name=name, ports=list(ports), sources=list(sources), provider_id=f"{nsg}/securityRules/{name}")

    def get(self, resource: NetworkResource) -> NetworkResource:
        found = self.api.find(self._url(resource.provider_id), VERSION)
        if found is None:
            return resource.model_copy(update={"status": ResourceStatus.DELETED})
        state = str((found.get("properties") or {}).get("provisioningState", "")).lower()
        return resource.model_copy(update={"status": STATES.get(state, ResourceStatus.UNKNOWN)})

    def delete(self, resource: NetworkResource, ctx) -> None:
        group = group_of(resource.provider_id)
        if resource.flow_log:
            self.api.delete(self._url(resource.flow_log), VERSION, timeout=self.ceiling("flow_log"))
        self.api.delete(self._url(resource.provider_id), VERSION, timeout=self.ceiling("network"))  # its subnet goes with it
        if resource.router:
            self.api.delete(self._url(resource.router), VERSION, timeout=self.ceiling("network"))
            self.api.delete(self._url(self._id(group, "publicIPAddresses", f"{resource.name}-nat-ip")), VERSION, timeout=self.ceiling("network"))
        self.api.delete(self._url(self._id(group, "networkSecurityGroups", f"{resource.name}-nsg")), VERSION, timeout=self.ceiling("network"))
