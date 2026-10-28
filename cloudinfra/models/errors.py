"""Structured errors: every failure the service reports has a code, a sentence and details, never a raw provider exception."""
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    UNSUPPORTED_PROVIDER = "UNSUPPORTED_PROVIDER"
    INVALID_REGION = "INVALID_REGION"
    INVALID_RESOURCE_NAME = "INVALID_RESOURCE_NAME"
    INVALID_COMPUTE_CONFIGURATION = "INVALID_COMPUTE_CONFIGURATION"
    INVALID_STORAGE_CONFIGURATION = "INVALID_STORAGE_CONFIGURATION"
    INVALID_NETWORK_CONFIGURATION = "INVALID_NETWORK_CONFIGURATION"
    INVALID_DATABASE_CONFIGURATION = "INVALID_DATABASE_CONFIGURATION"
    INVALID_CACHE_CONFIGURATION = "INVALID_CACHE_CONFIGURATION"
    INVALID_CLUSTER_CONFIGURATION = "INVALID_CLUSTER_CONFIGURATION"
    INVALID_LOAD_BALANCER_CONFIGURATION = "INVALID_LOAD_BALANCER_CONFIGURATION"
    UNSUPPORTED_RESOURCE_KIND = "UNSUPPORTED_RESOURCE_KIND"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    COST_LIMIT_EXCEEDED = "COST_LIMIT_EXCEEDED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_STATE = "INVALID_STATE"
    PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"
    PROVIDER_PERMISSION_DENIED = "PROVIDER_PERMISSION_DENIED"
    PROVIDER_CONFLICT = "PROVIDER_CONFLICT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    OPERATION_TIMEOUT = "OPERATION_TIMEOUT"
    PROVISIONING_FAILED = "PROVISIONING_FAILED"
    DESTROY_FAILED = "DESTROY_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorDetail(BaseModel):
    """The error object every FAILED result carries."""
    model_config = ConfigDict(extra="forbid")
    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    resource: str | None = None  # the resource id or name the error is about, when there is one


class InfrastructureError(ValueError):
    """A failure with a structured code. Raised inside the service, rendered as {"status": "FAILED", "error": {...}} outside.

    A ValueError, so that one raised inside a pydantic validator becomes part of the model's validation report; its text
    starts with the code, which `problems_from` reads back so no field-level code is lost on the way.
    """

    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None, resource: str | None = None):
        super().__init__(message)
        self.code, self.message, self.details, self.resource = ErrorCode(code), message, dict(details or {}), resource

    def __str__(self):
        return f"{self.code}: {self.message}"

    def detail(self) -> ErrorDetail:
        return ErrorDetail(code=self.code, message=self.message, details=self.details, resource=self.resource)

    def to_dict(self) -> dict[str, Any]:
        return {"status": "FAILED", "error": self.detail().model_dump(mode="json")}


def invalid(code: ErrorCode, message: str, **details: Any) -> InfrastructureError:
    return InfrastructureError(code, message, details)


CODED = re.compile(r"^(?:Value error, )?([A-Z_]+): (.*)$", re.DOTALL)


def problems_from(error: Any) -> list[ErrorDetail]:
    """The problems a pydantic ValidationError describes, one ErrorDetail each: a coded message keeps its code, any other
    becomes INVALID_REQUEST with the field it names."""
    problems = []
    for item in _without_union_noise(error.errors()):
        location = ".".join(str(part) for part in item.get("loc", ()))
        message = re.sub(r"^Value error, ", "", str(item.get("msg", "invalid")))
        found = CODED.match(message)
        code, text = (ErrorCode(found.group(1)), found.group(2)) if found and found.group(1) in ErrorCode.__members__ else (ErrorCode.INVALID_REQUEST, message)
        details = {"field": location} if location else {}
        named = bool(found)  # a coded message names its own field
        if item.get("type") == "extra_forbidden" and len(item.get("loc", ())) == 1:
            code, text, named = ErrorCode.UNSUPPORTED_RESOURCE_KIND, unknown_kind_message(str(item["loc"][0])), True
            details["nearest"] = nearest_kind(str(item["loc"][0]))
        elif item.get("type") == "extra_forbidden":
            text, named = f"{location} is not a field of the request", True
        problems.append(ErrorDetail(code=code, message=text if named or not location else f"{location}: {text}", details=details))
    return problems


def nearest_kind(name: str) -> str | None:
    """The resource kind an unknown top-level block most likely meant (loadbalancer → load_balancer, vm → compute)."""
    import difflib

    from .request import RESOURCE_KINDS
    aliases = {"vm": "compute", "vms": "compute", "instance": "compute", "instances": "compute", "server": "compute", "machine": "compute", "machines": "compute",
               "disk": "storage", "disks": "storage", "bucket": "storage", "volume": "storage", "vpc": "network", "subnet": "network", "firewall": "network",
               "db": "database", "postgres": "database", "postgresql": "database", "mysql": "database", "sql": "database", "redis": "cache", "memcache": "cache",
               "topic": "queue", "topics": "queue", "pubsub": "queue", "messaging": "queue", "secrets": "secret", "kubernetes": "cluster", "k8s": "cluster", "gke": "cluster",
               "loadbalancer": "load_balancer", "lb": "load_balancer", "balancer": "load_balancer", "ingress": "load_balancer"}
    lowered = name.lower().replace("-", "_")
    if lowered in aliases:
        return aliases[lowered]
    near = difflib.get_close_matches(lowered, RESOURCE_KINDS, n=1, cutoff=0.6)
    return near[0] if near else None


def unknown_kind_message(name: str) -> str:
    from .request import RESOURCE_KINDS
    guess = nearest_kind(name)
    hint = f" (did you mean {guess}?)" if guess else ""
    return f"{name} is not a resource kind this service provides{hint}; kinds: {', '.join(RESOURCE_KINDS)}. Other top-level fields: provider, region, zone, name, labels."


UNION_TAG = re.compile(r"^(?:[A-Z][A-Za-z]+|list\[[A-Za-z]+\])$")
BRANCH_TYPES = ("list_type", "model_type", "dict_type", "model_attributes_type")


def _without_union_noise(errors: list) -> list:
    """A field that takes one block or a list of them makes pydantic report both branches; keep the branch that matched the
    input's shape, and name the field without the branch tag (compute.cpu, not compute.ComputeRequest.cpu)."""
    cleaned = []
    for item in errors:
        loc = tuple(item.get("loc", ()))
        tagged = len(loc) >= 2 and isinstance(loc[1], str) and UNION_TAG.match(loc[1]) is not None
        cleaned.append({**item, "loc": (loc[0], *loc[2:]) if tagged else loc, "_branch": tagged})
    fields_with_detail = {item["loc"][0] for item in cleaned if item["_branch"] and item.get("type") not in BRANCH_TYPES}
    return [item for item in cleaned if not (item["_branch"] and item.get("type") in BRANCH_TYPES and item["loc"][0] in fields_with_detail)]
