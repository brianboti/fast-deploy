"""A fake Azure for the unit tests: the Resource Manager endpoints the adapter calls, answered from memory, with planted
failures. It grows with each kind of the adapter (docs/research/RESEARCH-2026-11-03-azure.md, section 10).

No test here touches a cloud. The fake replaces AzureApi.call, so the adapter's addresses, bodies, paging and waits run
as in production, and `calls` records every one for assertions.
"""
import re
from urllib.parse import parse_qsl

from gcp_fakes import config_dict

from cloudinfra.config import Config
from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.orchestration.provisioner import InlineExecutor, Orchestrator
from cloudinfra.providers import ProviderRegistry
from cloudinfra.providers.azure.api import API_VERSIONS, AzureApi, AzureResponse
from cloudinfra.providers.azure.provider import AzureProvider
from cloudinfra.state.state_manager import StateManager

SUBSCRIPTION = "00000000-0000-4000-8000-000000000001"
TENANT = "00000000-0000-4000-8000-0000000000aa"
ARM = "https://management.azure.com"


def azure_settings(**overrides):
    return {"subscription_id": SUBSCRIPTION, "allowed_regions": ["eastus2", "westus"], **overrides}


def make_azure_config(tmp_path=None, gcp=False, azure=None, **overrides):
    """The tests' configuration with an Azure block (and Google's beside it when gcp is true)."""
    providers = {"azure": azure_settings(**(azure or {}))}
    if gcp:
        providers["gcp"] = config_dict()["providers"]["gcp"]
    data = config_dict(providers=providers, **overrides)
    if tmp_path is not None:
        data["database"] = str(tmp_path / "cloudinfra.db")
    return Config.model_validate(data)


def usage(name, current, limit):
    return {"name": {"value": name, "localizedValue": name}, "currentValue": current, "limit": limit, "unit": "Count"}


class FakeAzureApi(AzureApi):
    """Azure's REST surface, in memory. `fail_on` holds (method, url fragment) pairs that raise a provider error once reached."""

    def __init__(self):
        self.now = 0.0  # the fake's own clock: a sleep moves it on, so a wait that never ends runs out instead of hanging a test
        super().__init__(SUBSCRIPTION, token=lambda audience=None: "fake-token", sleep=self._sleep, clock=lambda: self.now)
        self.calls = []  # (method, url, body, query parameters)
        self.fail_on = []
        self.page_size = 2
        self.usages = {"eastus2": [usage("availabilitySets", 0, 2500), usage("cores", 6, 20), usage("virtualMachines", 3, 25000),
                                   usage("standardDSv5Family", 6, 10), usage("standardNCADSA100v4Family", 0, 0), usage("PremiumDiskCount", 1, 1000)]}

    def _sleep(self, seconds):
        self.now += seconds

    def call(self, method, url, json=None, params=None, audience=None):
        path, _, query = url.partition("?")
        params = {**dict(parse_qsl(query)), **dict(params or {})}
        self.calls.append((method, url, json, params))
        for fail_method, fragment in self.fail_on:
            if method == fail_method and fragment in url:
                raise InfrastructureError(ErrorCode.PROVIDER_ERROR, f"planted failure on {method} {fragment}", {"http_status": 400, "planted": True})
        assert path.startswith(ARM), f"the adapter called outside Resource Manager: {url}"
        path = path[len(ARM):]
        handlers = (
            (r"^/subscriptions/([^/]+)/providers/Microsoft\.Compute/locations/([^/]+)/usages$", self._usages),
        )
        for pattern, handler in handlers:
            found = re.match(pattern, path)
            if found:
                return handler(found, method, json or {}, params)
        raise AssertionError(f"the fake has no handler for {method} {path}")

    def _usages(self, found, method, body, params):
        subscription, region = found.groups()
        assert method == "GET" and subscription == SUBSCRIPTION and params["api-version"] == API_VERSIONS["Microsoft.Compute"]
        items = self.usages.get(region, [])
        start = int(params.get("$skiptoken", 0))
        answer = {"value": items[start:start + self.page_size]}
        if start + self.page_size < len(items):
            answer["nextLink"] = (f"{ARM}/subscriptions/{subscription}/providers/Microsoft.Compute/locations/{region}/usages"
                                  f"?api-version={params['api-version']}&$skiptoken={start + self.page_size}")
        return AzureResponse(200, {}, answer)


def make_azure_provider(config, fake=None):
    fake = fake or FakeAzureApi()
    return AzureProvider(config.providers.azure, api=fake), fake


def make_azure_orchestrator(tmp_path, config=None, fake=None):
    """An orchestrator over a fake Azure (and the fake Google Cloud when the configuration has both)."""
    from gcp_fakes import make_provider
    config = config or make_azure_config(tmp_path)
    provider, fake = make_azure_provider(config, fake)
    adapters = {"azure": provider}
    if config.providers.gcp is not None:
        adapters["gcp"], _ = make_provider(config)
    registry = ProviderRegistry(config, adapters)
    return Orchestrator(config, StateManager(config.database), registry, InlineExecutor(), sleep=lambda seconds: None), fake
