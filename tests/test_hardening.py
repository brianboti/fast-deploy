import hashlib
from pathlib import Path

import pytest
import yaml
from conftest import TARGET, FakeGitHub
from fastapi.testclient import TestClient

from labdeploy.config import Config
from labdeploy.models import AppSpec
from labdeploy.release import CloudRun
from labdeploy.server import RateLimiter, build_app


def test_rate_limiter_counts_per_key_and_resets_after_window():
    clock = {"now": 100.0}
    limiter = RateLimiter(2, window_seconds=60, clock=lambda: clock["now"])
    assert limiter.allow("a") == (True, 0) and limiter.allow("a") == (True, 0)
    denied, retry_after = limiter.allow("a")
    assert denied is False and 1 <= retry_after <= 61
    assert limiter.allow("b") == (True, 0)
    clock["now"] += 60
    assert limiter.allow("a") == (True, 0)


def config(tmp_path, **overrides):
    return Config.model_validate({"applications": {"demo": {"repository": "lab/demo", "service": "demo", "target": TARGET}},
                                  "database": str(tmp_path / "db"),
                                  "principals": [{"name": "a", "token_sha256": hashlib.sha256(b"token-a").hexdigest(), "applications": ["demo"]},
                                                 {"name": "b", "token_sha256": hashlib.sha256(b"token-b").hexdigest(), "applications": ["demo"]}],
                                  **overrides})


def test_identity_rate_limit_returns_429_with_retry_after(tmp_path):
    with TestClient(build_app(config(tmp_path, rate_limit_per_minute=3), FakeGitHub())) as client:
        for _ in range(3):
            assert client.get("/applications", headers={"Authorization": "Bearer token-a"}).status_code == 200
        limited = client.get("/applications", headers={"Authorization": "Bearer token-a"})
        assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
        assert client.get("/applications", headers={"Authorization": "Bearer token-b"}).status_code == 200
        assert client.get("/healthz").status_code == 200
        mcp = client.post("/mcp", headers={"Authorization": "Bearer token-a", "Accept": "application/json, text/event-stream"},
                          json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert mcp.status_code == 429


def test_failed_logins_are_budgeted_per_client(tmp_path):
    with TestClient(build_app(config(tmp_path, auth_failures_per_minute=2), FakeGitHub())) as client:
        assert client.get("/applications", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/applications").status_code == 401
        assert client.get("/applications", headers={"Authorization": "Bearer wrong"}).status_code == 429
        assert client.get("/applications", headers={"Authorization": "Bearer token-a"}).status_code == 200


def test_rate_limit_configuration_bounds():
    with pytest.raises(ValueError):
        Config.model_validate({"applications": {}, "principals": [], "rate_limit_per_minute": 0})


def test_cloud_run_deploy_configures_startup_probe_and_concurrency():
    calls = []
    adapter = CloudRun("test-project", "us-central1", "demo", "demo-probe@test-project.iam.gserviceaccount.com", runner=lambda args: calls.append(args) or "")
    spec = AppSpec.model_validate({"name": "demo", "reviewed": True, "health_path": "/ready", "port": 9000, "concurrency": 40})
    adapter.deploy("us-central1-docker.pkg.dev/test-project/demo/demo@sha256:" + "b" * 64, spec, "rabc")
    args = next(call for call in calls if call[:3] == ["gcloud", "run", "deploy"])  # after reading the live containers (1.24.0)
    assert args[:3] == ["gcloud", "run", "deploy"] and "--no-traffic" in args and "--cpu-throttling" in args
    probe = args[args.index("--startup-probe") + 1]
    assert probe.startswith("httpGet.path=/ready,httpGet.port=9000,") and "failureThreshold=12" in probe and "timeoutSeconds=3" in probe
    assert args[args.index("--concurrency") + 1] == "40" and args[args.index("--port") + 1] == "9000"


def test_tools_declare_safety_hints_and_structured_output(tmp_path):
    headers = {"Authorization": "Bearer token-a", "Accept": "application/json, text/event-stream"}
    with TestClient(build_app(config(tmp_path), FakeGitHub())) as client:
        tools = {t["name"]: t for t in client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]}
        assert tools["deploy_staging"]["annotations"]["destructiveHint"] is True and tools["deploy_staging"]["annotations"]["readOnlyHint"] is False
        assert tools["create_deployment_plan"]["annotations"]["destructiveHint"] is False
        for name in ("list_applications", "analyze_repository", "get_deployment_status", "list_plans", "estimate_cost", "get_cost_report"):
            assert tools[name]["annotations"]["readOnlyHint"] is True, name
        assert all(t.get("title") and t.get("outputSchema", {}).get("type") == "object" and t["annotations"]["openWorldHint"] is False for t in tools.values())
        called = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                            "params": {"name": "list_applications", "arguments": {}}}).json()["result"]
        assert called["structuredContent"]["demo"]["service"] == "demo" and called["content"][0]["type"] == "text"


def test_deploy_template_attests_verifies_and_scans_before_release():
    workflow = yaml.safe_load(Path("templates/deploy-gcp.yml").read_text())
    deploy = workflow["jobs"]["deploy"]
    assert deploy["permissions"] == {"contents": "read", "id-token": "write", "attestations": "write"}
    names = [step.get("name") for step in deploy["steps"]]
    wanted = ["Push image and resolve immutable digest", "Attest build provenance", "Verify build provenance",
              "Summarize image vulnerabilities", "Deploy, probe, promote; restore traffic on failure"]
    positions = [names.index(name) for name in wanted]
    assert positions == sorted(positions)
    attest = deploy["steps"][names.index("Attest build provenance")]
    assert attest["uses"].startswith("actions/attest-build-provenance@") and len(attest["uses"].split("@")[1]) == 40
    assert set(attest["with"]) == {"subject-name", "subject-digest"}
    assert workflow["jobs"]["build"]["permissions"] if "permissions" in workflow["jobs"]["build"] else workflow["permissions"] == {"contents": "read"}


def test_template_action_pins_match_platform_ci_pins():
    def pins(path):
        found = {}
        for line in Path(path).read_text().splitlines():
            if "uses:" in line:
                action, _, rest = line.split("uses:")[1].strip().partition("@")
                found[action] = rest.split()[0]
        return found
    ci = pins(".github/workflows/ci.yml")
    for template in sorted(Path("templates").glob("*.yml")):
        for action, sha in pins(template).items():
            if action in ci:
                assert ci[action] == sha, f"{template.name} pins {action}@{sha} but ci.yml pins {ci[action]}"
