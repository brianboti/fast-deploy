"""The Google API client: a quota project on every call when the operator names one, and calls Google rate limited tried
again (a grant failed on a laptop on 2026-10-29 because Google counted its calls against gcloud's shared project)."""
import httpx
import pytest

from cloudinfra.config import Config
from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.providers.gcp import api as api_module
from cloudinfra.providers.gcp.api import GcpApi
from cloudinfra.providers.gcp.provider import GCPProvider

URL = "https://cloudresourcemanager.googleapis.com/v1/projects/test-project"


def client(*answers, quota_project=None):
    seen, slept = [], []
    queue = list(answers)

    def handle(request):
        seen.append(request)
        status, headers = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(status, headers=headers, json={"projectNumber": "123"} if status == 200 else {"error": {"message": "slow down", "status": "RESOURCE_EXHAUSTED"}})
    gcp = GcpApi("test-project", lambda: "token", client=httpx.Client(transport=httpx.MockTransport(handle)), sleep=slept.append, quota_project=quota_project)
    return gcp, seen, slept


def test_a_rate_limited_call_is_tried_again_with_growing_pauses():
    gcp, seen, slept = client((429, {}), (429, {}), (200, {}))
    assert gcp.project_number() == "123" and len(seen) == 3 and slept == [1.0, 2.0]


def test_googles_retry_after_is_followed_up_to_a_ceiling():
    gcp, _, slept = client((429, {"Retry-After": "7"}), (429, {"Retry-After": "600"}), (200, {}))
    gcp.get(URL)
    assert slept == [7.0, api_module.MAX_RETRY_PAUSE_SECONDS]


def test_after_the_last_attempt_the_rate_limit_is_reported():
    gcp, seen, slept = client((429, {}))
    with pytest.raises(InfrastructureError) as caught:
        gcp.get(URL)
    assert caught.value.code == ErrorCode.PROVIDER_UNAVAILABLE and caught.value.details["http_status"] == 429
    assert len(seen) == api_module.RETRIES and len(slept) == api_module.RETRIES - 1


def test_a_rate_limited_write_is_tried_again_because_google_refused_it_before_it_ran():
    gcp, seen, _ = client((429, {}), (200, {}))
    gcp.post(URL + ":setIamPolicy", json={"policy": {}})
    assert [request.method for request in seen] == ["POST", "POST"]


def test_a_read_that_met_a_server_error_is_tried_again_but_a_write_is_not():
    gcp, seen, _ = client((503, {}), (200, {}))
    gcp.get(URL)
    assert len(seen) == 2
    gcp, seen, slept = client((503, {}), (200, {}))
    with pytest.raises(InfrastructureError) as caught:
        gcp.post(URL + ":setIamPolicy", json={"policy": {}})
    assert caught.value.code == ErrorCode.PROVIDER_UNAVAILABLE and len(seen) == 1 and slept == [], "a write that may have been done is not repeated"


def test_other_refusals_are_not_retried():
    gcp, seen, _ = client((403, {}))
    with pytest.raises(InfrastructureError):
        gcp.get(URL)
    assert len(seen) == 1


def test_the_quota_project_goes_with_every_call_when_the_operator_names_one():
    gcp, seen, _ = client((200, {}), quota_project="billing-project")
    gcp.get(URL)
    assert seen[0].headers["X-Goog-User-Project"] == "billing-project"
    gcp, seen, _ = client((200, {}))
    gcp.get(URL)
    assert "X-Goog-User-Project" not in seen[0].headers, "a service account needs none, and the header needs a permission"


def test_the_setting_reaches_the_client():
    config = Config.model_validate({"providers": {"gcp": {"project_id": "test-project", "quota_project": "billing-project"}},
                                    "principals": [{"name": "app-layer", "token_sha256": "0" * 64}]})
    provider = GCPProvider(config.providers.gcp, token=lambda: "token")
    assert provider.api.quota_project == "billing-project"
