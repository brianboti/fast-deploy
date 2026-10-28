"""Load balancers behind the generic primitive: an external application load balancer (http) or a passthrough network
load balancer (tcp) in front of one instance group. Each is several provider pieces; they are recorded in creation order
on the resource and torn down in reverse."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.resources import LoadBalancerResource, ResourceStatus
from . import translate
from .api import COMPUTE

HEALTH_CHECK_RANGES = ["130.211.0.0/22", "35.191.0.0/16"]  # Google's health checkers and proxies
PORT_NAME = "app"


class GcpLoadBalancer:
    def __init__(self, api, settings, network_adapter):
        self.api, self.settings, self.network = api, settings, network_adapter

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

        def piece(kind, base, body, wait=True):
            operation = self.api.post(base, json=body)
            if wait:
                self.api.wait_compute(operation)
            url = f"{base}/{body['name']}"
            components.append({"kind": kind, "name": body["name"], "url": url})
            return url

        group = piece("instance_group", f"{COMPUTE}/projects/{self.api.project}/zones/{zone}/instanceGroups",
                      {"name": f"{name}-group", "description": translate.description(ctx), "network": network_link, "namedPorts": [{"name": PORT_NAME, "port": spec.port}]})
        self.api.wait_compute(self.api.post(f"{group}/addInstances", json={"instances": [{"instance": machine.provider_id} for machine in backends]}))
        rule = self.network._add_rule(network_link, f"{name}-allow-health", ctx, [spec.port], HEALTH_CHECK_RANGES)
        components.append({"kind": "firewall", "name": rule.name, "url": rule.provider_id})
        if spec.type == "http":
            health = piece("health_check", self._global("healthChecks"), {"name": f"{name}-health", "type": "HTTP", "checkIntervalSec": 10, "timeoutSec": 5,
                                                                          "httpHealthCheck": {"port": spec.port, "requestPath": spec.health_check_path}})
            backend = piece("backend_service", self._global("backendServices"), {"name": f"{name}-backend", "protocol": "HTTP", "portName": PORT_NAME, "timeoutSec": 30,
                                                                                 "loadBalancingScheme": "EXTERNAL_MANAGED", "healthChecks": [health],
                                                                                 "backends": [{"group": group, "balancingMode": "UTILIZATION"}]})
            url_map = piece("url_map", self._global("urlMaps"), {"name": f"{name}-map", "defaultService": backend})
            proxy = piece("target_proxy", self._global("targetHttpProxies"), {"name": f"{name}-proxy", "urlMap": url_map})
            address = piece("address", self._global("addresses"), {"name": f"{name}-address", "ipVersion": "IPV4", "networkTier": "PREMIUM"})
            piece("forwarding_rule", self._global("forwardingRules"), {"name": f"{name}-rule", "IPAddress": address, "IPProtocol": "TCP", "portRange": str(spec.port), "target": proxy,
                                                                        "loadBalancingScheme": "EXTERNAL_MANAGED", "networkTier": "PREMIUM"})
        else:
            health = piece("health_check", self._regional(region, "healthChecks"), {"name": f"{name}-health", "type": "TCP", "checkIntervalSec": 10, "timeoutSec": 5,
                                                                                    "tcpHealthCheck": {"port": spec.port}})
            backend = piece("backend_service", self._regional(region, "backendServices"), {"name": f"{name}-backend", "protocol": "TCP", "loadBalancingScheme": "EXTERNAL",
                                                                                           "healthChecks": [health], "backends": [{"group": group}]})
            address = piece("address", self._regional(region, "addresses"), {"name": f"{name}-address", "networkTier": "PREMIUM"})
            piece("forwarding_rule", self._regional(region, "forwardingRules"), {"name": f"{name}-rule", "IPAddress": address, "IPProtocol": "TCP", "ports": [str(spec.port)],
                                                                                 "backendService": backend, "loadBalancingScheme": "EXTERNAL", "networkTier": "PREMIUM"})
        public = self.api.get(address).get("address")
        return LoadBalancerResource(id=planned.spec["resource_id"], name=name, request_id=ctx.request_id, provider="gcp", region=region, zone=zone, provider_id=components[-1]["url"],
                                    type=spec.type, address=public, port=spec.port, backend_group=planned.spec["backend_group"], backends=[machine.id for machine in backends],
                                    health_check_path=spec.health_check_path if spec.type == "http" else None, components=components, status=ResourceStatus.READY,
                                    labels=translate.labels(ctx))

    def get(self, resource: LoadBalancerResource) -> LoadBalancerResource:
        try:
            self.api.get(resource.provider_id)
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED, "address": None})
            raise
        return resource.model_copy(update={"status": ResourceStatus.READY})

    def delete(self, resource: LoadBalancerResource, ctx) -> None:
        for component in reversed(resource.components):
            try:
                self.api.wait_compute(self.api.delete(component["url"]))
            except InfrastructureError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
