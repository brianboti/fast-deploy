"""Names of Azure resources: the request's prefix, kept within each type's rules, with a salt where a name must be unique
across all of Azure (learn.microsoft.com/azure/azure-resource-manager/management/resource-name-rules, 2026-08-05).

Six kinds of name are global: a storage account, a key vault, a container registry, a Service Bus namespace, a
PostgreSQL or MySQL flexible server, and a managed Redis. A request's name is not unique, so those names end in the
first eight hex digits of the request id, as Google's bucket and secret names end in six. The name is pure: plan makes
the same one every time. A request made again gets a new id and so a new name, which matters because a deleted key
vault's name stays reserved through its soft-delete period and a Service Bus namespace's for four hours.
"""
import re

SALT = 8
# kind: (shortest, longest, the whole name's rule)
RULES = {
    "resource_group": (1, 90, re.compile(r"^[A-Za-z0-9_.()-]*[A-Za-z0-9_()-]$")),
    "virtual_machine": (1, 64, re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9_]$|^[A-Za-z0-9]$")),
    "storage_account": (3, 24, re.compile(r"^[a-z0-9]+$")),
    "key_vault": (3, 24, re.compile(r"^[A-Za-z](?!.*--)[A-Za-z0-9-]*[A-Za-z0-9]$")),
    "container_registry": (5, 50, re.compile(r"^[A-Za-z0-9]+$")),
    "service_bus": (6, 50, re.compile(r"^[A-Za-z][A-Za-z0-9-]*[A-Za-z0-9]$")),
    "flexible_server": (3, 63, re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")),
    "redis": (1, 60, re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")),
}
GLOBAL = ("storage_account", "key_vault", "container_registry", "service_bus", "flexible_server", "redis")
# kinds whose names take no hyphen at all
SOLID = ("storage_account", "container_registry")


def problem(kind: str, name: str) -> str | None:
    """Why a name breaks its kind's rules, or None."""
    shortest, longest, rule = RULES[kind]
    if not shortest <= len(name) <= longest:
        return f"an Azure {kind.replace('_', ' ')} name has {shortest} to {longest} characters; {name!r} has {len(name)}"
    if not rule.fullmatch(name):
        return f"{name!r} breaks the naming rule of an Azure {kind.replace('_', ' ')}"
    return None


def resource_group(prefix: str, request_id: str) -> str:
    """The request's resource group: its prefix and the request id's first eight hex digits, unique in the subscription."""
    return f"{prefix}-{request_id[:SALT]}"


def global_name(kind: str, prefix: str, request_id: str, part: str = "") -> str:
    """A name unique across all of Azure for a resource of the request: the prefix (and a part, such as db), cut to fit,
    then the request id's first eight hex digits."""
    if kind not in GLOBAL:
        raise ValueError(f"{kind} names are not global")
    salt = request_id[:SALT].lower()
    stem = f"{prefix}-{part}" if part else prefix
    longest = RULES[kind][1]
    if kind in SOLID:
        stem = re.sub(r"[^a-z0-9]", "", stem.lower())
        name = stem[:longest - SALT] + salt
    else:
        stem = re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9-]", "-", stem)).strip("-")
        if kind == "flexible_server":
            stem = stem.lower()
        name = f"{stem[:longest - SALT - 1].rstrip('-')}-{salt}"
    found = problem(kind, name)
    if found:  # the request name and the part are already checked; this is the last word, never expected to speak
        raise ValueError(found)
    return name
