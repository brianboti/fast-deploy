"""The Azure adapter's first step (docs/research/RESEARCH-2026-11-03-azure.md, section 10): its clouds, settings, token
helper, REST client, names, regions and quota, and its place in the registry. Its kinds are test_infra_azure_kinds.py's."""
import json
from pathlib import Path

import httpx
import pytest
from azure_fakes import SUBSCRIPTION, TENANT, FakeAzureApi, make_azure_config, make_azure_orchestrator, make_azure_provider
from gcp_fakes import VM_REQUEST, principal

from cloudinfra.config import Config
from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.models.request import InfrastructureRequest
from cloudinfra.orchestration import validator
from cloudinfra.providers import PLANNED_PROVIDERS, ProviderRegistry
from cloudinfra.providers.azure import api as api_module
from cloudinfra.providers.azure import naming
from cloudinfra.providers.azure.api import AzureApi
from cloudinfra.providers.azure.auth import AzureToken
from cloudinfra.providers.azure.clouds import AKS_SERVER_APPLICATION, CLOUDS
from cloudinfra.providers.azure.provider import NOT_YET, AzureProvider
from cloudinfra.providers.azure.settings import AzureSettings

FIXTURES = Path(__file__).parent / "fixtures"
PUBLIC = CLOUDS["public"]
ARM_AUDIENCE = "https://management.azure.com/"
VAULT = "https://vault.azure.net"
GROUP_URL = f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/resourceGroups/platform-1a2b3c4d"
OPERATION = f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Resources/operations/op-1?api-version=2021-04-01"
AZURE_REQUEST = {"provider": "azure", "region": "eastus2", "network": {}, "compute": {"cpu": 2, "memory_gb": 8}}
NOT_YET_REQUEST = {"provider": "azure", "region": "eastus2", "database": {}}


# ---- the clouds

@pytest.mark.parametrize("name", ["public", "usgovernment", "china"])
def test_each_cloud_is_what_its_own_metadata_document_says(name):
    """The documents were read on 2026-11-03 from each cloud's Resource Manager; a refreshed one that moves a value fails here."""
    document = json.loads((FIXTURES / f"azure-cloud-{name}.json").read_text())
    cloud = CLOUDS[name]
    assert (document["name"], document["resourceManager"].rstrip("/"), document["authentication"]["loginEndpoint"].rstrip("/")) == \
        (cloud.name, cloud.resource_manager, cloud.login)
    assert cloud.arm_audience in document["authentication"]["audiences"]
    assert (document["microsoftGraphResourceId"], document["ossrDbmsResourceId"]) == (cloud.graph, cloud.oss_rdbms)
    suffixes = document["suffixes"]
    assert (suffixes["keyVaultDns"], suffixes["storage"], suffixes["acrLoginServer"], suffixes["postgresqlServerEndpoint"], suffixes["mysqlServerEndpoint"]) == \
        (cloud.key_vault_suffix, cloud.storage_suffix, cloud.registry_suffix, cloud.postgresql_suffix, cloud.mysql_suffix)


def test_a_sovereign_cloud_names_no_audience_no_page_gives():
    """The metrics plane, Storage and Service Bus audiences are documented for the public cloud only: None elsewhere, so
    no token is ever asked for a guessed address."""
    assert (PUBLIC.metrics, PUBLIC.storage, PUBLIC.service_bus) == ("https://metrics.monitor.azure.com", "https://storage.azure.com/", "https://servicebus.azure.net")
    for name in ("usgovernment", "china"):
        assert (CLOUDS[name].metrics, CLOUDS[name].storage, CLOUDS[name].service_bus) == (None, None, None)
    assert (PUBLIC.key_vault, CLOUDS["china"].key_vault) == ("https://vault.azure.net", "https://vault.azure.cn") and PUBLIC.host == "management.azure.com"


# ---- the settings

def test_settings_hold_no_credential_and_normalise_ids():
    settings = AzureSettings(subscription_id=SUBSCRIPTION.upper(), tenant_id=TENANT.upper())
    assert (settings.subscription_id, settings.tenant_id, settings.cloud, settings.allowed_regions) == (SUBSCRIPTION, TENANT, "public", ["eastus2"])
    assert settings.described() == {"subscription": SUBSCRIPTION, "cloud": "public"}
    for bad in ({"subscription_id": "my-subscription"}, {"subscription_id": SUBSCRIPTION, "allowed_regions": ["East US 2"]},
                {"subscription_id": SUBSCRIPTION, "zones": {"eastus2": ["a"]}}, {"subscription_id": SUBSCRIPTION, "zones": {"eastus2": ["1", "1"]}},
                {"subscription_id": SUBSCRIPTION, "client_secret": "x"}, {"subscription_id": SUBSCRIPTION, "cloud": "azurestack"}):
        with pytest.raises(ValueError):
            AzureSettings.model_validate(bad)


def test_a_configuration_may_name_azure_alone_but_not_no_provider():
    assert make_azure_config().configured_providers() == ["azure"]
    assert make_azure_config(gcp=True).configured_providers() == ["gcp", "azure"]
    with pytest.raises(ValueError, match=r"configure at least one provider \(gcp, azure\)"):
        Config.model_validate({"providers": {}, "principals": [{"name": "app-layer", "token_sha256": "0" * 64}]})


# ---- the token helper

def token_helper(handler, environ=None, runner=None, client_id=None, tenant_id=None, read=None):
    seen, slept, now = [], [], [1_000_000.0]

    def handle(request):
        seen.append(request)
        return handler(request)

    def refuse_cli(args):
        raise AssertionError(f"the Azure CLI was asked: {args}")
    token = AzureToken(PUBLIC, client=httpx.Client(transport=httpx.MockTransport(handle)), runner=runner or refuse_cli, clock=lambda: now[0], sleep=slept.append,
                       environ=environ or {}, client_id=client_id, tenant_id=tenant_id, read=read)
    return token, seen, slept, now


def metadata_answer(request, now=1_000_000.0):
    assert request.url.host == "169.254.169.254" and request.headers["Metadata"] == "true"
    assert request.url.params["api-version"] == "2018-02-01"
    return httpx.Response(200, json={"access_token": f"token-for-{request.url.params['resource']}", "expires_on": str(int(now) + 3600), "token_type": "Bearer"})


def test_on_a_virtual_machine_the_metadata_service_gives_one_token_per_audience_kept_until_a_minute_before_it_ends():
    token, seen, _, now = token_helper(metadata_answer)
    assert token() == f"token-for-{ARM_AUDIENCE}" and token() == f"token-for-{ARM_AUDIENCE}" and len(seen) == 1
    assert token(VAULT) == f"token-for-{VAULT}" and len(seen) == 2, "a key vault's token is asked for apart: Azure's tokens have one audience"
    now[0] += 3600 - 61
    token()
    assert len(seen) == 2
    now[0] += 2
    token()
    assert len(seen) == 3, "a minute before it ends, a token is asked for again"
    token.invalidate()
    token()
    assert len(seen) == 4 and token.source == "metadata service"


def test_a_user_assigned_identity_is_named_by_its_client_id():
    token, seen, _, _ = token_helper(metadata_answer, client_id="11111111-2222-4333-8444-555555555555")
    token()
    assert seen[0].url.params["client_id"] == "11111111-2222-4333-8444-555555555555"


def test_on_a_laptop_the_azure_cli_gives_the_token_and_the_metadata_service_is_tried_once():
    asked = []

    def unreachable(request):
        raise httpx.ConnectError("no route", request=request)

    def cli(args):
        asked.append(args)
        return json.dumps({"accessToken": f"cli-{args[4]}", "expires_on": 1_000_000 + 1800, "subscription": SUBSCRIPTION, "tokenType": "Bearer"})
    token, seen, _, _ = token_helper(unreachable, runner=cli, tenant_id=TENANT)
    assert token() == f"cli-{ARM_AUDIENCE}" and token(AKS_SERVER_APPLICATION) == f"cli-{AKS_SERVER_APPLICATION}"
    assert asked[0] == ["az", "account", "get-access-token", "--resource", ARM_AUDIENCE, "--output", "json", "--tenant", TENANT]
    assert len(seen) == 1 and token.source == "Azure CLI", "the metadata address is probed once, then the answer is remembered"


def test_another_clouds_metadata_server_is_not_taken_for_azures():
    """Google's metadata server answers at the same address; its HTML 404 is not Azure's JSON, so the CLI is used."""
    token, _, _, _ = token_helper(lambda request: httpx.Response(404, text="<html>Not Found</html>"),
                                  runner=lambda args: json.dumps({"accessToken": "cli", "expires_on": 1_000_900}))
    assert token() == "cli" and token.source == "Azure CLI"


def test_an_azure_machine_without_an_identity_is_told_so_and_the_cli_is_not_tried():
    token, _, _, _ = token_helper(lambda request: httpx.Response(400, json={"error": "invalid_request", "error_description": "Identity not found"}))
    with pytest.raises(ValueError, match="managed identity"):
        token()
    assert token.source == "metadata service"


def test_the_metadata_service_is_tried_again_while_it_is_being_updated():
    answers = [httpx.Response(404, json={"error": "not_found", "error_description": "IMDS is being updated"})] * 2

    def handle(request):
        return answers.pop(0) if answers else metadata_answer(request)
    token, seen, slept, _ = token_helper(handle)
    assert token() == f"token-for-{ARM_AUDIENCE}" and slept == [2] and len(seen) == 3, "the probe, a 404, then the token"


def test_on_container_apps_the_local_identity_endpoint_is_asked_with_its_header():
    def handle(request):
        assert str(request.url).startswith("http://localhost:42356/msi/token") and request.headers["X-IDENTITY-HEADER"] == "rotating-header"
        assert request.url.params["api-version"] == "2019-08-01" and request.url.params["resource"] == ARM_AUDIENCE
        return httpx.Response(200, json={"access_token": "from-the-endpoint", "expires_on": "1003600", "resource": ARM_AUDIENCE})
    token, seen, _, _ = token_helper(handle, environ={"IDENTITY_ENDPOINT": "http://localhost:42356/msi/token", "IDENTITY_HEADER": "rotating-header"})
    assert token() == "from-the-endpoint" and len(seen) == 1 and token.source == "identity endpoint"


def test_on_aks_the_pods_federated_token_is_exchanged_and_read_again_each_time():
    assertions = iter(["assertion-1", "assertion-2"])
    forms = []

    def handle(request):
        assert str(request.url) == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
        forms.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": f"exchanged-{len(forms)}", "expires_in": 3599, "token_type": "Bearer"})
    environ = {"AZURE_FEDERATED_TOKEN_FILE": "/var/run/secrets/azure/tokens/azure-identity-token", "AZURE_CLIENT_ID": "client-1", "AZURE_TENANT_ID": TENANT}
    token, _, _, _ = token_helper(handle, environ=environ, read=lambda path: next(assertions))
    assert token() == "exchanged-1" and token(AKS_SERVER_APPLICATION) == "exchanged-2"
    assert forms[0] == {"grant_type": "client_credentials", "client_id": "client-1", "scope": "https://management.azure.com/.default",
                        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer", "client_assertion": "assertion-1"}
    assert forms[1]["scope"] == f"{AKS_SERVER_APPLICATION}/.default" and forms[1]["client_assertion"] == "assertion-2"


def test_a_refused_exchange_names_entras_code_and_never_the_assertion():
    def handle(request):
        return httpx.Response(400, json={"error": "invalid_client", "error_codes": [70021], "error_description": "No matching federated identity record found"})
    environ = {"AZURE_FEDERATED_TOKEN_FILE": "/token", "AZURE_CLIENT_ID": "client-1", "AZURE_TENANT_ID": TENANT}
    token, _, _, _ = token_helper(handle, environ=environ, read=lambda path: "secret-assertion")
    with pytest.raises(ValueError) as caught:
        token()
    assert "AADSTS70021" in str(caught.value) and "secret-assertion" not in str(caught.value)


def test_a_host_with_no_identity_and_no_sign_in_gets_a_sentence():
    def broken_cli(args):
        raise OSError("az: not found")
    token, _, _, _ = token_helper(lambda request: httpx.Response(404, text=""), runner=broken_cli)
    with pytest.raises(ValueError, match="az login"):
        token()


# ---- the client

def client(*answers, token=None, timeout=600):
    """An AzureApi over a script of answers: (status, headers, body) or an exception to raise."""
    seen, slept, now = [], [], [0.0]
    queue = list(answers)

    def handle(request):
        seen.append(request)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, headers, body = answer
        return httpx.Response(status, headers=headers, json=body) if body is not None else httpx.Response(status, headers=headers)

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds
    api = AzureApi(SUBSCRIPTION, token or (lambda audience=None: f"token:{audience}"), client=httpx.Client(transport=httpx.MockTransport(handle)), sleep=sleep,
                   clock=lambda: now[0], operation_timeout=timeout)
    return api, seen, slept


def refusal(code, message="refused", **extra):
    return {"error": {"code": code, "message": message, **extra}}


def test_a_put_is_followed_through_its_async_operation_and_the_resource_read_back():
    api, seen, slept = client((201, {"Azure-AsyncOperation": OPERATION, "Retry-After": "5"}, {"properties": {"provisioningState": "Creating"}}),
                              (200, {}, {"status": "InProgress"}), (200, {}, {"status": "Succeeded"}),
                              (200, {}, {"name": "platform-1a2b3c4d", "properties": {"provisioningState": "Succeeded"}}))
    found = api.put(GROUP_URL, {"location": "eastus2", "tags": {"managed-by": "cloudinfra"}}, "2021-04-01")
    assert found["name"] == "platform-1a2b3c4d"
    assert [(request.method, str(request.url)) for request in seen] == [("PUT", GROUP_URL + "?api-version=2021-04-01"), ("GET", OPERATION), ("GET", OPERATION),
                                                                       ("GET", GROUP_URL + "?api-version=2021-04-01")]
    assert json.loads(seen[0].content) == {"location": "eastus2", "tags": {"managed-by": "cloudinfra"}} and slept == [5, 10]
    assert all(request.headers["Authorization"] == "Bearer token:None" for request in seen)


def test_without_an_async_operation_the_location_is_followed():
    location = OPERATION.replace("operations", "operationResults")
    api, seen, _ = client((202, {"Location": location}, None), (202, {}, None), (200, {}, None), (200, {}, {"name": "x", "properties": {"provisioningState": "Succeeded"}}))
    assert api.put(GROUP_URL, {}, "2021-04-01")["name"] == "x" and [str(request.url) for request in seen[1:3]] == [location, location]


def test_a_resource_that_says_it_is_still_being_made_is_read_until_it_is_done():
    api, seen, _ = client((200, {}, {"properties": {"provisioningState": "Creating"}}), (200, {}, {"properties": {"provisioningState": "Updating"}}),
                          (200, {}, {"name": "x", "properties": {"provisioningState": "Succeeded"}}))
    assert api.put(GROUP_URL, {}, "2021-04-01")["name"] == "x" and len(seen) == 3


def test_a_failed_operation_is_an_error_with_azures_code_kept():
    api, _, _ = client((201, {"Azure-AsyncOperation": OPERATION}, None),
                       (200, {}, {"status": "Failed", "error": refusal("QuotaExceeded", "exceeding approved standardDSv5Family Cores quota")["error"]}))
    with pytest.raises(InfrastructureError) as caught:
        api.put(GROUP_URL, {}, "2021-04-01")
    assert caught.value.code == ErrorCode.QUOTA_EXCEEDED and caught.value.details["reason"] == "QuotaExceeded"


def test_a_resource_that_ended_failed_is_an_error_whatever_the_status():
    api, _, _ = client((200, {}, {"properties": {"provisioningState": "Failed"}}))
    with pytest.raises(InfrastructureError) as caught:
        api.put(GROUP_URL, {}, "2021-04-01")
    assert caught.value.code == ErrorCode.PROVIDER_ERROR and caught.value.details["provisioning_state"] == "failed"


def test_an_operation_that_never_ends_runs_out_of_time():
    api, _, _ = client((201, {"Azure-AsyncOperation": OPERATION}, None), *[(200, {}, {"status": "InProgress"})] * 10, timeout=30)
    with pytest.raises(InfrastructureError) as caught:
        api.put(GROUP_URL, {}, "2021-04-01")
    assert caught.value.code == ErrorCode.OPERATION_TIMEOUT


def test_an_address_on_another_host_is_not_followed_and_gets_no_token():
    api, seen, _ = client((201, {"Azure-AsyncOperation": "https://attacker.example/operations/1"}, None))
    with pytest.raises(InfrastructureError) as caught:
        api.put(GROUP_URL, {}, "2021-04-01")
    assert caught.value.code == ErrorCode.PROVIDER_ERROR and len(seen) == 1
    api, seen, _ = client((200, {}, {"value": [{"id": 1}], "nextLink": "http://management.azure.com/next"}))
    with pytest.raises(InfrastructureError):
        api.list(GROUP_URL, "2021-04-01")
    assert len(seen) == 1, "nor a page over plain http"


def test_a_delete_of_something_already_gone_is_not_an_error_and_a_long_one_is_waited_on():
    api, seen, _ = client((204, {}, None))
    assert api.delete(GROUP_URL, "2021-04-01") is None and len(seen) == 1
    api, _, _ = client((404, {}, refusal("ResourceGroupNotFound")))
    assert api.delete(GROUP_URL, "2021-04-01") is None
    api, seen, _ = client((202, {"Location": OPERATION}, None), (202, {}, None), (200, {}, None))
    assert api.delete(GROUP_URL, "2021-04-01") is None and len(seen) == 3


def test_an_action_returns_its_result():
    api, _, _ = client((202, {"Azure-AsyncOperation": OPERATION, "Location": OPERATION + "&result=1"}, None), (200, {}, {"status": "Succeeded"}),
                       (200, {}, {"keys": ["a"]}))
    assert api.post(GROUP_URL + "/listKeys", "2021-04-01") == {"keys": ["a"]}


def test_a_refused_call_waits_as_long_as_azure_asks_for_any_method():
    api, seen, slept = client((429, {"Retry-After": "7"}, refusal("TooManyRequests")), (200, {}, {"done": True}))
    assert api.post(GROUP_URL + "/start", "2026-04-01") == {"done": True} and slept == [7.0] and len(seen) == 2


def test_a_wait_longer_than_a_worker_should_sleep_is_reported_not_slept():
    """Compute may ask for 1,200 seconds; a call sent before then is not processed, so the service says when to retry."""
    api, seen, slept = client((429, {"Retry-After": "1200"}, refusal("OperationNotAllowed", "too many requests")))
    with pytest.raises(InfrastructureError) as caught:
        api.get(GROUP_URL, "2021-04-01")
    assert caught.value.code == ErrorCode.PROVIDER_UNAVAILABLE and caught.value.details["retry_after_seconds"] == 1200 and len(seen) == 1 and slept == []


def test_another_operation_holding_the_resource_is_waited_out():
    api, seen, slept = client((409, {}, refusal("AnotherOperationInProgress")), (429, {}, refusal("RetryableErrorDueToAnotherOperation")), (200, {}, {}))
    api.post(GROUP_URL + "/deallocate", "2026-04-01")
    assert len(seen) == 3 and slept == [api_module.BUSY_PAUSE_SECONDS, api_module.BUSY_PAUSE_SECONDS]


def test_a_server_error_is_tried_again_only_where_repeating_is_harmless():
    api, seen, _ = client((503, {}, None), (200, {}, {"name": "x"}))
    assert api.get(GROUP_URL, "2021-04-01") == {"name": "x"} and len(seen) == 2
    api, seen, slept = client((500, {}, None), (200, {}, None))
    with pytest.raises(InfrastructureError) as caught:
        api.post(GROUP_URL + "/start", "2026-04-01")
    assert caught.value.code == ErrorCode.PROVIDER_UNAVAILABLE and len(seen) == 1 and slept == []


def test_a_lost_answer_repeats_an_idempotent_put_but_reports_an_action_as_maybe_done():
    api, seen, _ = client(httpx.ReadTimeout("lost"), (200, {}, {"name": "x", "properties": {"provisioningState": "Succeeded"}}))
    assert api.put(GROUP_URL, {}, "2021-04-01")["name"] == "x" and [request.method for request in seen] == ["PUT", "PUT"]
    api, seen, _ = client(httpx.ReadTimeout("lost"))
    with pytest.raises(InfrastructureError) as caught:
        api.post(GROUP_URL + "/deallocate", "2026-04-01")
    assert caught.value.details["maybe_done"] is True and len(seen) == 1


def test_a_refused_token_is_asked_for_again_once_for_its_own_audience():
    class Token:
        def __init__(self):
            self.asked, self.forgotten = [], []

        def __call__(self, audience=None):
            self.asked.append(audience)
            return f"token-{len(self.asked)}"

        def invalidate(self, audience=None):
            self.forgotten.append(audience)
    token = Token()
    api, seen, _ = client((401, {}, refusal("ExpiredAuthenticationToken")), (200, {}, {"value": "x"}), token=token)
    assert api.get(VAULT + "/secrets/a", "7.5", audience=VAULT) == {"value": "x"}
    assert token.asked == [VAULT, VAULT] and token.forgotten == [VAULT] and seen[1].headers["Authorization"] == "Bearer token-2"
    api, _, _ = client((401, {}, refusal("InvalidAuthenticationToken")), (401, {}, refusal("InvalidAuthenticationToken")), token=Token())
    with pytest.raises(InfrastructureError) as caught:
        api.get(GROUP_URL, "2021-04-01")
    assert caught.value.code == ErrorCode.PROVIDER_AUTH_FAILED


def test_no_credential_on_the_host_is_an_auth_failure():
    def no_token(audience=None):
        raise ValueError("Could not get an Azure token on this host")
    api, seen, _ = client((200, {}, {}), token=no_token)
    with pytest.raises(InfrastructureError) as caught:
        api.get(GROUP_URL, "2021-04-01")
    assert caught.value.code == ErrorCode.PROVIDER_AUTH_FAILED and seen == []


def test_a_policy_denial_names_the_policy():
    body = refusal("RequestDisallowedByPolicy", "Resource 'vm-1' was disallowed by policy.", target="vm-1",
                   additionalInfo=[{"type": "PolicyViolation", "info": {"policyAssignmentId": "/providers/Microsoft.Management/managementGroups/corp/providers/"
                                    "Microsoft.Authorization/policyAssignments/deny-public-ip", "policyDefinitionId": "/providers/Microsoft.Authorization/"
                                    "policyDefinitions/83a86a26-fd1f-447c-b59d-e51f44264114"}}])
    api, _, _ = client((403, {}, body))
    with pytest.raises(InfrastructureError) as caught:
        api.put(GROUP_URL, {}, "2021-04-01")
    assert caught.value.code == ErrorCode.PROVIDER_PERMISSION_DENIED and "Azure Policy" in caught.value.message
    assert caught.value.details["policyDefinitionId"].endswith("83a86a26-fd1f-447c-b59d-e51f44264114") and caught.value.details["target"] == "vm-1"


@pytest.mark.parametrize(("status", "reason", "expected"), [
    (403, "AuthorizationFailed", ErrorCode.PROVIDER_PERMISSION_DENIED),
    (400, "MarketplacePurchaseEligibilityFailed", ErrorCode.PROVIDER_PERMISSION_DENIED),
    (400, "RoleAssignmentLimitExceeded", ErrorCode.QUOTA_EXCEEDED),
    (409, "OperationNotAllowed", ErrorCode.PROVIDER_CONFLICT),
    (400, "SkuNotAvailable", ErrorCode.PROVIDER_UNAVAILABLE),
    (409, "ZonalAllocationFailed", ErrorCode.PROVIDER_UNAVAILABLE),
    (404, "ResourceGroupNotFound", ErrorCode.NOT_FOUND),
    (409, "StorageAccountAlreadyTaken", ErrorCode.PROVIDER_CONFLICT),
    (409, "ScopeLocked", ErrorCode.PROVIDER_CONFLICT),
    (400, "InvalidParameter", ErrorCode.PROVIDER_ERROR),
])
def test_azures_error_codes_become_the_services(status, reason, expected):
    api, _, _ = client((status, {}, refusal(reason, "what Azure said")))
    with pytest.raises(InfrastructureError) as caught:
        api.put(GROUP_URL, {}, "2021-04-01")
    assert caught.value.code == expected and caught.value.details["reason"] == reason and caught.value.details["provider_message"] == "what Azure said"


def test_a_list_follows_its_pages_and_a_find_answers_none_for_nothing():
    api, seen, _ = client((200, {}, {"value": [1, 2], "nextLink": GROUP_URL + "?api-version=2021-04-01&$skiptoken=2"}), (200, {}, {"value": [3]}))
    assert api.list(GROUP_URL, "2021-04-01") == [1, 2, 3] and str(seen[1].url).endswith("$skiptoken=2")
    api, _, _ = client((404, {}, refusal("ResourceNotFound")))
    assert api.find(GROUP_URL, "2021-04-01") is None


# ---- names

PREFIXES = ("a", "web", "platform", "data-team", "a1-2", "x--y", "infra-0123abcd", "abcdefghijklmnopqrst", "a-b-c-d-e-f-g-h-i-j")
REQUEST_IDS = ("0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210", "deadbeef" * 4)


@pytest.mark.parametrize("kind", naming.GLOBAL)
def test_every_global_name_keeps_its_kinds_rules_for_every_request_name(kind):
    for prefix in PREFIXES:
        for request_id in REQUEST_IDS:
            for part in ("", "db", "cache"):
                name = naming.global_name(kind, prefix, request_id, part)
                assert naming.problem(kind, name) is None, (kind, prefix, part, name)
                assert name.endswith(request_id[:8]) and name == naming.global_name(kind, prefix, request_id, part), "pure: the plan makes it again"
    assert naming.global_name(kind, "web", REQUEST_IDS[0]) != naming.global_name(kind, "web", REQUEST_IDS[1]), "a request made again gets a new name"


def test_names_read_as_the_request_and_fit_azures_rules():
    request_id = REQUEST_IDS[0]
    assert naming.global_name("storage_account", "data-team", request_id) == "datateam01234567"
    assert naming.global_name("key_vault", "data-team", request_id) == "data-team-01234567"
    assert naming.global_name("flexible_server", "data-team", request_id, "db") == "data-team-db-01234567"
    assert len(naming.global_name("key_vault", "abcdefghijklmnopqrst", request_id)) == 24
    assert naming.resource_group("platform", request_id) == "platform-01234567" and naming.problem("resource_group", "platform-01234567") is None
    assert "3 to 24" in naming.problem("storage_account", "ab") and naming.problem("key_vault", "a--b") is not None
    assert naming.problem("virtual_machine", "web-1") is None and naming.problem("virtual_machine", "web-") is not None
    with pytest.raises(ValueError):
        naming.global_name("virtual_machine", "web", request_id)


# ---- the provider

def test_the_registry_builds_the_adapter_with_the_operators_timeout():
    config = make_azure_config(gcp=True, limits={"operation_timeout_seconds": 1200})
    registry = ProviderRegistry(config)
    adapter = registry.get("azure")
    assert isinstance(adapter, AzureProvider) and adapter.api.operation_timeout == 1200 and adapter.cloud is PUBLIC
    assert registry.names() == ["azure", "gcp"] and "azure" not in PLANNED_PROVIDERS


def test_a_kind_it_does_not_provide_yet_is_refused_before_any_call_naming_those_it_does():
    config = make_azure_config()
    provider, fake = make_azure_provider(config)
    problems = validator.validate(InfrastructureRequest.model_validate(NOT_YET_REQUEST), config, provider)
    assert [(problem.code, problem.message) for problem in problems if problem.code == ErrorCode.UNSUPPORTED_RESOURCE_KIND] == \
        [(ErrorCode.UNSUPPORTED_RESOURCE_KIND, "The azure adapter does not provide database; it provides identity, network, storage, compute")]
    assert validator.validate(InfrastructureRequest.model_validate(AZURE_REQUEST), config, provider) == [] and fake.calls == []


def test_every_primitive_not_written_yet_is_a_structured_refusal():
    provider, fake = make_azure_provider(make_azure_config())
    assert {"create_database", "delete_cache", "get_cluster", "create_load_balancer"} <= set(NOT_YET) and not {"create_compute", "resource_health"} & set(NOT_YET)
    for operation in ("create_database", "get_queue", "delete_cluster"):
        with pytest.raises(InfrastructureError) as caught:
            getattr(provider, operation)(None, None, None, None)
        assert caught.value.code == ErrorCode.UNSUPPORTED_RESOURCE_KIND and caught.value.details == {"provider": "azure", "operation": operation}
    assert provider.estimate_cost([]).monthly_usd == 0.0 and fake.calls == []


def test_regions_and_their_numbered_zones():
    provider, _ = make_azure_provider(make_azure_config(azure={"zones": {"taiwannorth": ["1", "2", "3"], "eastus2": ["1", "2", "3", "4"]}}))
    assert provider.zones("westeurope") == ["1", "2", "3"] and provider.zones("westus") == [] and provider.zones("eastus2") == ["1", "2", "3", "4"]
    assert "taiwannorth" in provider.regions() and "francesouth" not in provider.regions(), "a restricted region is the operator's to add"

    def problems(**request):
        return provider.validate(InfrastructureRequest.model_validate({**AZURE_REQUEST, **request}))
    assert problems() == [] and problems(zone="2") == [] and problems(region="westus") == []
    assert [problem.field for problem in problems(region="us-central1")] == ["region"]
    assert "has no availability zones" in problems(region="westus", zone="1")[0].message
    assert "not one of eastus2's" in problems(zone="5")[0].message


def test_a_sovereign_clouds_regions_are_the_operators():
    provider, _ = make_azure_provider(make_azure_config(azure={"cloud": "usgovernment", "allowed_regions": ["usgovvirginia"], "zones": {"usgovvirginia": ["1", "2", "3"]}}))
    assert provider.regions() == ["usgovvirginia"] and provider.zones("eastus2") == [] and provider.cloud is CLOUDS["usgovernment"]


def test_quota_reads_regional_and_family_vcpus_through_every_page():
    provider, fake = make_azure_provider(make_azure_config())
    quota = provider.quota("eastus2")
    assert quota == {"CPUS": {"limit": 20, "usage": 6}, "INSTANCES": {"limit": 25000, "usage": 3}, "standardDSv5Family": {"limit": 50, "usage": 6},
                     "standardNCADSA100v4Family": {"limit": 0, "usage": 0}, "IN_USE_ADDRESSES": {"limit": 1000, "usage": 2}}, \
        "a GPU family starts at zero in a new subscription"
    assert len(fake.calls) == 4 and fake.calls[0][1].endswith(f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Compute/locations/eastus2/usages")
    assert fake.calls[3][1].endswith(f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Network/locations/eastus2/usages")
    fake.fail_on.append(("GET", "Microsoft.Network/locations"))
    assert "IN_USE_ADDRESSES" not in provider.quota("eastus2") and provider.quota("eastus2")["CPUS"], "the addresses' usages are a part, not all"
    fake.fail_on.append(("GET", "/usages"))
    assert provider.quota("eastus2") == {}, "best effort: never an error"


def test_the_service_names_the_subscription_and_refuses_what_azure_does_not_provide_without_raising(tmp_path):
    orchestrator, fake = make_azure_orchestrator(tmp_path, config=make_azure_config(tmp_path, gcp=True))
    status = orchestrator.service_status("1.38.0")
    assert status["providers"]["azure"] == {"subscription": SUBSCRIPTION, "cloud": "public", "allowed_regions": ["eastus2", "westus"], "planned": False}
    assert status["providers"]["gcp"]["project"] == "test-project" and status["planned_providers"] == ["aws", "jetstream"]
    who = principal(orchestrator.config)
    result = orchestrator.validate(InfrastructureRequest.model_validate(NOT_YET_REQUEST), who)
    assert not result.valid and result.problems[0].code == ErrorCode.UNSUPPORTED_RESOURCE_KIND and fake.calls == []
    assert orchestrator.validate(InfrastructureRequest.model_validate(AZURE_REQUEST), who).valid and fake.calls == []
    assert orchestrator.validate(InfrastructureRequest.model_validate(VM_REQUEST), who).valid, "the Google adapter beside it is untouched"
    kinds = orchestrator.resource_kinds()
    assert kinds.providers == ["azure", "gcp"] and [kind.kind for kind in kinds.kinds if "azure" in kind.providers] == ["identity", "network", "storage", "compute"]


def test_the_fake_is_the_client_with_its_seam_replaced():
    assert isinstance(FakeAzureApi(), AzureApi)


def test_the_contract_takes_azures_region_and_zone_names_and_still_refuses_others():
    request = InfrastructureRequest.model_validate({**AZURE_REQUEST, "region": "westeurope", "zone": "2"})
    assert (request.region, request.zone) == ("westeurope", "2")
    assert InfrastructureRequest.model_validate(VM_REQUEST).region == "us-central1", "Google's names are untouched"
    for region in ("East US 2", "eastus-", "e", "eastus2;", "../eastus2"):
        with pytest.raises(ValueError):
            InfrastructureRequest.model_validate({**AZURE_REQUEST, "region": region})
    for zone in ("0", "10", "1a!", ""):
        with pytest.raises(ValueError):
            InfrastructureRequest.model_validate({**AZURE_REQUEST, "zone": zone})
