"""Shared fixtures and explicit fakes. No test touches GitHub or a cloud."""
import copy
import hashlib

import pytest
import yaml

from labdeploy.config import Config
from labdeploy.controller import Controller, Store

TARGET = {
    "project_id": "test-project", "region": "us-central1", "service": "demo", "registry": "demo",
    "deploy_service_account": "demo-deploy@test-project.iam.gserviceaccount.com",
    "probe_service_account": "demo-probe@test-project.iam.gserviceaccount.com",
    "wif_provider": "projects/123456789/locations/global/workloadIdentityPools/demo-gh/providers/github",
    "recovery_bucket": "test-project-demo-recovery",
}


class FakeGitHub:
    sha = "a" * 40
    calls = 0
    fail = False
    spec = {"name": "demo", "reviewed": True}

    def head(self, app):
        return self.sha

    def file(self, app, sha, filename):
        return yaml.safe_dump(self.spec)

    def dispatch(self, app, plan):
        self.calls += 1
        if self.fail:
            raise TimeoutError()
        return {"workflow_run_id": 123}

    def cost_report(self, app):
        return {"report": None, "note": "fake"}

    def status(self, app, plan):
        def steps(*names):
            return [{"name": name, "status": "completed", "conclusion": "success"} for name in names]
        return {"status": "completed", "conclusion": "success", "jobs": [
            {"name": "build", "steps": steps("Run unit tests", "Audit Python dependencies")},
            {"name": "deploy", "steps": steps("Validate artifact and deployment configuration", "Deploy, probe, promote; restore traffic on failure")},
        ]}


@pytest.fixture
def setup(tmp_path):
    config = Config.model_validate({
        "live_dispatch": True, "database": str(tmp_path / "plans.db"),
        "applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
        "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True}],
    })
    github = FakeGitHub()
    github.spec = copy.deepcopy(FakeGitHub.spec)
    controller = Controller(config, github, Store(config.database))
    return controller, config.principals[0], github
