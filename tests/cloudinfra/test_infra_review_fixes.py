"""The code review of 2026-10-31 over releases S1 and S2 of the security assessment, infrastructure side: a create whose
answer was lost is looked for again and waited on, cluster nodes are behind the public switch, a resize is priced under
its request's lock, the node-role check reads custom roles, and credentials are found in more shapes."""
import re

import httpx
import pytest
from test_infra_release_a import orchestrated, provision

from cloudinfra.models.errors import ErrorCode, InfrastructureError
from cloudinfra.models.request import InfrastructureRequest
from cloudinfra.providers.gcp.api import GcpApi, settled

INSTANCES = "https://compute.googleapis.com/compute/v1/projects/p/zones/z/instances"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def lost_then(answers):
    """A Google whose create times out after it was taken, then answers each GET from `answers` in turn."""
    gets = []

    def handle(request):
        if request.method == "POST":
            raise httpx.ReadTimeout("no answer", request=request)
        gets.append(request.url.path)
        status, body = answers[min(len(gets), len(answers)) - 1]
        return httpx.Response(status, json=body)
    clock = Clock()
    api = GcpApi("p", token=lambda: "t", client=httpx.Client(transport=httpx.MockTransport(handle)), sleep=clock.sleep, clock=clock, operation_timeout=60)
    return api, gets, clock


MISSING = (404, {"error": {"message": "not found"}})


# ---- 4: Google may show a create a moment after it took it
def test_a_create_that_shows_up_late_is_still_found():
    api, gets, _ = lost_then([MISSING, MISSING, (200, {"name": "vm", "status": "RUNNING"})])
    assert api.create(INSTANCES, f"{INSTANCES}/vm", json={"name": "vm"})["found_after"] == ErrorCode.PROVIDER_UNAVAILABLE.value
    assert len(gets) == 3


def test_a_create_never_shown_keeps_the_first_error_after_a_few_looks():
    api, gets, _ = lost_then([MISSING])
    with pytest.raises(InfrastructureError) as caught:
        api.create(INSTANCES, f"{INSTANCES}/vm", json={"name": "vm"})
    assert caught.value.code == ErrorCode.PROVIDER_UNAVAILABLE and len(gets) == 4


# ---- 1: a found create is waited on until it has settled, as its operation would have been
def test_a_found_create_is_read_until_it_has_settled():
    api, gets, clock = lost_then([(200, {"status": "PROVISIONING"}), (200, {"status": "STAGING"}), (200, {"status": "RUNNING"})])
    operation = api.create(INSTANCES, f"{INSTANCES}/vm", json={"name": "vm"}, ready=settled("status", ("PROVISIONING", "STAGING")))
    assert operation["status"] == "DONE" and len(gets) == 3 and clock.now > 0


def test_a_found_create_that_never_settles_is_still_returned_so_it_is_recorded_and_cleaned_up():
    api, gets, clock = lost_then([(200, {"status": "PROVISIONING"})])
    assert api.create(INSTANCES, f"{INSTANCES}/vm", json={"name": "vm"}, ready=settled("status", ("PROVISIONING",)))["done"] is True
    assert clock.now > 60, "it gave up at the operation timeout"


MOVING = [({"name": "lost", "cluster": {}, "network": {}}, "/clusters", r"container\.googleapis\.com/.*/clusters/[^/:]+$", "status", "PROVISIONING"),
          ({"name": "lost", "database": {}, "network": {}}, "/instances", r"sqladmin\.googleapis\.com/.*/instances/[^/]+$", "state", "PENDING_CREATE"),
          ({"name": "lost", "compute": {"cpu": 2, "memory_gb": 4}}, "/instances", r"compute\.googleapis\.com/.*/instances/[^/]+$", "status", "PROVISIONING")]


@pytest.mark.parametrize(("body", "suffix", "path", "field", "moving"), MOVING, ids=["cluster", "database", "compute"])
def test_an_adapter_waits_on_a_found_resource_before_its_next_step(tmp_path, body, suffix, path, field, moving):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    fake.lose_answer.append(("POST", suffix))
    original, seen = fake.request, []

    def request(method, url, json=None, params=None):
        answer = original(method, url, json=json, params=params)
        if method == "GET" and re.search(path, url) and seen.count("moving") < 2 and not fake.lose_answer:
            seen.append("moving")
            return {**answer, field: moving, "endpoint": None} if field == "status" and "clusters" in url else {**answer, field: moving}
        seen.append(f"{method} {url}")
        return answer
    fake.request = request
    response = orchestrator.provision(InfrastructureRequest.model_validate({"region": "us-central1", **body}), who, wait_seconds=45)
    assert response.error is None, response.error
    assert seen.count("moving") == 2, "read again while it was still being made"
    after = seen[seen.index("moving") + 1:]
    assert after[0] == "moving", "nothing else was asked of it before it settled"


def test_a_subnet_asked_of_a_found_network_still_being_made_is_asked_again(tmp_path):
    orchestrator, fake, _, who = orchestrated(tmp_path)
    fake.lose_answer.append(("POST", "/global/networks"))
    original, subnets = fake.request, []

    def request(method, url, json=None, params=None):
        if method == "POST" and url.endswith("/subnetworks"):
            subnets.append(url)
            if len(subnets) == 1:
                raise InfrastructureError(ErrorCode.INVALID_REQUEST, "The resource is not ready", {"http_status": 400, "reason": "resourceNotReady"})
        return original(method, url, json=json, params=params)
    fake.request = request
    provision(orchestrator, who, {"name": "lost", "network": {}})
    assert len(subnets) == 2


# ---- 2: public nodes are behind the switch (the other public paths: test_infra_security_s2.PUBLIC_PATHS)
def test_public_cluster_nodes_need_the_operators_allow_public_exposure():
    from gcp_fakes import config_dict, make_provider

    from cloudinfra.config import Config
    from cloudinfra.orchestration import validator
    request = InfrastructureRequest.model_validate({"region": "us-central1", "cluster": {"private_nodes": False}})
    closed = Config.model_validate({**config_dict(), "allow_public_exposure": False})
    assert [item.field for item in validator.validate(request, closed, make_provider(closed)[0]) if "allow_public_exposure" in item.message] == ["cluster.private_nodes"]
    private = InfrastructureRequest.model_validate({"region": "us-central1", "cluster": {}})
    assert not [item for item in validator.validate(private, closed, make_provider(closed)[0]) if "allow_public_exposure" in item.message]


# ---- 6: the ceiling is checked under the request's lock
def test_a_resize_is_priced_while_its_request_is_locked(tmp_path, monkeypatch):
    from cloudinfra.orchestration import provisioner
    orchestrator, fake, _, who = orchestrated(tmp_path, limits={"max_monthly_cost_usd": 5000.0})
    response = provision(orchestrator, who, {"name": "fleet", "compute": {"cpu": 2, "memory_gb": 8, "count": 2, "os": "ubuntu"}})
    lock = orchestrator.lock(response.request_id)
    priced = []
    original = provisioner.planner.cost_problem

    def cost_problem(*args, **kwargs):
        priced.append(lock._is_owned())
        return original(*args, **kwargs)
    monkeypatch.setattr(provisioner.planner, "cost_problem", cost_problem)
    orchestrator.resize_compute(response.resources.compute[0].id, 4, 16, who, wait_seconds=45)
    assert priced == [True]


# ---- 7: what the node-role check reads
SERVICE = "serviceAccount:cloudinfra@test-project.iam.gserviceaccount.com"


def test_a_custom_role_that_sets_the_project_policy_counts_and_one_that_does_not_or_cannot_be_read_does_not():
    from cloudinfra.providers.gcp.nodeaccount import SET_POLICY, unlimited_roles
    roles = {"projects/test-project/roles/everything": [SET_POLICY, "storage.buckets.get"], "organizations/1/roles/reader": ["storage.buckets.get"]}
    policy = {"bindings": [{"role": role, "members": [SERVICE]} for role in [*roles, "projects/test-project/roles/hidden"]]}
    assert unlimited_roles(policy, SERVICE, roles.get) == ["projects/test-project/roles/everything"]
    assert unlimited_roles(policy, SERVICE) == [], "without a reader custom roles are not looked at"


@pytest.mark.parametrize("role", ["roles/resourcemanager.organizationAdmin", "roles/resourcemanager.folderAdmin", "roles/resourcemanager.folderIamAdmin"])
def test_a_folder_or_organization_administrator_role_on_the_project_counts(role):
    from cloudinfra.providers.gcp.nodeaccount import unlimited_roles
    assert unlimited_roles({"bindings": [{"role": role, "members": [SERVICE]}]}, SERVICE) == [role]


def test_a_custom_roles_permissions_are_read_from_iam_and_an_unreadable_one_is_none():
    from cloudinfra.providers.gcp.nodeaccount import NodeAccounts
    asked = []

    class Api:
        def get(self, url):
            asked.append(url)
            if url.endswith("/hidden"):
                raise InfrastructureError(ErrorCode.PROVIDER_PERMISSION_DENIED, "no")
            return {"includedPermissions": ["resourcemanager.projects.setIamPolicy"]}
    accounts = NodeAccounts(Api())
    assert accounts._permissions("projects/p/roles/all") == ["resourcemanager.projects.setIamPolicy"]
    assert accounts._permissions("projects/p/roles/hidden") is None
    assert asked == ["https://iam.googleapis.com/v1/projects/p/roles/all", "https://iam.googleapis.com/v1/projects/p/roles/hidden"]


# ---- 8: credentials in more shapes, in both services' logs
@pytest.mark.parametrize("module", ["cloudinfra.observability.logging", "appdelivery.events"])
@pytest.mark.parametrize(("text", "kept"), [("key AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/+abcdEFGH= end", False),
                                            ("key q7w8e9r0t1y2u3i4o5p6a7s8d9f0g1h2j3k4l5z6x7 end", False),
                                            ("sha256:" + "ab12" * 16, True),
                                            ("projects/acme-apps/locations/us-central1/repositories/shop-images-for-team", True),
                                            ("cloudinfra-request-0123456789abcdef0123456789abcdef", True)])
def test_credentials_split_by_a_slash_or_in_one_case_are_redacted_and_digests_and_names_are_not(module, text, kept):
    import importlib
    redacted = importlib.import_module(module).redact_text(text)
    assert (redacted == text) is kept, redacted
