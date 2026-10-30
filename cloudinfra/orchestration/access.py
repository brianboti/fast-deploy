"""Who is granted access to what a request creates. The requester comes first when the operator maps its principal to an
identity; then the request's own access list, each member checked against the operator's allowed domains. Principals' own
identities and the operator's registry writers may always be named; anyone else must be in one of the allowed domains.

Registries are the one kind where the level does not decide alone: only the operator's registry writers push, and only
there; every other member of a registry pulls (images are built only in the trusted project, decided 2026-10-30)."""
from ..models.errors import ErrorCode
from ..providers.base import Problem
from ..security import validation


def allowed(member: str, config) -> bool:
    identities = {principal.identity for principal in config.principals if principal.identity}
    return (member in identities or config.access.writes_registries(member)
            or validation.member_domain(member) in [domain.lower() for domain in config.access.allowed_domains])


def level_on(kind: str, member: str, level: str, config) -> str | None:
    """The level a member is granted on a resource of this kind, or None for no grant there. A registry writer is granted
    only on registries, and pushes only at admin; any other member of a registry pulls, whatever its level."""
    writer = config.access.writes_registries(member)
    if kind == "registry":
        return level if writer else "use"
    return None if writer else level


def grants_nobody(config) -> bool:
    """True when the operator's settings let this service grant access to no one at all."""
    return config.access.max_members == 0 or not (config.access.allowed_domains or any(principal.identity for principal in config.principals))


def refusal(member: str, config) -> str:
    if config.access.allowed_domains:
        return f"{member} is outside the domains this service grants to: {', '.join(config.access.allowed_domains)}"
    return f"{member} is not the identity of one of this service's principals, and the operator allows no other members (access.allowed_domains)"


def problems(request, config, principal=None) -> list[Problem]:
    found = [Problem(ErrorCode.INVALID_ACCESS, refusal(entry.member, config), "access") for entry in request.access if not allowed(entry.member, config)]
    count = len(members(request, principal, config))
    if count > config.access.max_members:
        found.append(Problem(ErrorCode.INVALID_ACCESS, f"At most {config.access.max_members} members per request, the requester included; {count} asked", "access"))
    if request.database is not None and not config.return_database_password and grants_nobody(config):
        found.append(Problem(ErrorCode.INVALID_ACCESS, "Nobody could read this database's password: it is written to a secret that only access members read, and this "
                             "service grants access to no one. The operator sets access.allowed_domains or a principal's identity, or return_database_password",
                             "database"))
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


def outcome(granted: list[str], errors: list, changing: list[str] = ()) -> tuple[str, dict | None]:
    """The status of one member after a round of grants, and the first error. Resources in the middle of a change are not
    granted on, and say so; with nothing in the request to grant on at all, the member is RECORDED, not GRANTED."""
    if not errors and changing:
        errors = [{"code": "INVALID_STATE", "message": f"{len(changing)} resources were changing ({', '.join(changing)}); grant again once they have settled",
                   "details": {"resources": list(changing)}}]
    if errors and not granted:
        return "FAILED", errors[0]
    if errors:
        return "PARTIAL", errors[0]
    return ("GRANTED" if granted else "RECORDED"), None
