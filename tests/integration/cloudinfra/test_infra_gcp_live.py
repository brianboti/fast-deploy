"""Real Google Cloud, deliberately apart from the unit tests: runs only with CLOUDINFRA_INTEGRATION=1 and a project.

    CLOUDINFRA_INTEGRATION=1 CLOUDINFRA_PROJECT=my-project python -m pytest tests/integration/cloudinfra -q

Creates the smallest machine the service offers in us-central1 and destroys it; a run costs a few cents and leaves nothing
when it passes. The host needs a Google token (gcloud sign-in or an attached service account) with Compute Engine rights.

With CLOUDINFRA_TEST_MEMBER (user:someone@your-company.com) as well, a second test checks release A on the real APIs: a
grant on a machine's own policy, its IAP tunnel and a secret, then stop, start and revoke; it adds a few cents more.
The service's identity then also needs the getIamPolicy and setIamPolicy permissions of instances, IAP tunnel instances
and secrets.
"""
import os

import pytest

from cloudinfra.config import Config
from cloudinfra.models import InfrastructureRequest, RequestStatus, ResourceStatus
from cloudinfra.orchestration.provisioner import InlineExecutor, Orchestrator
from cloudinfra.providers import ProviderRegistry
from cloudinfra.state.state_manager import StateManager

pytestmark = pytest.mark.integration
LIVE = os.environ.get("CLOUDINFRA_INTEGRATION") == "1" and bool(os.environ.get("CLOUDINFRA_PROJECT"))


@pytest.mark.skipif(not LIVE, reason="set CLOUDINFRA_INTEGRATION=1 and CLOUDINFRA_PROJECT to run against a real project")
def test_one_small_machine_round_trip(tmp_path):
    config = Config.model_validate({"database": str(tmp_path / "live.db"), "providers": {"gcp": {"project_id": os.environ["CLOUDINFRA_PROJECT"], "allowed_regions": ["us-central1"],
                                                                                                "impersonate_service_account": os.environ.get("CLOUDINFRA_IMPERSONATE")}},
                                    "principals": [{"name": "live-test", "token_sha256": "0" * 64, "can_provision": True, "can_destroy": True}]})
    orchestrator = Orchestrator(config, StateManager(config.database), ProviderRegistry(config), InlineExecutor())
    principal = config.principals[0]
    request = InfrastructureRequest.model_validate({"provider": "gcp", "region": "us-central1", "name": "cloudinfra-live", "compute": {"cpu": 1, "memory_gb": 1, "boot_disk_gb": 10}})
    response = orchestrator.provision(request, principal, wait_seconds=45)
    try:
        assert response.status == RequestStatus.READY, response.error
        [machine] = response.resources.compute
        assert machine.status == ResourceStatus.READY and machine.internal_ip and machine.machine_type == "e2-micro"
        assert orchestrator.health(machine.id, principal).healthy is True
    finally:
        destroyed = orchestrator.destroy(response.request_id, principal, wait_seconds=45)
        assert destroyed.status == RequestStatus.DESTROYED, destroyed.error


MEMBER = os.environ.get("CLOUDINFRA_TEST_MEMBER")


@pytest.mark.skipif(not (LIVE and MEMBER), reason="also set CLOUDINFRA_TEST_MEMBER (user:someone@your-company.com) to check grants, stop and start")
def test_release_a_on_the_real_apis(tmp_path):
    domain = MEMBER.rsplit("@", 1)[-1]
    config = Config.model_validate({"database": str(tmp_path / "live.db"), "access": {"allowed_domains": [domain]},
                                    "providers": {"gcp": {"project_id": os.environ["CLOUDINFRA_PROJECT"], "allowed_regions": ["us-central1"],
                                                          "impersonate_service_account": os.environ.get("CLOUDINFRA_IMPERSONATE")}},
                                    "principals": [{"name": "live-test", "token_sha256": "0" * 64, "can_provision": True, "can_destroy": True}]})
    orchestrator = Orchestrator(config, StateManager(config.database), ProviderRegistry(config), InlineExecutor())
    principal = config.principals[0]
    request = InfrastructureRequest.model_validate({"provider": "gcp", "region": "us-central1", "name": "cloudinfra-live-a",
                                                    "compute": {"cpu": 1, "memory_gb": 1, "boot_disk_gb": 10, "max_run_hours": 1},
                                                    "secret": {"name": "probe", "generate_value": True}, "lifetime": {"hours": 2, "on_expiry": "destroy"},
                                                    "access": [{"member": MEMBER}]})
    response = orchestrator.provision(request, principal, wait_seconds=45)
    try:
        assert response.status == RequestStatus.READY, response.error
        [entry] = response.access
        assert entry.status == "GRANTED", entry.error
        [machine] = response.resources.compute
        assert machine.instance_id and {grant.role for grant in machine.access} == {"roles/compute.osLogin", "roles/iap.tunnelResourceAccessor"}
        assert machine.connect.commands["ssh"].endswith("--tunnel-through-iap")
        stopped = orchestrator.control(response.request_id, "stop", principal, wait_seconds=45)
        assert stopped.status == RequestStatus.STOPPED and stopped.resources.compute[0].status == ResourceStatus.STOPPED, stopped.operations[-1]
        started = orchestrator.control(response.request_id, "start", principal, wait_seconds=45)
        assert started.status == RequestStatus.READY and started.resources.compute[0].status == ResourceStatus.READY, started.operations[-1]
        revoked = orchestrator.manage_access(response.request_id, "revoke", MEMBER, principal)
        assert revoked.access[0].status == "REVOKED", revoked.access[0].error
    finally:
        destroyed = orchestrator.destroy(response.request_id, principal, wait_seconds=45)
        assert destroyed.status == RequestStatus.DESTROYED, destroyed.error
