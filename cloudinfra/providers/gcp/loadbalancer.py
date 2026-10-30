"""Load balancers behind the generic primitive: an external application load balancer (http) or a passthrough network
load balancer (tcp) in front of one instance group. Each is several provider pieces; they are recorded in creation order
on the resource and torn down in reverse. With hostnames, the http one also serves HTTPS on 443 of the same address, with
a Google-managed certificate for them, which becomes ACTIVE once their DNS points at the address
(https://docs.cloud.google.com/load-balancing/docs/ssl-certificates/google-managed-certs)."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import LoadBalancerResource, ResourceStatus
from . import translate
from .api import COMPUTE
from .armor import GcpArmor

# Google's health checkers, per load balancer type (docs.cloud.google.com/load-balancing/docs/health-check-concepts, read
# 2026-10-28): the global external application load balancer probes from 35.191.0.0/16 and its front ends reach backends from
# 130.211.0.0/22; a backend-service passthrough network load balancer probes from 35.191.0.0/16, 209.85.204.0/22 and 209.85.152.0/22.
HEALTH_CHECK_RANGES = {"http": ["130.211.0.0/22", "35.191.0.0/16"], "tcp": ["35.191.0.0/16", "209.85.204.0/22", "209.85.152.0/22"]}
PORT_NAME = "app"


class GcpLoadBalancer:
    def __init__(self, api, settings, network_adapter):
        self.api, self.settings, self.network = api, settings, network_adapter
        self.armor = GcpArmor(api)

    def _global(self, collection):
        return f"{COMPUTE}/projects/{self.api.project}/global/{collection}"

    def _regional(self, region, collection):
        return f"{COMPUTE}/projects/{self.api.project}/regions/{region}/{collection}"

    def create(self, spec, planned, ctx, backends, network) -> LoadBalancerResource:
        if not backends:
            raise InfrastructureError(ErrorCode.INVALID_LOAD_BALANCER_CONFIGURATION, "A load balancer needs at least one machine behind it", resource=planned.name)
        name, zone, region = planned.name, backends[0].zone, ctx.region
        network_link = network.provider_id if network else f"{self._global('networks')}/{self.settings.default_network}"
        components = []
        shape = {"id": planned.spec["resource_id"], "name": name, "request_id": ctx.request_id, "provider": "gcp", "region": region, "zone": zone, "type": spec.type,
                 "port": spec.port, "backend_group": planned.spec["backend_group"], "backends": [machine.id for machine in backends],
                 "health_check_path": spec.health_check_path if spec.type == "http" else None, "labels": translate.labels(ctx), "hostnames": list(spec.hostnames)}

        def taken(kind, component_name, url):
            # each piece is known as soon as Google has taken its create, so a failure further on deletes every one made so far
            components.append({"kind": kind, "name": component_name, "url": url})
            ctx.accepted(LoadBalancerResource(**shape, provider_id=url, components=list(components), status=ResourceStatus.CREATING))

        def piece(kind, base, body, wait=True):
            operation = self.api.post(base, json=body)
            url = f"{base}/{body['name']}"
            taken(kind, body["name"], url)
            if wait:
                self.api.wait_compute(operation)
            return url

        group = piece("instance_group", f"{COMPUTE}/projects/{self.api.project}/zones/{zone}/instanceGroups",
                      {"name": f"{name}-group", "description": translate.description(ctx), "network": network_link, "namedPorts": [{"name": PORT_NAME, "port": spec.port}]})
        self.api.wait_compute(self.api.post(f"{group}/addInstances", json={"instances": [{"instance": machine.provider_id} for machine in backends]}))
        self.network._add_rule(network_link, f"{name}-allow-health", ctx, [spec.port], HEALTH_CHECK_RANGES[spec.type],
                               accepted=lambda rule: taken("firewall", rule.name, rule.provider_id))
        if spec.type == "http":
            health = piece("health_check", self._global("healthChecks"), {"name": f"{name}-health", "type": "HTTP", "checkIntervalSec": 10, "timeoutSec": 5,
                                                                          "httpHealthCheck": {"port": spec.port, "requestPath": spec.health_check_path}})
            protected = {}
            if spec.waf is not None:  # made before the backend that names it, so deleted after it
                protected["securityPolicy"] = piece("security_policy", self.armor.url(), self.armor.body(f"{name}-waf", spec.waf, ctx))
            backend = piece("backend_service", self._global("backendServices"), {"name": f"{name}-backend", "protocol": "HTTP", "portName": PORT_NAME, "timeoutSec": 30,
                                                                                 "loadBalancingScheme": "EXTERNAL_MANAGED", "healthChecks": [health],
                                                                                 "backends": [{"group": group, "balancingMode": "UTILIZATION"}], **protected})
            url_map = piece("url_map", self._global("urlMaps"), {"name": f"{name}-map", "defaultService": backend})
            proxy = piece("target_proxy", self._global("targetHttpProxies"), {"name": f"{name}-proxy", "urlMap": url_map})
            address = piece("address", self._global("addresses"), {"name": f"{name}-address", "ipVersion": "IPV4", "networkTier": "PREMIUM"})
            piece("forwarding_rule", self._global("forwardingRules"), {"name": f"{name}-rule", "IPAddress": address, "IPProtocol": "TCP", "portRange": str(spec.port), "target": proxy,
                                                                        "loadBalancingScheme": "EXTERNAL_MANAGED", "networkTier": "PREMIUM"})
            if spec.hostnames:
                certificate = piece("certificate", self._global("sslCertificates"), {"name": f"{name}-cert", "type": "MANAGED", "managed": {"domains": list(spec.hostnames)}})
                secure = piece("target_proxy", self._global("targetHttpsProxies"), {"name": f"{name}-https", "urlMap": url_map, "sslCertificates": [certificate]})
                piece("forwarding_rule", self._global("forwardingRules"), {"name": f"{name}-https-rule", "IPAddress": address, "IPProtocol": "TCP", "portRange": "443",
                                                                            "target": secure, "loadBalancingScheme": "EXTERNAL_MANAGED", "networkTier": "PREMIUM"})
        else:
            health = piece("health_check", self._regional(region, "healthChecks"), {"name": f"{name}-health", "type": "TCP", "checkIntervalSec": 10, "timeoutSec": 5,
                                                                                    "tcpHealthCheck": {"port": spec.port}})
            backend = piece("backend_service", self._regional(region, "backendServices"), {"name": f"{name}-backend", "protocol": "TCP", "loadBalancingScheme": "EXTERNAL",
                                                                                           "healthChecks": [health], "backends": [{"group": group}]})
            address = piece("address", self._regional(region, "addresses"), {"name": f"{name}-address", "networkTier": "PREMIUM"})
            piece("forwarding_rule", self._regional(region, "forwardingRules"), {"name": f"{name}-rule", "IPAddress": address, "IPProtocol": "TCP", "ports": [str(spec.port)],
                                                                                 "backendService": backend, "loadBalancingScheme": "EXTERNAL", "networkTier": "PREMIUM"})
        public = self.api.get(address).get("address")
        policy = next((item["name"] for item in components if item["kind"] == "security_policy"), None)
        found = LoadBalancerResource(**shape, provider_id=components[-1]["url"], address=public, components=components, status=ResourceStatus.READY, security_policy=policy)
        return self._certificate(found)

    def _certificate(self, resource: LoadBalancerResource) -> LoadBalancerResource:
        url = next((item["url"] for item in resource.components if item["kind"] == "certificate"), None)
        if url is None:
            return resource
        try:
            return resource.model_copy(update={"certificate": (self.api.get(url).get("managed") or {}).get("status")})
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
            return resource

    def get(self, resource: LoadBalancerResource) -> LoadBalancerResource:
        try:
            self.api.get(resource.provider_id)
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED, "address": None})
            raise
        return self._certificate(resource.model_copy(update={"status": ResourceStatus.READY}))

    def delete(self, resource: LoadBalancerResource, ctx) -> None:
        for component in reversed(resource.components):
            try:
                self.api.wait_compute(self.api.delete(component["url"]))
            except InfrastructureError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
