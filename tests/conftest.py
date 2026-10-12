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

    receipt = None
    recovery = None

    def head(self, app):
        return self.sha

    def repository_ids(self, repository):
        return {"repository_id": "123", "owner_id": "45"}

    platform_prs = []

    def open_platform_pull_request(self, repository, base_branch, branch, files, title, body, message):
        self.platform_prs.append({"repository": repository, "base": base_branch, "branch": branch, "files": dict(files), "title": title})
        return {"number": 7, "url": f"https://github.com/{repository}/pull/7", "branch": branch, "changed": sorted(files)}

    def commit(self, app, ref=None):
        sha = ref if ref and len(ref) == 40 else self.sha
        return {"sha": sha, "message": "Fix the login bug", "author": "Alice", "date": "2026-10-12T10:00:00Z", "url": "https://github.com/lab/demo/commit/" + sha}

    def compare(self, app, base_sha):
        return {"status": "ahead", "ahead_by": 2, "behind_by": 0, "commits": [{"sha": "b" * 40, "message": "Second change"}, {"sha": "c" * 40, "message": "Third change"}]}

    def release_receipt(self, app, run_id):
        return self.receipt

    def recovery_after(self, app, run):
        return self.recovery

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
        return {"id": 123, "status": "completed", "conclusion": "success", "html_url": "https://github.com/lab/demo/actions/runs/123",
                "created_at": "2026-10-12T10:00:00Z", "updated_at": "2026-10-12T10:08:00Z", "jobs": [
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
