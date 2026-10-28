"""The provider registry: a name in a request becomes an adapter here, and nowhere else.

Adding a provider is one entry: `register("aws", lambda config: AWSProvider(config.providers.aws))`. A name without an entry
is refused with UNSUPPORTED_PROVIDER; a registered one the operator has not configured, with the same code and a sentence
that says so.
"""
from collections.abc import Callable

from ..models.errors import ErrorCode, InfrastructureError
from .base import CloudProvider

Factory = Callable[[object], CloudProvider]
KNOWN: dict[str, Factory] = {}


def register(name: str, factory: Factory) -> None:
    KNOWN[name] = factory


def _gcp(config):
    from .gcp.provider import GCPProvider
    return GCPProvider(config.providers.gcp)


register("gcp", _gcp)
PLANNED_PROVIDERS = ("aws", "azure", "jetstream")  # named in the design; adapters not written yet


class ProviderRegistry:
    """Holds one adapter per configured provider; tests pass ready-made adapters instead of factories."""

    def __init__(self, config, adapters: dict[str, CloudProvider] | None = None):
        self.config = config
        self.adapters: dict[str, CloudProvider] = dict(adapters or {})

    def names(self) -> list[str]:
        return sorted(set(self.adapters) | {name for name in KNOWN if getattr(self.config.providers, name, None) is not None})

    def get(self, name: str) -> CloudProvider:
        if name in self.adapters:
            return self.adapters[name]
        if name not in KNOWN:
            planned = " Planned adapters: " + ", ".join(PLANNED_PROVIDERS) + "." if name in PLANNED_PROVIDERS else ""
            raise InfrastructureError(ErrorCode.UNSUPPORTED_PROVIDER, f"Provider {name!r} has no adapter in this service; available: {', '.join(self.names()) or 'none'}.{planned}",
                                      {"provider": name, "available": self.names()})
        if getattr(self.config.providers, name, None) is None:
            raise InfrastructureError(ErrorCode.UNSUPPORTED_PROVIDER, f"Provider {name!r} is not configured by the operator of this service; available: {', '.join(self.names()) or 'none'}",
                                      {"provider": name, "available": self.names()})
        self.adapters[name] = KNOWN[name](self.config)
        return self.adapters[name]
