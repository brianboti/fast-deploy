"""Turning a release's bindings into the values the container gets, at the moment it is started. Secret values are read as
the environment's service account and exist only in the mapping this returns, which goes to the machine and is dropped."""
from dataclasses import dataclass

from .errors import DeliveryError, ErrorCode
from .google import read_secret, secret_version
from .infra import InfraRequest
from .models import MAX_VALUE, ReleaseRequest


def resolve(release: ReleaseRequest, infra: InfraRequest, client, token: str) -> dict[str, str]:
    """NAME → value for every binding. A value Docker's env file cannot carry (a line break, a NUL, too long) is refused
    by name, without the value."""
    values = {}
    for name, binding in release.env.items():
        if binding.value is not None:
            value = binding.value
        elif binding.endpoint is not None:
            found = infra.endpoints.get(binding.endpoint)
            if found in (None, "", []):
                raise DeliveryError(ErrorCode.INVALID_BINDING, f"env.{name}: the request has no {binding.endpoint}", {"field": f"env.{name}"})
            value = str(found)
        else:
            secret = infra.secret(binding.secret)
            if secret is None:
                raise DeliveryError(ErrorCode.INVALID_BINDING, f"env.{name}: the request has no secret {binding.secret!r}", {"field": f"env.{name}"})
            value = read_secret(client, token, secret.resource_name)
        if "\n" in value or "\r" in value or "\0" in value or len(value) > MAX_VALUE:
            raise DeliveryError(ErrorCode.INVALID_BINDING, f"env.{name}: its value holds a line break or a NUL, or is longer than {MAX_VALUE} characters, which "
                                "Docker's env file cannot carry; store it base64-encoded", {"field": f"env.{name}"})
        values[name] = value
    return values


@dataclass(frozen=True)
class SecretRef:
    """A secret the container reads itself at start (Cloud Run's secretKeyRef), pinned to a version; never its value."""
    resource_name: str
    version: str


def references(release: ReleaseRequest, infra: InfraRequest, client, token: str) -> dict[str, str | SecretRef]:
    """NAME → value for plain bindings and endpoints, NAME → SecretRef for secrets, each pinned to the version current now."""
    found: dict[str, str | SecretRef] = {}
    for name, binding in release.env.items():
        if binding.secret is None:
            found.update(resolve(release.model_copy(update={"env": {name: binding}}), infra, client, token))
            continue
        secret = infra.secret(binding.secret)
        if secret is None:
            raise DeliveryError(ErrorCode.INVALID_BINDING, f"env.{name}: the request has no secret {binding.secret!r}", {"field": f"env.{name}"})
        found[name] = SecretRef(secret.resource_name, secret_version(client, token, secret.resource_name))
    return found


def env_file(values: dict[str, str]) -> str:
    """Docker's --env-file text: one NAME=value per line, no quoting."""
    return "".join(f"{name}={value}\n" for name, value in values.items())
