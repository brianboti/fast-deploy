"""The rules every request value must pass before anything reaches a provider: names, regions, addresses, ports, labels.

Each check raises a structured error. Nothing here accepts a command, a script, a key or a provider-specific structure; the
contract carries sizes, names, versions and switches only, so no parameter can become executable code anywhere downstream.
"""
import ipaddress
import re

from ..models.errors import ErrorCode, InfrastructureError

# A request name: the prefix of every resource name, so it stays short enough for every provider's own limits (the common
# limit is 63 characters for a resource name, and the longest suffix this service adds is "-net-allow-internal").
NAME = re.compile(r"^[a-z](?:[a-z0-9-]{0,18}[a-z0-9])?$")
REGION = re.compile(r"^[a-z]{2,20}-[a-z]{2,20}[0-9]{1,2}(?:-[0-9]{1,2})?$")  # us-central1 (Google), us-east-1 (AWS)
ZONE = re.compile(r"^[a-z0-9-]{3,32}$")
LABEL_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
LABEL_VALUE = re.compile(r"^[a-z0-9_-]{0,63}$")
RESERVED_LABEL_PREFIXES = ("cloudinfra", "managed-by")
MAX_LABELS = 16
MAX_PORTS = 32
MAX_SOURCES = 16
# Who may be granted access to what a request creates: a person, a group or a service account, by email address. Never
# allUsers, allAuthenticatedUsers or a whole domain.
MEMBER = re.compile(r"^(user|group|serviceAccount):[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
IDENTIFIER = re.compile(r"^[a-z]{2,8}-[a-f0-9]{12}$")  # a resource id this service issued (cmp-…, sto-…, net-…, db-…)
REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")
PRIVATE_RANGES = [ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
# Ranges a provider's own identity-aware proxy reaches machines from: not public, though outside the private ranges.
PROXY_RANGES = [ipaddress.ip_network("35.235.240.0/20")]  # Google's Identity-Aware Proxy TCP forwarding (translate.IAP_RANGE)
CLOSED_PUBLIC_PORTS = (22, 3389)  # never opened to a public range: SSH goes through the proxy, RDP is not offered


def check_name(name: str, what: str = "name") -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise InfrastructureError(ErrorCode.INVALID_RESOURCE_NAME,
                                  f"{what} must be 1 to 20 lowercase letters, digits and dashes, starting with a letter and not ending with a dash",
                                  {"field": what, "value": name})
    return name


def check_region(region: str, allowed: list[str] | tuple[str, ...] | None = None) -> str:
    if not isinstance(region, str) or not REGION.fullmatch(region):
        raise InfrastructureError(ErrorCode.INVALID_REGION, f"{region!r} is not a region name", {"field": "region", "value": region})
    if allowed is not None and region not in allowed:
        raise InfrastructureError(ErrorCode.INVALID_REGION, f"Region {region} is not allowed by this service; allowed: {', '.join(allowed)}",
                                  {"field": "region", "value": region, "allowed": list(allowed)})
    return region


def check_cidr(cidr: str, private: bool = True, field: str = "cidr") -> str:
    try:
        network = ipaddress.IPv4Network(cidr, strict=True)
    except (ValueError, TypeError):
        raise InfrastructureError(ErrorCode.INVALID_NETWORK_CONFIGURATION, f"{field} must be an IPv4 network in CIDR form (10.10.0.0/24)",
                                  {"field": field, "value": cidr}) from None
    if private:
        if not 8 <= network.prefixlen <= 29:
            raise InfrastructureError(ErrorCode.INVALID_NETWORK_CONFIGURATION, f"{field} must have a prefix between /8 and /29", {"field": field, "value": cidr})
        if not any(network.subnet_of(private_range) for private_range in PRIVATE_RANGES):
            raise InfrastructureError(ErrorCode.INVALID_NETWORK_CONFIGURATION, f"{field} must be a private range (10/8, 172.16/12 or 192.168/16)",
                                      {"field": field, "value": cidr})
    return str(network)


def check_sources(sources: list[str]) -> list[str]:
    if not sources or len(sources) > MAX_SOURCES:
        raise InfrastructureError(ErrorCode.INVALID_NETWORK_CONFIGURATION, f"allowed_sources takes 1 to {MAX_SOURCES} IPv4 ranges", {"field": "allowed_sources"})
    seen = []
    for source in sources:
        try:
            network = ipaddress.IPv4Network(source, strict=False)
        except (ValueError, TypeError):
            raise InfrastructureError(ErrorCode.INVALID_NETWORK_CONFIGURATION, f"allowed_sources entry {source!r} is not an IPv4 range",
                                      {"field": "allowed_sources", "value": source}) from None
        if str(network) in seen:
            raise InfrastructureError(ErrorCode.INVALID_NETWORK_CONFIGURATION, "allowed_sources must be unique", {"field": "allowed_sources"})
        seen.append(str(network))
    return seen


def check_ports(ports: list[int], field: str = "allowed_ports") -> list[int]:
    if not ports or len(ports) > MAX_PORTS:
        raise InfrastructureError(ErrorCode.INVALID_NETWORK_CONFIGURATION, f"{field} takes 1 to {MAX_PORTS} ports", {"field": field})
    for port in ports:
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise InfrastructureError(ErrorCode.INVALID_NETWORK_CONFIGURATION, f"{field} entries are ports from 1 to 65535", {"field": field, "value": port})
    if len(set(ports)) != len(ports):
        raise InfrastructureError(ErrorCode.INVALID_NETWORK_CONFIGURATION, f"{field} must be unique", {"field": field})
    return sorted(ports)


def is_public_source(source: str) -> bool:
    """A range outside the private ranges and the provider's proxy range."""
    network = ipaddress.IPv4Network(source, strict=False)
    return not any(network.subnet_of(known) for known in (*PRIVATE_RANGES, *PROXY_RANGES))


def check_public_rule(ports: list[int], sources: list[str], closed: tuple[int, ...] | list[int] = CLOSED_PUBLIC_PORTS, field: str = "network.allowed_ports") -> None:
    """A rule that opens a closed port (22, 3389, and whatever the operator adds) from a public range is refused: SSH is
    reached through the provider's identity-aware proxy only, however the rule is asked for (the review of 2026-10-30,
    CIS GCP 3.6 and 3.7)."""
    opened = sorted(port for port in ports if port in closed)
    public = [source for source in sources if is_public_source(source)]
    if opened and public:
        raise InfrastructureError(ErrorCode.INVALID_NETWORK_CONFIGURATION,
                                  f"Port{'s' if len(opened) > 1 else ''} {', '.join(map(str, opened))} cannot be opened from {', '.join(public)}: this service never opens "
                                  "SSH or RDP to a public range; SSH goes through the identity-aware proxy (network.iap_ssh). Drop the port, or limit the sources "
                                  "to private ranges", {"field": field, "ports": opened, "sources": public})


def check_labels(labels: dict[str, str]) -> dict[str, str]:
    if len(labels) > MAX_LABELS:
        raise InfrastructureError(ErrorCode.INVALID_REQUEST, f"At most {MAX_LABELS} labels", {"field": "labels"})
    for key, value in labels.items():
        if not isinstance(key, str) or not LABEL_KEY.fullmatch(key):
            raise InfrastructureError(ErrorCode.INVALID_REQUEST, f"Label key {key!r}: lowercase letters, digits, dashes and underscores, starting with a letter, up to 63 characters",
                                      {"field": "labels", "key": key})
        if key.startswith(RESERVED_LABEL_PREFIXES):
            raise InfrastructureError(ErrorCode.INVALID_REQUEST, f"Label key {key!r} is reserved for the service's own labels", {"field": "labels", "key": key})
        if not isinstance(value, str) or not LABEL_VALUE.fullmatch(value):
            raise InfrastructureError(ErrorCode.INVALID_REQUEST, f"Label value for {key!r}: lowercase letters, digits, dashes and underscores, up to 63 characters",
                                      {"field": "labels", "key": key})
    return dict(labels)


def check_member(value: str, field: str = "access.member") -> str:
    """A member as it is compared and stored: its kind as written, its email address in lowercase, as Google accounts and
    groups do not tell case apart. A CI job's federated identity (principal://, principalSet://) is not a member: images
    are built only in the trusted project, so nothing outside pushes to a registry (decided 2026-10-30)."""
    if not isinstance(value, str) or not MEMBER.fullmatch(value):
        raise InfrastructureError(ErrorCode.INVALID_ACCESS, f"{value!r} is not a member: write user:, group: or serviceAccount: and an email address",
                                  {"field": field, "value": value})
    return normal_member(value)


def normal_member(value: str) -> str:
    kind, email = value.split(":", 1)
    return f"{kind}:{email.lower()}"


def member_domain(member: str) -> str:
    return member.rsplit("@", 1)[-1].lower()


def check_identifier(value: str, what: str = "resource_id") -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise InfrastructureError(ErrorCode.NOT_FOUND, f"{what} {value!r} is not an identifier this service issued", {"field": what, "value": value})
    return value


def check_request_id(value: str) -> str:
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise InfrastructureError(ErrorCode.NOT_FOUND, f"request_id {value!r} is not an identifier this service issued", {"field": "request_id", "value": value})
    return value
