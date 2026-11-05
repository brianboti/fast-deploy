"""Load balancers behind the generic primitive (docs/research/RESEARCH-2026-11-03-azure.md, sections 5 and 5.3).

Every load balancer is a Standard public load balancer in the request's group: a static Standard address (zone-redundant
where the region has zones), the machines in an IP-based backend pool by their private addresses in the virtual network
their own interfaces are on (machines of two networks are refused), a health probe (TCP, or HTTP on health_check_path
for http) and one rule on the port; TCP reset on idle, and no outbound SNAT, since the machines leave through their
network's NAT gateway. It passes connections through, so the machines see each client's own address, and the security
group on their subnet admits the port to those machines' addresses only: from the internet, or with hostnames from
Front Door's backends only (the AzureFrontDoor.Backend service tag). On the operator's subnet, whose security group is
the operator's, nothing is added. Azure's probes come from AzureLoadBalancer, which every network the service makes
admits already.

With hostnames, Azure Front Door Standard is in front of it, the only Azure product that gives a managed certificate
(learn.microsoft.com/azure/frontdoor/domain): one profile, an endpoint, an origin group probing health_check_path, the
load balancer's address as its one origin, a custom domain per hostname with a managed certificate and TLS 1.2 at
least, and a route that redirects HTTP to HTTPS and forwards to the origin over HTTP; the operator's WAF policy when it
names one. The operator proves each hostname with a TXT record (_dnsauth.<hostname>, the token the resource reports)
and points it at the endpoint with a CNAME; the certificate is ACTIVE once Front Door has approved the domain. The
service tag admits every Front Door profile, any tenant's, so the profile's id is recorded (front_door_id) for the
application to check each request's X-Azure-FDID header against.

Each piece is recorded in creation order on the resource as soon as Azure is asked for it, and torn down in reverse; a
Front Door profile takes its endpoints, origins, domains, routes and policies with it.
"""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import LoadBalancerResource, ResourceStatus
from . import translate
from .api import API_VERSIONS

VERSION = API_VERSIONS["Microsoft.Network"]
CDN_VERSION = API_VERSIONS["Microsoft.Cdn"]
FRONT_DOOR_SOURCE, INTERNET = "AzureFrontDoor.Backend", "Internet"
TOP_LEVEL = ("public_ip", "load_balancer", "security_rule", "front_door")  # what a teardown deletes; the rest goes with its profile
FAILED_DOMAINS = ("rejected", "timedout", "internalerror")


def certificate_state(states: list[str]) -> str | None:
    """The contract's certificate state from Front Door's domainValidationState of each hostname."""
    if not states:
        return None
    lowered = [str(state or "").lower() for state in states]
    if any(state in FAILED_DOMAINS for state in lowered):
        return "FAILED"
    return "ACTIVE" if all(state == "approved" for state in lowered) else "PROVISIONING"


def domain_name(hostname: str) -> str:
    """A custom domain's resource name: the hostname with its dots as dashes."""
    return hostname.replace(".", "-")


class AzureLoadBalancer:
    def __init__(self, api, settings, groups, network, compute, zones, ceiling):
        self.api, self.settings, self.groups, self.network, self.compute, self.zones, self.ceiling = api, settings, groups, network, compute, zones, ceiling

    def _subnet_of(self, machine) -> str:
        """The subnet a machine's primary interface is on, as Azure answers it: a machine of an earlier request may be on
        another network than this request's."""
        interface = self.api.find(self.api.arm(self.compute.interface(machine)), VERSION) or {}
        configurations = (interface.get("properties") or {}).get("ipConfigurations") or [{}]
        subnet = ((configurations[0].get("properties") or {}).get("subnet") or {}).get("id")
        if not subnet:
            raise InfrastructureError(ErrorCode.INVALID_LOAD_BALANCER_CONFIGURATION, f"{machine.name} has no network interface Azure can read")
        return subnet

    def create(self, spec, planned, ctx, backends, network) -> LoadBalancerResource:
        if not backends:
            raise InfrastructureError(ErrorCode.INVALID_LOAD_BALANCER_CONFIGURATION, "A load balancer needs at least one machine behind it", resource=planned.name)
        unaddressed = [machine.name for machine in backends if not machine.internal_ip]
        if unaddressed:
            raise InfrastructureError(ErrorCode.INVALID_LOAD_BALANCER_CONFIGURATION, f"{', '.join(unaddressed)} has no private address yet", resource=planned.name)
        subnets = {machine.id: self._subnet_of(machine) for machine in backends}
        networks = {subnet.lower().split("/subnets/", 1)[0] for subnet in subnets.values()}
        if len(networks) > 1:
            raise InfrastructureError(ErrorCode.INVALID_LOAD_BALANCER_CONFIGURATION, "A load balancer's machines are on one network; these are on "
                                      f"{len(networks)}", resource=planned.name)
        vnet = next(iter(subnets.values())).split("/subnets/", 1)[0]
        name, region, hostnames = planned.name, ctx.region, list(spec.hostnames)
        group_id = self.groups.id(self.groups.ensure(ctx))
        ip_id = f"{group_id}/providers/Microsoft.Network/publicIPAddresses/{name}-ip"
        lb_id = f"{group_id}/providers/Microsoft.Network/loadBalancers/{name}"
        tags = translate.tags(ctx)
        components: list[dict] = []
        shape = {"id": planned.spec["resource_id"], "name": name, "request_id": ctx.request_id, "provider": "azure", "region": region, "zone": ctx.zone,
                 "type": spec.type, "port": spec.port, "backend_group": planned.spec["backend_group"], "backends": [machine.id for machine in backends],
                 "health_check_path": spec.health_check_path if spec.type == "http" else None, "hostnames": hostnames, "labels": tags}

        def taken(kind, resource_id, **extra):
            # known before Azure is asked, so a failure further on deletes every piece asked for so far
            components.append({"kind": kind, "name": resource_id.rsplit("/", 1)[-1], "url": resource_id, **extra})
            ctx.accepted(LoadBalancerResource(**shape, provider_id=lb_id, components=list(components), status=ResourceStatus.CREATING))

        zones = self.zones(region)
        address = {"location": region, "tags": tags, "sku": {"name": "Standard", "tier": "Regional"},
                   "properties": {"publicIPAllocationMethod": "Static", "publicIPAddressVersion": "IPv4"}}
        if zones:
            address["zones"] = list(zones)
        taken("public_ip", ip_id)
        made = self.api.put(self.api.arm(ip_id), address, VERSION, timeout=self.ceiling("network"))
        taken("load_balancer", lb_id)
        self.api.put(self.api.arm(lb_id), self._body(spec, lb_id, ip_id, region, tags), VERSION, timeout=self.ceiling("network"))
        self._fill_pool(lb_id, backends, vnet)
        for subnet in sorted(set(subnets.values())):
            nsg = self.network.subnet_security_group(subnet)
            if nsg is None:  # the operator's subnet: its security group is the operator's to open
                continue
            addresses = sorted(machine.internal_ip for machine in backends if subnets[machine.id] == subnet)
            taken("security_rule", f"{nsg}/securityRules/{name}-allow")
            self.network.admit(nsg, f"{name}-allow", FRONT_DOOR_SOURCE if hostnames else INTERNET, [spec.port], addresses)
        public = (made.get("properties") or {}).get("ipAddress") or (self.api.get(self.api.arm(ip_id), VERSION).get("properties") or {}).get("ipAddress")
        resource = LoadBalancerResource(**shape, provider_id=lb_id, address=public, components=list(components), status=ResourceStatus.READY)
        if hostnames:
            resource = self._front_door(spec, resource, public, group_id, tags, taken, components)
        return resource

    def _fill_pool(self, lb_id: str, backends, vnet: str) -> None:
        """The machines into the pool, by the pool's own call, and read back: Azure takes a load balancer whose PUT names
        IP-based members and keeps none of them (the real run of 2026-11-05), so that one would send traffic nowhere."""
        pool = f"{lb_id}/backendAddressPools/machines"
        members = [{"name": machine.name, "properties": {"ipAddress": machine.internal_ip, "virtualNetwork": {"id": vnet}}} for machine in backends]
        self.api.put(self.api.arm(pool), {"properties": {"loadBalancerBackendAddresses": members}}, VERSION, timeout=self.ceiling("network"))
        kept = (self.api.get(self.api.arm(pool), VERSION).get("properties") or {}).get("loadBalancerBackendAddresses") or []
        if {str((member.get("properties") or {}).get("ipAddress")) for member in kept} != {machine.internal_ip for machine in backends}:
            raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"Azure kept {len(kept)} of the load balancer's {len(backends)} machines in its pool",
                                      {"pool": pool.rsplit("/", 1)[-1]})

    @staticmethod
    def _body(spec, lb_id, ip_id, region, tags) -> dict:
        probe = {"protocol": "Http", "port": spec.port, "requestPath": spec.health_check_path} if spec.type == "http" else {"protocol": "Tcp", "port": spec.port}
        probe.update(intervalInSeconds=5, probeThreshold=2)
        return {"location": region, "tags": tags, "sku": {"name": "Standard", "tier": "Regional"}, "properties": {
            "frontendIPConfigurations": [{"name": "front", "properties": {"publicIPAddress": {"id": ip_id}}}],
            "backendAddressPools": [{"name": "machines"}],  # its members by the pool's own call (_fill_pool)
            "probes": [{"name": "health", "properties": probe}],
            "loadBalancingRules": [{"name": f"port-{spec.port}", "properties": {
                "frontendIPConfiguration": {"id": f"{lb_id}/frontendIPConfigurations/front"}, "backendAddressPool": {"id": f"{lb_id}/backendAddressPools/machines"},
                "probe": {"id": f"{lb_id}/probes/health"}, "protocol": "Tcp", "frontendPort": spec.port, "backendPort": spec.port, "enableFloatingIP": False,
                "idleTimeoutInMinutes": 4, "enableTcpReset": True, "disableOutboundSnat": True, "loadDistribution": "Default"}}]}}

    def _front_door(self, spec, resource, address: str, group_id: str, tags: dict, taken, components: list) -> LoadBalancerResource:
        name = resource.name
        profile = f"{group_id}/providers/Microsoft.Cdn/profiles/{name}-fd"
        endpoint = f"{profile}/afdEndpoints/{name}"
        origins = f"{profile}/originGroups/machines"
        taken("front_door", profile)

        def put(resource_id, body):
            return self.api.put(self.api.arm(resource_id), body, CDN_VERSION, timeout=self.ceiling("front_door"))

        made_profile = put(profile, {"location": "global", "tags": tags, "sku": {"name": "Standard_AzureFrontDoor"}, "properties": {"originResponseTimeoutSeconds": 60}})
        components[-1] = {**components[-1], "front_door_id": (made_profile.get("properties") or {}).get("frontDoorId")}
        made = put(endpoint, {"location": "global", "tags": tags, "properties": {"enabledState": "Enabled", "autoGeneratedDomainNameLabelScope": "TenantReuse"}})
        host = (made.get("properties") or {}).get("hostName")
        put(origins, {"properties": {"loadBalancingSettings": {"sampleSize": 4, "successfulSamplesRequired": 3, "additionalLatencyInMilliseconds": 50},
                                     "healthProbeSettings": {"probePath": spec.health_check_path, "probeProtocol": "Http", "probeRequestType": "GET",
                                                             "probeIntervalInSeconds": 30},
                                     "sessionAffinityState": "Disabled"}})
        put(f"{origins}/origins/load-balancer", {"properties": {"hostName": address, "httpPort": spec.port, "httpsPort": 443, "priority": 1, "weight": 1000,
                                                                "enabledState": "Enabled", "enforceCertificateNameCheck": True}})
        domains = []
        for hostname in spec.hostnames:
            domain = f"{profile}/customDomains/{domain_name(hostname)}"
            put(domain, {"properties": {"hostName": hostname, "tlsSettings": {"certificateType": "ManagedCertificate", "minimumTlsVersion": "TLS12"}}})
            domains.append(domain)
        put(f"{endpoint}/routes/default", {"properties": {"customDomains": [{"id": domain} for domain in domains], "originGroup": {"id": origins},
                                                          "supportedProtocols": ["Http", "Https"], "patternsToMatch": ["/*"], "forwardingProtocol": "HttpOnly",
                                                          "httpsRedirect": "Enabled", "linkToDefaultDomain": "Disabled", "enabledState": "Enabled"}})
        if self.settings.front_door_waf_policy:
            put(f"{profile}/securityPolicies/waf", {"properties": {"parameters": {
                "type": "WebApplicationFirewall", "wafPolicy": {"id": self.settings.front_door_waf_policy},
                "associations": [{"domains": [{"id": domain} for domain in domains], "patternsToMatch": ["/*"]}]}}})
        for hostname, domain in zip(spec.hostnames, domains, strict=True):
            components.append({"kind": "custom_domain", "name": hostname, "url": domain, "cname": host})
        return self._domains(resource.model_copy(update={"address": host, "components": list(components)}))

    def _domains(self, resource: LoadBalancerResource) -> LoadBalancerResource:
        """Each hostname's validation token and state, read again: the token the operator's TXT record holds, and the certificate's state."""
        components, states = [], []
        for component in resource.components:
            if component["kind"] == "custom_domain":
                found = self.api.find(self.api.arm(component["url"]), CDN_VERSION)
                properties = (found or {}).get("properties") or {}
                validation = properties.get("validationProperties") or {}
                component = {**component, "txt_name": f"_dnsauth.{component['name']}", "txt": validation.get("validationToken"),
                             "state": properties.get("domainValidationState")}
                states.append(component["state"])
            components.append(component)
        return resource.model_copy(update={"components": components, "certificate": certificate_state(states)})

    def get(self, resource: LoadBalancerResource) -> LoadBalancerResource:
        if self.api.find(self.api.arm(resource.provider_id), VERSION) is None:
            return resource.model_copy(update={"status": ResourceStatus.DELETED, "address": None})
        return self._domains(resource.model_copy(update={"status": ResourceStatus.READY}))

    def delete(self, resource: LoadBalancerResource, ctx) -> None:
        for component in reversed(resource.components):
            if component["kind"] not in TOP_LEVEL:
                continue
            version = CDN_VERSION if component["kind"] == "front_door" else VERSION
            self.api.delete(self.api.arm(component["url"]), version, timeout=self.ceiling("front_door" if component["kind"] == "front_door" else "network"))
