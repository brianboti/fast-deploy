"""Who is granted access to what a request creates. The requester comes first when the operator maps its principal to an
identity; then the request's own access list, each member checked against the operator's allowed domains. Principals' own
identities may always be named; anyone else must be in one of the allowed domains."""
from ..models.errors import ErrorCode
from ..providers.base import Problem
from ..security import validation


def allowed(member: str, config) -> bool:
    identities = {principal.identity for principal in config.principals if principal.identity}
    return member in identities or validation.member_domain(member) in [domain.lower() for domain in config.access.allowed_domains]


def grants_nobody(config) -> bool:
    """True when the operator's settings let this service grant access to no one at all."""
    return config.access.max_members == 0 or not (config.access.allowed_domains or any(principal.identity for principal in config.principals))


def refusal(member: str, config) -> str:
    if config.access.allowed_domains:
        return f"{member} is outside the domains this service grants to: {', '.join(config.access.allowed_domains)}"
    return f"{member} is not the identity of one of this service's principals, and the operator allows no other members (access.allowed_domains)"


def problems(request, config, principal=None) -> list[Problem]:
    found = [Problem(ErrorCode.INVALID_ACCESS, refusal(entry.member, config), "access") for entry in request.access if not allowed(entry.member, config)]
    for address in (request.monitor.notify if request.monitor else []):  # alerts go only where access may
        if not allowed(f"user:{address}", config):
            found.append(Problem(ErrorCode.INVALID_ACCESS, refusal(address, config), "monitor.notify"))
    for address in (request.budget.notify if request.budget else []):
        if not allowed(f"user:{address}", config):
            found.append(Problem(ErrorCode.INVALID_ACCESS, refusal(address, config), "budget.notify"))
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
