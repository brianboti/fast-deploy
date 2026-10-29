"""Structured errors: every refusal has a code, a sentence and details, in the same shape as the infrastructure layer's."""
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(StrEnum):
    INVALID_RELEASE = "INVALID_RELEASE"
    INVALID_BINDING = "INVALID_BINDING"
    UNKNOWN_ENVIRONMENT = "UNKNOWN_ENVIRONMENT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    INVALID_STATE = "INVALID_STATE"
    NOT_GRANTED = "NOT_GRANTED"
    UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"
    UNREACHABLE_TARGET = "UNREACHABLE_TARGET"
    REGISTRY_NOT_ALLOWED = "REGISTRY_NOT_ALLOWED"
    ATTESTATION_FAILED = "ATTESTATION_FAILED"
    INFRA_UNAVAILABLE = "INFRA_UNAVAILABLE"
    INFRA_AUTH_FAILED = "INFRA_AUTH_FAILED"
    GOOGLE_AUTH_FAILED = "GOOGLE_AUTH_FAILED"
    SECRET_UNAVAILABLE = "SECRET_UNAVAILABLE"  # noqa: S105 - an error code, not a credential
    MACHINE_UNREACHABLE = "MACHINE_UNREACHABLE"
    MACHINE_ERROR = "MACHINE_ERROR"
    CLUSTER_UNREACHABLE = "CLUSTER_UNREACHABLE"
    CLUSTER_ERROR = "CLUSTER_ERROR"
    PULL_FAILED = "PULL_FAILED"
    UNHEALTHY = "UNHEALTHY"
    ROLLOUT_FAILED = "ROLLOUT_FAILED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    REMOVE_FAILED = "REMOVE_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class DeliveryError(ValueError):
    """A failure with a structured code, rendered as {"status": "FAILED", "error": {...}} outside. A ValueError, so that one
    raised in a pydantic validator becomes part of the model's report with its code kept (problems_from reads it back)."""

    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code, self.message, self.details = ErrorCode(code), message, dict(details or {})

    def __str__(self):
        return f"{self.code}: {self.message}"

    def detail(self) -> ErrorDetail:
        return ErrorDetail(code=self.code, message=self.message, details=self.details)

    def to_dict(self) -> dict[str, Any]:
        return {"status": "FAILED", "error": self.detail().model_dump(mode="json")}


CODED = re.compile(r"^(?:Value error, )?([A-Z_]+): (.*)$", re.DOTALL)


def problems_from(error: Any) -> list[ErrorDetail]:
    """One ErrorDetail per problem of a pydantic ValidationError: a coded message keeps its code, any other is INVALID_RELEASE
    naming its field."""
    problems = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        message = re.sub(r"^Value error, ", "", str(item.get("msg", "invalid")))
        found = CODED.match(message)
        if found and found.group(1) in ErrorCode.__members__:
            code, text = ErrorCode(found.group(1)), found.group(2)
        elif item.get("type") == "extra_forbidden":
            code, text = ErrorCode.INVALID_RELEASE, f"{location} is not a field of a release"
        else:
            code, text = ErrorCode.INVALID_RELEASE, f"{location}: {message}" if location else message
        problems.append(ErrorDetail(code=code, message=text, details={"field": location} if location else {}))
    return problems
