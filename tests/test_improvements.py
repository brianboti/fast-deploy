import hashlib
import subprocess
import sys

import pytest
from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient

from labdeploy.config import Config
from labdeploy.server import build_app

BASE = {"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
        "principals": [{"name": "manager", "token_sha256": hashlib.sha256(b"test-token").hexdigest(), "applications": ["demo"], "can_deploy": True}]}


def test_bearer_scheme_is_case_insensitive_and_token_is_not(tmp_path):
    config = Config.model_validate({**BASE, "database": str(tmp_path / "db")})
    with TestClient(build_app(config, FakeGitHub())) as client:
        assert client.get("/applications", headers={"Authorization": "bearer test-token"}).status_code == 200
        assert client.get("/applications", headers={"Authorization": "BEARER  test-token "}).status_code == 200
        assert client.get("/applications", headers={"Authorization": "Bearer TEST-TOKEN"}).status_code == 401
        assert client.get("/applications", headers={"Authorization": "Basic test-token"}).status_code == 401
        assert client.get("/plans", headers={"Authorization": "Bearer test-token"}).json() == {"plans": []}


@pytest.mark.parametrize("hosts", [["*"], ["*:*"], ["*.example.edu"], ["deploy.example.edu:*:*"], [""]])
def test_wildcard_allowed_hosts_rejected(hosts):
    with pytest.raises(ValueError, match="allowed_hosts"):
        Config.model_validate({**BASE, "allowed_hosts": hosts})


def test_exact_allowed_hosts_accepted():
    config = Config.model_validate({**BASE, "allowed_hosts": ["deploy.example.edu", "127.0.0.1:*", "localhost:8000", "[::1]:8000"]})
    assert len(config.allowed_hosts) == 4


def test_list_plans_and_audit_events_are_per_identity(setup):
    c, p, gh = setup
    assert c.plans(p) == {"plans": []}
    plan = c.plan(p, "demo")
    c.deploy(p, plan["id"])
    listed = c.plans(p)["plans"]
    assert [x["id"] for x in listed] == [plan["id"]] and listed[0]["state"] == "dispatched"
    assert [e["event"] for e in c.status(p, plan["id"])["events"]] == ["planned", "dispatching", "dispatched", "succeeded"]  # the outcome is recorded too
    assert c.plans(p.model_copy(update={"name": "other", "can_deploy": False})) == {"plans": []}  # read-only identities see their own plans
    assert [x["owner"] for x in c.plans(p.model_copy(update={"name": "other"}))["plans"]] == ["manager"]  # deployers see every plan of their apps


def test_cli_reports_errors_without_tracebacks(tmp_path):
    bad = tmp_path / "platform.yaml"
    bad.write_text("name: demo\nunknown: 1\n")
    result = subprocess.run([sys.executable, "-m", "labdeploy.cli", "validate", str(bad)], capture_output=True, text=True)
    assert result.returncode == 1 and "Error:" in result.stderr and "Traceback" not in result.stderr
    existing = tmp_path / "deploy-gcp.yml"
    existing.write_text("keep me\n")
    render = [sys.executable, "-m", "labdeploy.cli", "render-workflow", "--platform-repo", "lab/platform", "--platform-sha", "a" * 40, "--output", str(existing)]
    result = subprocess.run(render, capture_output=True, text=True)
    assert result.returncode == 1 and "Refusing to overwrite" in result.stderr and existing.read_text() == "keep me\n"
    result = subprocess.run([sys.executable, "-m", "labdeploy.cli", "validate", str(tmp_path / "missing.yaml")], capture_output=True, text=True)
    assert result.returncode == 1 and "Error:" in result.stderr and "Traceback" not in result.stderr
