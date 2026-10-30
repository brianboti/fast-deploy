from typing import Annotated

from ...models.resources import ResourceKind
from ._common import Result, Wait, field, register


def tools(mcp, orchestrator, wrap, identity):
    def create_network(region: Annotated[str, field("create_network", "region")], provider: Annotated[str, field("create_network", "provider")] = "gcp",
                       public_access: Annotated[bool, field("create_network", "public_access")] = False, cidr: Annotated[str, field("create_network", "cidr")] = "10.10.0.0/24",
                       allowed_ports: Annotated[list[int] | None, field("create_network", "allowed_ports")] = None,
                       allowed_sources: Annotated[list[str] | None, field("create_network", "allowed_sources")] = None,
                       iap_ssh: Annotated[bool, field("create_network", "iap_ssh")] = True, nat: Annotated[bool, field("create_network", "nat")] = False,
                       name: Annotated[str | None, field("create_network", "name")] = None,
                       labels: Annotated[dict[str, str] | None, field("create_network", "labels")] = None, wait_seconds: Wait = 0) -> Result:
        return orchestrator.create_network(identity(), region=region, provider=provider, public_access=public_access, cidr=cidr, allowed_ports=allowed_ports,
                                           allowed_sources=allowed_sources, iap_ssh=iap_ssh, nat=nat, name=name, labels=labels, wait_seconds=wait_seconds)

    def get_network(resource_id: Annotated[str, field("get_network", "resource_id")]) -> Result:
        return orchestrator.get_resource(resource_id, identity(), ResourceKind.network)

    def configure_firewall(network_id: Annotated[str, field("configure_firewall", "network_id")], ports: Annotated[list[int], field("configure_firewall", "ports")],
                           sources: Annotated[list[str], field("configure_firewall", "sources")]) -> Result:
        return orchestrator.configure_firewall(network_id, ports, sources, identity())

    def delete_network(resource_id: Annotated[str, field("delete_network", "resource_id")], wait_seconds: Wait = 0) -> Result:
        return orchestrator.delete_resource(resource_id, identity(), ResourceKind.network, wait_seconds)

    for fn in (create_network, get_network, configure_firewall, delete_network):
        register(mcp, wrap, fn)
