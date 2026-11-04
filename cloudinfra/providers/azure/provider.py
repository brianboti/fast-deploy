"""AzureProvider: the CloudProvider interface implemented with Microsoft Azure.

Built in steps (docs/research/RESEARCH-2026-11-03-azure.md, section 10). This first step gives the adapter its
credential (auth.py), its client (api.py), the clouds, the regions and their zones, and the quota read. It provides no
kind yet: the validator refuses every block before any call, saying so, and every primitive the interface asks for
that this adapter does not have yet is refused with UNSUPPORTED_RESOURCE_KIND (NOT_YET below). Each kind lands with
its own change, adds itself to supported_kinds, and leaves NOT_YET by being written.
"""
import abc

from ...models.errors import ErrorCode, InfrastructureError
from ...models.request import InfrastructureRequest
from ...models.response import CostEstimate, PlannedResource
from ..base import CloudProvider, Problem, ProvisionContext
from .api import API_VERSIONS, AzureApi
from .auth import AzureToken
from .clouds import CLOUDS
from .regions import ZONES

# The quota names the orchestrator checks before it provisions, and the Compute usages they are read from
# (learn.microsoft.com/rest/api/compute/usage/list): regional vCPUs and virtual machines. Every family's vCPUs are kept
# too, under Azure's own names (standardDSv5Family…): a GPU family starts at zero in a new subscription.
QUOTA_NAMES = {"cores": "CPUS", "virtualMachines": "INSTANCES"}


class AzureProvider(CloudProvider):
    name = "azure"
    supported_kinds: tuple[str, ...] = ()
    stoppable_kinds: tuple[str, ...] = ()
    grantable_kinds: tuple[str, ...] = ()

    def __init__(self, settings, api: AzureApi | None = None, token=None, operation_timeout: int = 900):
        self.settings = settings
        self.cloud = CLOUDS[settings.cloud]
        self.api = api or AzureApi(settings.subscription_id, token or AzureToken(self.cloud, client_id=settings.identity_client_id, tenant_id=settings.tenant_id),
                                   cloud=self.cloud, operation_timeout=operation_timeout)

    # ---- knowledge
    def regions(self) -> list[str]:
        known = set(ZONES) if self.settings.cloud == "public" else set()
        return sorted(known | set(self.settings.zones))

    def zones(self, region: str) -> list[str]:
        """A region's logical zones ("1", "2", "3"), the first the default; [] for a region without zones, where a
        resource is regional."""
        if region in self.settings.zones:
            return list(self.settings.zones[region])
        return list(ZONES.get(region, [])) if self.settings.cloud == "public" else []

    def validate(self, request: InfrastructureRequest) -> list[Problem]:
        if request.region not in self.regions():
            return [Problem(ErrorCode.INVALID_REGION, f"The Azure adapter does not know the region {request.region}; regions are named as eastus2, and the "
                            "operator lists any other under providers.azure.zones", "region")]
        zones = self.zones(request.region)
        if request.zone and not zones:
            return [Problem(ErrorCode.INVALID_REGION, f"{request.region} has no availability zones; leave zone out", "zone")]
        if request.zone and request.zone not in zones:
            return [Problem(ErrorCode.INVALID_REGION, f"Zone {request.zone} is not one of {request.region}'s: {', '.join(zones)} (Azure's zones are numbered)", "zone")]
        return []

    def plan(self, request: InfrastructureRequest, ctx: ProvisionContext) -> list[PlannedResource]:
        if request.kinds():  # the validator refuses these first; a plan never reaches here with one
            raise InfrastructureError(ErrorCode.UNSUPPORTED_RESOURCE_KIND, "The azure adapter provides no kinds yet", {"provider": self.name, "kinds": request.kinds()})
        return []

    def estimate_cost(self, planned: list[PlannedResource]) -> CostEstimate:
        if planned:
            raise InfrastructureError(ErrorCode.UNSUPPORTED_RESOURCE_KIND, "The azure adapter prices no kinds yet", {"provider": self.name})
        return CostEstimate(monthly_usd=0.0, source="https://prices.azure.com/api/retail/prices", note="Nothing to price.")

    def quota(self, region: str) -> dict[str, dict]:
        """Regional vCPUs and machines under the orchestrator's names, and each family's vCPUs under Azure's; {} when
        they cannot be read (never an error)."""
        try:
            usages = self.api.list(self.api.subscription(f"/providers/Microsoft.Compute/locations/{region}/usages"), API_VERSIONS["Microsoft.Compute"])
        except InfrastructureError:
            return {}
        found = {}
        for usage in usages:
            name = str((usage.get("name") or {}).get("value") or "")
            limit = usage.get("limit")
            entry = {"limit": limit if isinstance(limit, int) and limit >= 0 else None, "usage": usage.get("currentValue", 0)}
            if name in QUOTA_NAMES:
                found[QUOTA_NAMES[name]] = entry
            elif name.endswith("Family"):
                found[name] = entry
        return found


def _not_yet(operation: str):
    def refuse(self, *args, **kwargs):
        raise InfrastructureError(ErrorCode.UNSUPPORTED_RESOURCE_KIND, f"The azure adapter does not provide this yet ({operation})",
                                  {"provider": "azure", "operation": operation})
    refuse.__name__ = operation
    return refuse


# Every primitive of the interface this adapter has not written yet, refused with a structured error.
NOT_YET = tuple(sorted(AzureProvider.__abstractmethods__))
for _operation in NOT_YET:
    setattr(AzureProvider, _operation, _not_yet(_operation))
abc.update_abstractmethods(AzureProvider)
