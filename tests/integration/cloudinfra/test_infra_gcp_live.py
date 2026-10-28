"""Real Google Cloud, deliberately apart from the unit tests: runs only with CLOUDINFRA_INTEGRATION=1 and a project.

    CLOUDINFRA_INTEGRATION=1 CLOUDINFRA_PROJECT=my-project python -m pytest tests/integration/cloudinfra -q

Creates the smallest machine the service offers in us-central1 and destroys it; a run costs a few cents and leaves nothing
when it passes. The host needs a Google token (gcloud sign-in or an attached service account) with Compute Engine rights.
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
