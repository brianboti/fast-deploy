"""Who is granted access to what a request creates. The requester comes first when the operator maps its principal to an
identity; then the request's own access list, each member checked against the operator's allowed domains. With no allowed
domains, only principals' own identities may be named."""
from ..models.errors import ErrorCode
from ..providers.base import Problem
from ..security import validation


def allowed(member: str, config) -> bool:
    domains = [domain.lower() for domain in config.access.allowed_domains]
    if domains:
        return validation.member_domain(member) in domains
    return member in {principal.identity for principal in config.principals if principal.identity}


def refusal(member: str, config) -> str:
    if config.access.allowed_domains:
        return f"{member} is outside the domains this service grants to: {', '.join(config.access.allowed_domains)}"
    return f"{member} is not the identity of one of this service's principals, and the operator allows no other members (access.allowed_domains)"


def problems(request, config, principal=None) -> list[Problem]:
    found = [Problem(ErrorCode.INVALID_ACCESS, refusal(entry.member, config), "access") for entry in request.access if not allowed(entry.member, config)]
    count = len(members(request, principal, config))
    if count > config.access.max_members:
        found.append(Problem(ErrorCode.INVALID_ACCESS, f"At most {config.access.max_members} members per request, the requester included; {count} asked", "access"))
    return found


def members(request, principal, config) -> list[dict]:
    """The access list a request starts with: the requester as admin, then the members it names."""
    found = []
    if config.access.grant_requester and config.access.max_members > 0 and principal is not None and getattr(principal, "identity", None):
        found.append({"member": principal.identity, "level": "admin"})
    for entry in request.access:
        known = next((item for item in found if item["member"] == entry.member), None)
        if known is not None:
            known["level"] = entry.level
        else:
            found.append({"member": entry.member, "level": entry.level})
    return [{**item, "status": "PENDING", "resources": [], "error": None, "updated_at": None} for item in found]


def outcome(granted: list[str], errors: list) -> tuple[str, dict | None]:
    """The status of one member after a round of grants, and the first error."""
    if errors and not granted:
        return "FAILED", errors[0]
    if errors:
        return "PARTIAL", errors[0]
    return "GRANTED", None
